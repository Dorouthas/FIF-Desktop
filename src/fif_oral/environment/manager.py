"""Explicit, resumable local dependency installation shared by both interfaces."""
import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from http.client import IncompleteRead
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path, PurePosixPath
from threading import Event, RLock, Thread
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fif_oral.audio.assessment import Cancelled, check_cancel
from fif_oral.api.client import ApiError
from fif_oral.audio.speech import MODEL_FILES, SpeechSettings, save_json
from fif_oral.paths import RESOURCE_ROOT, default_data_dir


BASE = RESOURCE_ROOT
MANIFEST = json.loads((BASE / 'dependency-manifest.json').read_text(encoding='utf-8'))
COMPONENTS = ('check', 'edge', 'edge_test', 'ffmpeg', 'runtime', 'your_tts', 'xtts_v2')


def digest(path, cancel):
    hasher = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(1024 * 1024):
            check_cancel(cancel)
            hasher.update(chunk)
    return hasher.hexdigest()


class HTTPSRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != 'https':
            raise ApiError('下载源重定向到了非 HTTPS 地址。')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class DownloadUnavailable(ApiError):
    pass


def download(spec, target, cancel, progress):
    sources = [spec['url'], *spec.get('mirrors', [])]
    for index, url in enumerate(sources):
        try:
            return _download({**spec, 'url': url, 'identity_url': spec['url']}, target, cancel, progress)
        except DownloadUnavailable:
            if index == len(sources) - 1:
                raise
            progress(message='当前下载源连接失败，正在切换到预设镜像。')


def _download(spec, target, cancel, progress):
    """Retain partial files on cancel/network errors; verify before atomic promotion."""
    if urlsplit(spec['url']).scheme != 'https':
        raise ApiError('自动下载仅支持预设的 HTTPS 来源。')
    target.parent.mkdir(parents=True, exist_ok=True)
    expected = spec.get('sha256')
    size = spec.get('size')
    if target.is_file() and (not size or target.stat().st_size == size) and expected and digest(target, cancel) == expected:
        progress(message=f'复用已校验文件：{target.name}', received=target.stat().st_size, total=size)
        return target
    if target.is_file() and size and target.stat().st_size == size and not expected and spec.get('files'):
        progress(message=f'复用已下载压缩包：{target.name}；解压后将逐文件校验。')
        return target
    partial = target.with_name(target.name + '.part')
    metadata = partial.with_name(partial.name + '.json')
    identity = {key: spec.get(key) for key in ('url', 'sha256', 'size')}
    identity['url'] = spec.get('identity_url', spec['url'])
    try:
        same = json.loads(metadata.read_text(encoding='utf-8')) == identity
    except (OSError, ValueError):
        same = False
    if not same or (size and partial.exists() and partial.stat().st_size > size):
        partial.unlink(missing_ok=True)
        save_json(metadata, identity)
    for attempt in range(3):
        check_cancel(cancel)
        offset = partial.stat().st_size if partial.exists() else 0
        if (size and offset == size) or (offset and not size and expected and digest(partial, cancel) == expected):
            break
        headers = {'User-Agent': 'FiF-Setup/1', 'Accept-Encoding': 'identity'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
        try:
            with build_opener(HTTPSRedirects()).open(Request(spec['url'], headers=headers), timeout=20) as response:
                if offset and response.status == 206:
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                    if not match or int(match[1]) != offset or (size and int(match[3]) != size):
                        partial.unlink(missing_ok=True)
                        raise ApiError('下载续传位置不匹配，已重置部分文件，请重试。')
                    total = int(match[3])
                elif response.status == 200:
                    offset = 0
                    total = size or int(response.headers.get('Content-Length') or 0)
                else:
                    raise ApiError(f'下载服务响应异常（HTTP {response.status}）。')
                if total and total - offset > shutil.disk_usage(target.parent).free:
                    raise ApiError('磁盘空间不足，无法完成下载。')
                last = 0
                with partial.open('ab' if offset else 'wb') as output:
                    while True:
                        check_cancel(cancel)
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        output.write(chunk)
                        offset += len(chunk)
                        if total and offset > total:
                            raise ApiError('下载文件超过预期大小。')
                        if time.monotonic() - last >= .2:
                            progress(message=f'正在下载 {target.name}', received=offset, total=total or None)
                            last = time.monotonic()
                if total and offset != total:
                    raise OSError('incomplete download')
                break
        except HTTPError as error:
            raise DownloadUnavailable(f'下载服务返回 HTTP {error.code}，稍后可继续重试。') from error
        except (URLError, OSError, TimeoutError, IncompleteRead) as error:
            if attempt == 2:
                raise DownloadUnavailable('下载连接中断，已保留部分文件，可继续重试。') from error
            progress(message='下载连接中断，正在续传。')
            if cancel.wait(1):
                check_cancel(cancel)
    check_cancel(cancel)
    progress(message=f'正在校验 {target.name}', received=partial.stat().st_size, total=size)
    if (size and partial.stat().st_size != size) or (expected and digest(partial, cancel) != expected):
        partial.unlink(missing_ok=True)
        metadata.unlink(missing_ok=True)
        raise ApiError('下载文件校验失败，未使用该文件。再次运行会重新下载。')
    partial.replace(target)
    metadata.unlink(missing_ok=True)
    return target


def extract_selected(archive, destination, names, cancel):
    """Flatten an explicit file allowlist, rejecting traversal, symlinks and duplicates."""
    destination.mkdir(parents=True, exist_ok=True)
    selected = {}
    with zipfile.ZipFile(archive) as package:
        for info in package.infolist():
            parts = PurePosixPath(info.filename.replace('\\', '/'))
            if (parts.is_absolute() or '..' in parts.parts or ':' in info.filename or
                    stat.S_ISLNK(info.external_attr >> 16)):
                raise ApiError('压缩包包含不安全的路径。')
            if not info.is_dir() and parts.name in names:
                if parts.name in selected or info.file_size > 3 * 1024**3:
                    raise ApiError('压缩包文件重复或大小异常。')
                selected[parts.name] = info
        if not set(names) <= selected.keys():
            raise ApiError('压缩包缺少必需文件。')
        total = sum(info.file_size for info in selected.values())
        if total > shutil.disk_usage(destination).free:
            raise ApiError('磁盘空间不足，无法解压。')
        for name, info in selected.items():
            check_cancel(cancel)
            path = destination / name
            temporary = path.with_name(name + '.extracting')
            try:
                with package.open(info) as source, temporary.open('wb') as output:
                    while chunk := source.read(1024 * 1024):
                        check_cancel(cancel)
                        output.write(chunk)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)


