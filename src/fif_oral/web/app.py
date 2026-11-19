"""Local WebUI API. Business workflows are shared with the native client."""
import asyncio
import base64
import json
import re
import secrets
import shutil
import tempfile
import time
import wave
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event, RLock

from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from fif_oral.audio.assessment import Cancelled, UnsupportedQuestion, challenge_items, challenge_kind
from fif_oral.api.auth import CaptchaRequired, LoginFlow
from fif_oral.automation import AutomationScope, AutomationStore, run_queue
from fif_oral.api.client import ApiError, FiFClient, SessionExpired, plain, question_groups, records
from fif_oral.audio.speech import MODEL_FILES, SpeechSettings, save_json
from fif_oral.storage import Account, Vault, VaultError
from fif_oral.environment.manager import InstallationManager, catalog, default_ffmpeg
from fif_oral.paths import default_data_dir


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Unlock(Input):
    password: str = Field(min_length=1, max_length=1024)
    confirmation: str = ''


class AccountInput(Input):
    name: str = Field(min_length=1, max_length=100)
    username: str = Field(min_length=1, max_length=200)
    password: str | None = Field(default=None, max_length=1024)


class LoginInput(Input):
    password: str = Field(default='', max_length=1024)
    answer: str = Field(default='', max_length=64)
    token: str = Field(default='', max_length=16000)


class SettingsInput(Input):
    settings: dict[str, str]
    ffmpeg: str = ''


class QueueInput(Input):
    account_id: str
    scope: dict[str, str]
    voice_id: str | None = None
    skip_qualified: bool = True


class SetupInput(Input):
    component: str
    accept_license: bool = False


class LocalState:
    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve()
        self.vault = None
        self.cookies = set()
        self.sessions = {}
        self.flows = {}
        self.mutex = RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='fif-web-queue')
        self.jobs = {}
        self.active = None
        self.installer = InstallationManager(self.root)

    @property
    def busy(self):
        job = self.jobs.get(self.active)
        return bool(job and not job['future'].done())

    def editable(self):
        if self.busy or self.installer.busy:
            raise HTTPException(409, '请先暂停任务队列或等待环境安装结束，再进行此操作。')

    def account(self, identity):
        account = next((a for a in self.vault.accounts if a.id == identity), None)
        if account is None:
            raise HTTPException(404, '账号不存在。')
        return account

    def client(self, identity):
        self.account(identity)
        client = self.sessions.get(identity)
        if client is None or client.expired():
            raise HTTPException(428, '请先登录这个 FIF 口语账号。')
        return client

    def settings(self):
        config = self.root / 'web.json'
        ffmpeg = json.loads(config.read_text(encoding='utf-8')).get('ffmpeg', '') if config.exists() else ''
        return {'settings': asdict(SpeechSettings.load(self.root)), 'ffmpeg': ffmpeg or default_ffmpeg(self.root)}

    def public_account(self, account):
        client = self.sessions.get(account.id)
        return {'id': account.id, 'name': account.name, 'username': account.username,
                'has_password': bool(account.password), 'user_id': account.user_id,
                'default_voice': account.default_voice, 'voices': [
                    {'id': v.id, 'name': v.name} for v in account.voices],
                'connected': bool(client and not client.expired()),
                'display_name': client.name if client else ''}

    def load_queue(self, identity):
        if not re.fullmatch(r'[0-9a-f]{64}', identity):
            raise HTTPException(404, '队列不存在。')
        path = self.root / 'queues' / identity / 'queue.json'
        if not path.is_file():
            raise HTTPException(404, '队列不存在。')
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            context = data['context']
            scope = AutomationScope(**context['scope'], title=data.get('title', ''))
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise ApiError('队列记录无法读取，请检查该队列的本地进度文件。') from error
        account = self.account(context['account_id'])
        if not account.user_id or account.user_id != context['user_id']:
            raise HTTPException(404, '队列不属于当前账号身份。')
        store = AutomationStore(self.root / 'queues', account.id, account.user_id, scope)
        if store.path.parent.name != identity:
            raise HTTPException(404, '队列范围不匹配。')
        return store

    def queue_view(self, identity):
        with self.mutex:
            if identity in self.jobs:
                job = self.jobs[identity]
                context = job['store'].context
                if self.account(context['account_id']).user_id != context['user_id']:
                    raise HTTPException(404, '队列不属于当前账号身份。')
                data = deepcopy(job['snapshot'])
                return {'id': identity, 'data': data, 'busy': not job['future'].done(),
                        'logs': list(job['logs']), 'error': job.get('error', '')}
            store = self.load_queue(identity)
            return {'id': identity, 'data': store.data, 'busy': False, 'logs': [], 'error': ''}

    def start(self, account_id, scope, voice_id, skip_qualified):
        with self.mutex:
            self.editable()
            client = self.client(account_id)
            account = self.account(account_id)
            config = self.settings()
            settings = SpeechSettings(**config['settings'])
            settings.validate(neural=False)
            voice = next((v for v in account.voices if v.id == voice_id), None)
            if voice_id and voice is None:
                raise HTTPException(400, '参考音色不属于这个账号。')
            store = AutomationStore(self.root / 'queues', account_id, client.user_id, scope)
            requires_reference = settings.backend in MODEL_FILES or settings.fallback_model != 'off'
            store.configure(settings, self.vault.voice_path(voice) if voice and requires_reference else None,
                            config['ffmpeg'], skip_qualified)
            identity = store.path.parent.name
            job = {'store': store, 'snapshot': deepcopy(store.data), 'cancel': Event(), 'logs': deque(maxlen=400)}

            def progress(message):
                with self.mutex:
                    job['snapshot'] = deepcopy(store.data)
                    job['logs'].append(f'{time.strftime("%H:%M:%S")}  {message}')

            def run():
                try:
                    run_queue(client, store, self.root, job['cancel'], progress)
                except (Cancelled, ApiError, OSError) as error:
                    with self.mutex:
                        job['error'] = str(error)
                except Exception as error:
                    with self.mutex:
                        job['error'] = f'执行中断（{type(error).__name__}），可检查进度后继续。'
                finally:
                    with self.mutex:
                        job['snapshot'] = deepcopy(store.data)

            job['future'] = self.pool.submit(run)
            self.jobs[identity] = job
            self.active = identity
            return identity

    def close(self):
        with self.mutex:
            for job in self.jobs.values():
                job['cancel'].set()
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.installer.close()
        self.cookies.clear()
        self.sessions.clear()
        self.flows.clear()
        self.vault = None


def create_app(data_dir=None, *, allowed_hosts=None):
    state = LocalState(data_dir or default_data_dir())
    web_root = Path(__file__).resolve().parent / 'static'

    @asynccontextmanager
    async def lifespan(app):
        state.root.mkdir(parents=True, exist_ok=True)
        yield
        await asyncio.to_thread(state.close)

    app = FastAPI(title='FIF 口语本机 WebUI', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.local = state
    hosts = set(allowed_hosts or {'127.0.0.1:8765', 'localhost:8765'})

    @app.middleware('http')
    async def local_only(request, call_next):
        host = request.headers.get('host', '')
        origin = request.headers.get('origin')
        if host not in hosts or (origin and origin != 'http://' + host):
            return JSONResponse({'detail': '仅允许本机同源访问。'}, status_code=403)
        if request.url.path.startswith('/api/') and request.headers.get('x-fif-client') != '1':
            return JSONResponse({'detail': '请求来源不符合本地客户端约定。'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; media-src 'self' blob: https://*.fifedu.com; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    def authorized(request: Request):
        if not state.vault or request.cookies.get('fif_local_session') not in state.cookies:
            raise HTTPException(401, '请先解锁本地账号库。')

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, error):
        return JSONResponse({'detail': '参数格式不正确，请检查填写内容。'}, status_code=422)

    @app.exception_handler(ApiError)
    async def api_error(request, error):
        return JSONResponse({'detail': str(error)}, status_code=428 if isinstance(error, SessionExpired) else 400)

    @app.exception_handler(VaultError)
    async def vault_error(request, error):
        return JSONResponse({'detail': str(error)}, status_code=400)

    @app.exception_handler(OSError)
    async def file_error(request, error):
        return JSONResponse({'detail': '本地文件无法读写，请检查路径和目录权限。'}, status_code=500)

    @app.get('/api/bootstrap')
    def bootstrap(request: Request):
        unlocked = state.vault is not None and request.cookies.get('fif_local_session') in state.cookies
        return {'unlocked': unlocked, 'exists': (state.root / 'accounts.json').exists(),
                'active_queue': state.active if unlocked and state.busy else None,
                'environment_busy': state.installer.busy if unlocked else False}

    @app.post('/api/unlock')
    def unlock(body: Unlock, response: Response):
        with state.mutex:
            path = state.root / 'accounts.json'
            if not path.exists() and body.password != body.confirmation:
                raise HTTPException(400, '两次主密码不一致。')
            vault = Vault(path, body.password)
            if state.vault is None:
                state.vault = vault
            token = secrets.token_urlsafe(32)
            if len(state.cookies) >= 16:
                state.cookies.clear()
            state.cookies.add(token)
            response.set_cookie('fif_local_session', token, httponly=True, samesite='strict', path='/')
            return {'ok': True}

    @app.post('/api/lock', dependencies=[Depends(authorized)])
    def lock(response: Response):
        with state.mutex:
            state.editable()
            state.cookies.clear()
            state.sessions.clear()
            state.flows.clear()
            state.vault = None
            response.delete_cookie('fif_local_session', path='/')
            return {'ok': True}

    protected = [Depends(authorized)]

    @app.get('/api/accounts', dependencies=protected)
    def accounts():
        with state.mutex:
            return {'accounts': [state.public_account(a) for a in state.vault.accounts], 'active': state.vault.active}

    def save_account(body, identity=None):
        with state.mutex:
            state.editable()
            old = state.account(identity) if identity else Account()
            changed = body.username.strip().casefold() != old.username.casefold()
            account = replace(old, name=body.name.strip(), username=body.username.strip(),
                              password=old.password if body.password is None else body.password,
                              user_id='' if changed else old.user_id)
            if not account.name or not account.username:
                raise HTTPException(400, '账号别名和登录账号不能为空。')
            if any(a.id != account.id and a.username.casefold() == account.username.casefold() for a in state.vault.accounts):
                raise HTTPException(409, '这个登录账号已经添加。')
            values = [account if a.id == account.id else a for a in state.vault.accounts]
            if not identity:
                values.append(account)
            state.vault.save(values, state.vault.active or account.id)
            if changed or body.password is not None:
                state.sessions.pop(account.id, None)
                state.flows.pop(account.id, None)
            return state.public_account(account)

    @app.post('/api/accounts', dependencies=protected)
    def add_account(body: AccountInput):
        return save_account(body)

    @app.put('/api/accounts/{identity}', dependencies=protected)
    def edit_account(identity: str, body: AccountInput):
        return save_account(body, identity)

    @app.delete('/api/accounts/{identity}', dependencies=protected)
    def delete_account(identity: str):
        with state.mutex:
            state.editable()
            state.account(identity)
            values = [a for a in state.vault.accounts if a.id != identity]
            active = state.vault.active if state.vault.active != identity else (values[0].id if values else '')
            state.vault.save(values, active)
            state.sessions.pop(identity, None)
            state.flows.pop(identity, None)
            return {'ok': True}

    @app.post('/api/accounts/{identity}/select', dependencies=protected)
    def select_account(identity: str):
        with state.mutex:
            state.account(identity)
            state.vault.save(state.vault.accounts, identity)
            return {'ok': True}

    @app.post('/api/accounts/{identity}/login', dependencies=protected)
    def login(identity: str, body: LoginInput):
        # LoginFlow carries cookies and captcha state. Serializing login also prevents account edits mid-login.
        with state.mutex:
            state.editable()
            account = state.account(identity)
            flow = state.flows.setdefault(identity, LoginFlow())
            try:
                if body.token:
                    client = FiFClient.connect(body.token, '10003', account.user_id, account.username)
                else:
                    password = body.password or account.password
                    if not password:
                        raise HTTPException(400, '请输入密码。')
                    client = flow.sign_in(account.username, password, account.user_id, body.answer)
            except CaptchaRequired as error:
                return {'connected': False, 'message': str(error),
                        'captcha': 'data:image/png;base64,' + base64.b64encode(error.image).decode('ascii')}
            bound = replace(account, user_id=client.user_id)
            state.vault.save([bound if a.id == identity else a for a in state.vault.accounts], state.vault.active)
            state.sessions[identity] = client
            state.flows.pop(identity, None)
            return {'connected': True, 'account': state.public_account(bound)}

    @app.post('/api/accounts/{identity}/voices', dependencies=protected)
    def import_voice(identity: str, file: UploadFile):
        with state.mutex:
            state.editable()
            account = state.account(identity)
            with tempfile.TemporaryDirectory(prefix='fif-voice-') as folder:
                source = Path(folder) / 'reference.wav'
                total = 0
                with source.open('wb') as output:
                    while chunk := file.file.read(1024 * 1024):
                        total += len(chunk)
                        if total > 64 * 1024 * 1024:
                            raise HTTPException(413, '参考 WAV 最大为 64 MB。')
                        output.write(chunk)
                try:
                    voice = state.vault.import_voice(source)
                except (wave.Error, EOFError) as error:
                    raise HTTPException(400, '请选择有效的 PCM WAV 文件。') from error
                voice.name = Path((file.filename or '参考音色.wav').replace('\\', '/')).stem[:100]
                updated = replace(account, voices=[*account.voices, voice], default_voice=account.default_voice or voice.id)
                try:
                    state.vault.save([updated if a.id == identity else a for a in state.vault.accounts], state.vault.active)
                except BaseException:
                    state.vault.voice_path(voice).unlink(missing_ok=True)
                    raise
            return state.public_account(updated)

    @app.get('/api/accounts/{identity}/voices/{voice_id}/audio', dependencies=protected)
    def voice_audio(identity: str, voice_id: str):
        account = state.account(identity)
        voice = next((v for v in account.voices if v.id == voice_id), None)
        if voice is None or not state.vault.voice_path(voice).is_file():
            raise HTTPException(404, '音色文件不存在。')
        return FileResponse(state.vault.voice_path(voice), media_type='audio/wav')

    @app.post('/api/accounts/{identity}/voices/{voice_id}/default', dependencies=protected)
    def default_voice(identity: str, voice_id: str):
        with state.mutex:
            state.editable()
            account = state.account(identity)
            if not any(v.id == voice_id for v in account.voices):
                raise HTTPException(404, '音色不存在。')
            state.vault.save([replace(a, default_voice=voice_id) if a.id == identity else a for a in state.vault.accounts], state.vault.active)
            return {'ok': True}

    @app.delete('/api/accounts/{identity}/voices/{voice_id}', dependencies=protected)
    def delete_voice(identity: str, voice_id: str):
        with state.mutex:
            state.editable()
            account = state.account(identity)
            voice = next((v for v in account.voices if v.id == voice_id), None)
            if voice is None:
                raise HTTPException(404, '音色不存在。')
            voices = [v for v in account.voices if v.id != voice_id]
            selected = account.default_voice if account.default_voice != voice_id else (voices[0].id if voices else '')
            state.vault.save([replace(a, voices=voices, default_voice=selected) if a.id == identity else a for a in state.vault.accounts], state.vault.active)
            state.vault.voice_path(voice).unlink(missing_ok=True)
            return {'ok': True}

    @app.get('/api/settings', dependencies=protected)
    def settings():
        return state.settings()

    @app.get('/api/edge/voices', dependencies=protected)
    def edge_voices():
        from fif_oral.audio.edge import english_voices
        return english_voices(Event())

    @app.get('/api/environment', dependencies=protected)
    def environment_status():
        return {'task': state.installer.snapshot(), 'resources': catalog(state.root)}

    @app.post('/api/environment', dependencies=protected)
    def environment_install(body: SetupInput):
        with state.mutex:
            state.editable()
            state.installer.start(body.component, accept_license=body.accept_license)
            return state.installer.snapshot()

    @app.post('/api/environment/cancel', dependencies=protected)
    def environment_cancel():
        state.installer.cancel.set()
        return {'ok': True}

    @app.put('/api/settings', dependencies=protected)
    def set_settings(body: SettingsInput):
        with state.mutex:
            state.editable()
            try:
                values = SpeechSettings(**body.settings)
            except TypeError as error:
                raise HTTPException(400, '模型设置字段不正确。') from error
            values.validate(neural=False)
            values.save(state.root)
            save_json(state.root / 'web.json', {'ffmpeg': body.ffmpeg.strip()})
            return state.settings()

    def node(row, kind, task_id='', unit_id=''):
        identity = (row.get('taskId') or row.get('id')) if kind == 'task' else (
            row.get('unitid') or row.get('unitId') or row.get('id')) if kind == 'unit' else row.get('levelId')
        identity = str(identity or '').split(',')[-1]
        return {'id': identity, 'kind': kind,
                'title': plain(row.get({'task': 'taskName', 'unit': 'unitName', 'level': 'levelName'}[kind])) or '未命名',
                'task_id': identity if kind == 'task' else task_id,
                'unit_id': identity if kind == 'unit' else unit_id,
                'score': row.get({'task': 'score', 'unit': 'totalScore', 'level': 'levelScore'}[kind]),
                'complete': row.get('complete'), 'end_time': row.get('endTime', '')}

    @app.get('/api/catalog/{account_id}/tasks', dependencies=protected)
    def tasks(account_id: str, status: int = 1):
        if status not in (1, 2):
            raise HTTPException(400, '任务状态无效。')
        return [node(row, 'task') for row in state.client(account_id).tasks(status)]

    @app.get('/api/catalog/{account_id}/tasks/{task_id}', dependencies=protected)
    def task(account_id: str, task_id: str):
        data = state.client(account_id).task(task_id)
        return [node(row, 'unit', task_id) for row in records(data.get('ttdList'))]

    @app.get('/api/catalog/{account_id}/tasks/{task_id}/units/{unit_id}', dependencies=protected)
    def unit(account_id: str, task_id: str, unit_id: str):
        data = state.client(account_id).unit(task_id, unit_id)
        return [node(row, 'level', task_id, unit_id) for row in records(data.get('levelList'))]

    @app.get('/api/catalog/{account_id}/levels/{level_id}', dependencies=protected)
    def level(account_id: str, level_id: str):
        data = state.client(account_id).level(level_id)
        reason = ''
        try:
            items = challenge_items(data)
            supported = bool(items)
        except (UnsupportedQuestion, ApiError) as error:
            reason, supported = str(error), False
        return {'supported': supported, 'reason': reason, 'kind': challenge_kind(data), 'groups': [
            {'name': name, 'kind': kind, 'prompts': [asdict(prompt) for prompt in prompts]}
            for name, kind, prompts in question_groups(data, for_practice=True)]}

    @app.post('/api/queues', dependencies=protected)
    def start_queue(body: QueueInput):
        try:
            scope = AutomationScope(**body.scope)
        except TypeError as error:
            raise HTTPException(400, '自动执行范围不正确。') from error
        scope.validate()
        identity = state.start(body.account_id, scope, body.voice_id, body.skip_qualified)
        return state.queue_view(identity)

    @app.get('/api/queues', dependencies=protected)
    def list_queues(account_id: str):
        account = state.account(account_id)
        rows = []
        for path in (state.root / 'queues').glob('*/queue.json'):
            try:
                with state.mutex:
                    data = (deepcopy(state.jobs[path.parent.name]['snapshot']) if path.parent.name in state.jobs
                            else json.loads(path.read_text(encoding='utf-8')))
                if data['context']['account_id'] != account_id or data['context']['user_id'] != account.user_id:
                    continue
                rows.append({'id': path.parent.name, 'title': data['title'], 'stage': data['stage'],
                             'updated': data.get('updated', 0), 'scope': data['context']['scope']})
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(rows, key=lambda row: row['updated'], reverse=True)

    @app.get('/api/queues/{identity}', dependencies=protected)
    def get_queue(identity: str):
        return state.queue_view(identity)

    @app.post('/api/queues/{identity}/pause', dependencies=protected)
    def pause_queue(identity: str):
        with state.mutex:
            state.queue_view(identity)
            job = state.jobs.get(identity)
            if job:
                job['cancel'].set()
            return {'ok': True}

    @app.post('/api/queues/{identity}/resume', dependencies=protected)
    def resume_queue(identity: str):
        store = state.load_queue(identity)
        account = state.account(store.context['account_id'])
        voice_id = next((v.id for v in account.voices if store.data.get('voice') and
                         str(state.vault.voice_path(v).resolve()) == store.data['voice']), None)
        identity = state.start(account.id, store.scope, voice_id, store.data.get('skip_qualified', True))
        return state.queue_view(identity)

    app.mount('/static', StaticFiles(directory=web_root), name='static')

    @app.get('/')
    def index():
        return FileResponse(web_root / 'index.html', media_type='text/html')

    return app