def run_command(command, cancel, progress, *, environment=None, timeout=3600):
    check_cancel(cancel)
    process = subprocess.Popen([str(value) for value in command], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
        env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    lines = deque(maxlen=25)
    def read():
        for line in process.stdout:
            value = line.strip()
            if value:
                lines.append(value)
                progress(message=value[:700])
    reader = Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            check_cancel(cancel)
            if time.monotonic() > deadline:
                raise ApiError('安装步骤等待超时，可重试并复用已下载缓存。')
            cancel.wait(.1)
        reader.join(timeout=3)
        if process.returncode:
            raise ApiError(f'安装步骤失败（退出码 {process.returncode}）。' + '\n' + '\n'.join(list(lines)[-4:]))
        return '\n'.join(lines)
    finally:
        if process.poll() is None:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True,
                               timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            else:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        reader.join(timeout=3)
        process.stdout.close()


def default_ffmpeg(data_dir):
    path = Path(data_dir) / 'web.json'
    if path.is_file():
        try:
            configured = json.loads(path.read_text(encoding='utf-8')).get('ffmpeg')
            if configured and Path(configured).is_file():
                return configured
        except (ValueError, OSError):
            pass
    managed = Path(data_dir) / 'tools' / 'ffmpeg' / MANIFEST['ffmpeg']['version'] / 'ffmpeg.exe'
    return str(managed) if managed.is_file() else shutil.which('ffmpeg') or ''


def runtime_python(data_dir):
    return Path(data_dir) / 'runtime' / 'tts-cpu' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def probe_runtime(python, cancel, progress):
    if not python or not Path(python).is_file():
        raise ApiError('尚未找到 TTS Python 环境。')
    script = ("import json,sys,importlib.metadata as m; import torch,transformers,soundfile; "
              "from TTS.utils.synthesizer import Synthesizer; "
              "print('FIF_RUNTIME='+json.dumps({'python':sys.executable,'coqui':m.version('coqui-tts'),"
              "'torch':torch.__version__,'transformers':transformers.__version__,'cuda':torch.cuda.is_available()}))")
    environment = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', PYTHONIOENCODING='utf-8')
    output = run_command([python, '-c', script], cancel, progress, environment=environment, timeout=90)
    match = next((line[len('FIF_RUNTIME='):] for line in output.splitlines() if line.startswith('FIF_RUNTIME=')), None)
    if not match:
        raise ApiError('TTS 环境检查没有返回有效结果。')
    return json.loads(match)


def verify_model(folder, model, cancel):
    return all((folder / row['name']).is_file() and (folder / row['name']).stat().st_size == row['size'] and
               digest(folder / row['name'], cancel) == row['sha256'] for row in model['files'])


def catalog(data_dir):
    settings = SpeechSettings.load(data_dir)
    from fif_oral.audio.edge import available
    try:
        checked_python = json.loads((Path(data_dir) / 'runtime-ready.json').read_text(encoding='utf-8'))['python']
        runtime_found = bool(settings.python and Path(settings.python).is_file() and
                             Path(checked_python).resolve() == Path(settings.python).resolve())
    except (OSError, ValueError, KeyError, TypeError):
        runtime_found = False
    rows = [{'id': 'edge', 'name': 'Edge TTS（默认）', 'found': available(), 'path': '', 'size': None,
             'description': '联网合成英文语音，无需大模型和参考 WAV。'},
            {'id': 'ffmpeg', 'name': 'FFmpeg', 'found': bool(default_ffmpeg(data_dir)),
             'path': default_ffmpeg(data_dir), 'size': None, 'description': '音频格式转换；优先使用现有安装。'},
            {'id': 'runtime', 'name': 'TTS Python 与运行依赖', 'found': runtime_found,
             'path': settings.python, 'size': None, 'description': '可选：供 YourTTS / XTTS v2 使用的独立 CPU 环境，建议预留 6 GB。'}]
    for key, item in MANIFEST['models'].items():
        folder = Path(getattr(settings, key)) if getattr(settings, key) else None
        rows.append({'id': key, 'name': item['name'], 'found': bool(folder and all((folder / f).is_file() for f in MODEL_FILES[key])),
                     'path': str(folder) if folder else '', 'size': item.get('size') or sum(f['size'] for f in item['files']),
                     'description': '只在下载后配置路径，不会切换当前生成策略。',
                     'license': item['license'], 'license_url': item['license_url']})
    return rows


def ensure_uv(root, cancel, progress):
    uv = shutil.which('uv')
    if uv:
        return uv
    tools = root / 'tools' / 'uv' / MANIFEST['uv']['version']
    archive = download(MANIFEST['uv'], root / 'downloads' / 'uv.zip', cancel, progress)
    extract_selected(archive, tools, ['uv.exe'], cancel)
    return str(tools / 'uv.exe')


def install_component(component, data_dir, cancel, progress, *, accept_license=False):
    root = Path(data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if component not in COMPONENTS:
        raise ApiError('未知的环境安装项目。')
    if component in MANIFEST['models'] and not accept_license:
        raise ApiError('请先阅读并确认所选模型的许可。')
    settings = SpeechSettings.load(root)
    if component == 'check':
        progress(message='正在检查音频工具、TTS 导入和本地模型文件。')
        from fif_oral.audio.edge import available
        report = {'resources': catalog(root), 'runtime': None, 'edge_available': available()}
        try:
            report['runtime'] = probe_runtime(settings.python, cancel, progress)
            save_json(root / 'runtime-ready.json', report['runtime'])
        except Cancelled:
            raise
        except ApiError as error:
            report['runtime_error'] = str(error)
        return report
    if platform.system() != 'Windows' or platform.machine().lower() not in {'amd64', 'x86_64'}:
        raise ApiError('当前自动安装支持 Windows x64；其他平台请手动配置路径。')
    if component == 'edge':
        from fif_oral.audio.edge import available
        if not available():
            if getattr(sys, 'frozen', False):
                raise ApiError('此安装包缺少 Edge TTS 组件，请更新应用安装包。')
            uv = ensure_uv(root, cancel, progress)
            run_command([uv, 'pip', 'install', '--python', sys.executable, '--index-url',
                         'https://pypi.org/simple', 'edge-tts==7.3.1'], cancel, progress)
            import importlib
            importlib.invalidate_caches()
            if not available():
                raise ApiError('安装后仍无法导入 Edge TTS，请重启应用。')
        return {'edge_available': True}
    if component == 'edge_test':
        from fif_oral.audio.edge import EdgeSpeech
        progress(message='正在通过 Edge TTS 合成固定测试句（不提交 FIF 口语）。')
        target = root / 'diagnostics' / 'edge-test.wav'
        target.parent.mkdir(parents=True, exist_ok=True)
        engine = EdgeSpeech(settings, default_ffmpeg(root))
        engine.synthesize('Hello, this is a speech test.', target, cancel)
        return {'edge_available': True, 'edge_network': True, 'audio': str(target)}
    if component == 'runtime':
        try:
            info = probe_runtime(settings.python, cancel, progress)
        except Cancelled:
            raise
        except ApiError:
            info = None
        if info:
            progress(message='当前 TTS 环境可用，已复用。')
        else:
            if shutil.disk_usage(root).free < 6 * 1024**3:
                raise ApiError('安装 TTS 环境建议至少预留 6 GB 可用空间。')
            uv = ensure_uv(root, cancel, progress)
            python = runtime_python(root)
            env = dict(os.environ, UV_PYTHON_INSTALL_DIR=str(root / 'runtime' / 'python'),
                       UV_CACHE_DIR=str(root / 'cache' / 'uv'), UV_PYTHON_BIN_DIR=str(root / 'runtime' / 'bin'),
                       UV_NO_MODIFY_PATH='1', PYTHONIOENCODING='utf-8', NO_COLOR='1')
            progress(message='正在准备独立 Python 3.11 环境。', received=0, total=None)
            run_command([uv, 'venv', '--allow-existing', '--managed-python', '--python', '3.11', python.parent.parent], cancel, progress, environment=env)
            progress(message='正在安装 CPU PyTorch 与 Torchaudio。')
            run_command([uv, 'pip', 'install', '--python', python, '--index-url', 'https://download.pytorch.org/whl/cpu',
                         'torch==2.9.1+cpu', 'torchaudio==2.9.1+cpu'], cancel, progress, environment=env)
            progress(message='正在安装 Coqui TTS 及兼容依赖。')
            run_command([uv, 'pip', 'install', '--python', python, '--index-url', 'https://pypi.org/simple',
                         '-r', BASE / 'requirements-tts.lock.txt'], cancel, progress, environment=env)
            info = probe_runtime(str(python), cancel, progress)
            replace(SpeechSettings.load(root), python=str(python)).save(root)
        save_json(root / 'runtime-ready.json', info)
        return info
    if component == 'ffmpeg':
        existing = default_ffmpeg(root)
        if existing:
            output = run_command([existing, '-version'], cancel, progress, timeout=15)
            if 'ffmpeg version' not in output:
                raise ApiError('找到的 FFmpeg 无法正常运行，请检查原配置路径。')
            executable = Path(existing)
        else:
            item = MANIFEST['ffmpeg']
            archive = download(item, root / 'downloads' / 'ffmpeg.zip', cancel, progress)
            folder = root / 'tools' / 'ffmpeg' / item['version']
            extract_selected(archive, folder, ['ffmpeg.exe', 'LICENSE'], cancel)
            executable = folder / 'ffmpeg.exe'
            output = run_command([executable, '-version'], cancel, progress, timeout=15)
            if 'ffmpeg version' not in output:
                raise ApiError('FFmpeg 安装后检查失败。')
            save_json(folder / 'source.json', item)
        path = root / 'web.json'
        config = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        save_json(path, {**config, 'ffmpeg': str(executable.resolve())})
        return {'ffmpeg': str(executable.resolve())}
    model = MANIFEST['models'][component]
    existing = getattr(settings, component)
    required = {**model, 'files': [row for row in model['files'] if row['name'] in MODEL_FILES[component]]}
    if existing and verify_model(Path(existing), required, cancel):
        progress(message='已校验现有模型，复用原目录。')
        folder = Path(existing)
    else:
        folder = root / 'models' / component
        if not verify_model(folder, model, cancel):
            staging = folder.with_name('.' + component + '.partial')
            staging.mkdir(parents=True, exist_ok=True)
            if model.get('url'):
                archive = download(model, root / 'downloads' / (component + '.zip'), cancel, progress)
                try:
                    extract_selected(archive, staging, [r['name'] for r in model['files']], cancel)
                except zipfile.BadZipFile as error:
                    archive.unlink(missing_ok=True)
                    raise ApiError('模型压缩包损坏，请重试下载。') from error
            else:
                for row in model['files']:
                    progress(message=f"准备模型文件 {row['name']}", received=0, total=row['size'])
                    download(row, staging / row['name'], cancel, progress)
            progress(message='正在核对模型全部文件。', received=0, total=None)
            if not verify_model(staging, model, cancel):
                # A damaged unsigned archive must not be reused indefinitely on every retry.
                if model.get('url'):
                    (root / 'downloads' / (component + '.zip')).unlink(missing_ok=True)
                raise ApiError('模型文件与预设版本校验不符，未更换当前模型路径。')
            check_cancel(cancel)
            if folder.exists():
                folder.rename(folder.with_name(component + '.previous-' + str(time.time_ns())))
            staging.rename(folder)
        save_json(folder / 'installation.json', {'version': model['version'], 'license': model['license'],
                  'license_url': model['license_url'], 'accepted_at': time.time(), 'files': model['files']})
    for row in model['files']:
        if row['name'] == 'LICENSE.txt':
            download(row, root / 'model-licenses' / component / 'LICENSE.txt', cancel, progress)
    save_json(root / 'model-licenses' / (component + '.json'), {
        'version': model['version'], 'license': model['license'], 'license_url': model['license_url'],
        'accepted_at': time.time()})
    replace(SpeechSettings.load(root), **{component: str(folder.resolve())}).save(root)
    return {component: str(folder.resolve())}


class InstallationManager:
    def __init__(self, data_dir):
        self.root = Path(data_dir)
        self.cancel = Event()
        self.mutex = RLock()
        self.future = None
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='fif-setup')
        self.data = {'stage': 'idle', 'component': None, 'message': '选择要检查或安装的项目。',
                     'received': 0, 'total': None, 'result': None, 'logs': []}

    @property
    def busy(self):
        return self.future is not None and not self.future.done()

    def snapshot(self):
        with self.mutex:
            return {**deepcopy(self.data), 'busy': self.busy}

    def start(self, component, *, accept_license=False):
        with self.mutex:
            if self.busy:
                raise ApiError('另一个环境安装任务正在运行。')
            if component not in COMPONENTS:
                raise ApiError('未知的环境安装项目。')
            if component in MANIFEST['models'] and not accept_license:
                raise ApiError('请先阅读并确认所选模型的许可。')
            self.cancel.clear()
            self.data.update(stage='running', component=component, message='正在启动…', received=0, total=None, result=None, logs=[])
            def progress(**values):
                with self.mutex:
                    message = values.get('message')
                    if message and (not self.data['logs'] or self.data['logs'][-1] != message):
                        self.data['logs'] = [*self.data['logs'][-199:], message]
                    self.data.update(values)
            def run():
                try:
                    result = install_component(component, self.root, self.cancel, progress, accept_license=accept_license)
                    with self.mutex:
                        self.data.update(stage='completed', result=result, message='检查完成。' if component == 'check' else '已完成，路径已配置。')
                except Cancelled:
                    with self.mutex:
                        self.data.update(stage='paused', message='已取消，下载进度和包缓存已保留。')
                except Exception as error:
                    with self.mutex:
                        self.data.update(stage='error', message=str(error) if isinstance(error, (ApiError, OSError, zipfile.BadZipFile)) else f'安装失败（{type(error).__name__}）。')
            self.future = self.pool.submit(run)

    def close(self):
        self.cancel.set()
        self.pool.shutdown(wait=True, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser(description='FIF 口语环境检查与安装')
    parser.add_argument('component', choices=COMPONENTS)
    parser.add_argument('--data-dir', type=Path, default=default_data_dir())
    parser.add_argument('--accept-license', action='store_true', help='已阅读并接受所选模型许可')
    args = parser.parse_args()
    from PySide6.QtCore import QLockFile
    args.data_dir.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str((args.data_dir / 'accounts.lock').resolve()))
    if not lock.tryLock(0):
        parser.exit(1, '数据目录正在被 FIF 口语客户端使用，请关闭该入口后再运行命令行安装。\n')
    cancel = Event()
    try:
        result = install_component(args.component, args.data_dir, cancel,
            lambda **values: print(values.get('message', ''), flush=True), accept_license=args.accept_license)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except KeyboardInterrupt:
        cancel.set()
        return 130
    except (ApiError, OSError, zipfile.BadZipFile) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        lock.unlock()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
