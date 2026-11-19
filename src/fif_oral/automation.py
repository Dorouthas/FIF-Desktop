"""Account-bound, persistent serial queues for task / unit / level scopes."""
import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from fif_oral.audio.assessment import Cancelled, UnsupportedQuestion, challenge_items, challenge_kind, check_cancel
from fif_oral.challenge import AttemptStore, ChallengePending, file_hash
from fif_oral.api.client import ApiError, SessionExpired, plain, records
from fif_oral.audio.speech import SpeechSettings, run_automatic_challenge, save_json, needs_reference_voice


STATUS = {'pending': '等待', 'running': '执行中', 'confirmed': '成绩已确认', 'skipped': '已达目标，跳过',
          'review': '待复核', 'unsupported': '未支持', 'error': '执行失败', 'paused': '已暂停'}
RECOVER = {'submitting', 'submitted', 'confirmed'}


@dataclass(frozen=True)
class AutomationScope:
    kind: str
    task_id: str
    unit_id: str = ''
    level_id: str = ''
    title: str = ''

    def validate(self):
        if self.kind not in {'task', 'unit', 'level'} or not self.task_id:
            raise ApiError('自动任务范围无效。')
        if self.kind in {'unit', 'level'} and not self.unit_id:
            raise ApiError('自动任务缺少课程编号。')
        if self.kind == 'level' and not self.level_id:
            raise ApiError('自动任务缺少关卡编号。')


def job_key(job):
    return json.dumps([job['task_id'], job['unit_id'], job['level_id']], separators=(',', ':'))


def score_number(value):
    try:
        if isinstance(value, bool) or value in (None, ''):
            return None
        value = float(value)
        return value if math.isfinite(value) and 0 <= value <= 100 else None
    except (ValueError, TypeError):
        return None


def enumerate_levels(client, scope, cancel, progress, checkpoint=None):
    scope.validate()
    jobs, issues, seen = [], [], set()

    def add(unit_id, unit_name, row):
        level_id = str(row.get('levelId') or '')
        if not level_id:
            issues.append({'unit_id': unit_id, 'name': unit_name, 'reason': '平台目录中存在没有编号的关卡。'})
            return
        job = {'task_id': scope.task_id, 'unit_id': unit_id, 'unit_name': unit_name,
               'level_id': level_id, 'name': plain(row.get('levelName')) or '关卡',
               'remote_score': score_number(row.get('levelScore')), 'state': 'pending', 'message': ''}
        key = job_key(job)
        if key not in seen:
            seen.add(key)
            jobs.append(job)

    check_cancel(cancel)
    if scope.kind == 'level':
        add(scope.unit_id, '', {'levelId': scope.level_id, 'levelName': scope.title})
        return jobs, issues
    if scope.kind == 'unit':
        units = [{'unitid': scope.unit_id, 'unitName': scope.title}]
    else:
        progress('正在查询任务的完整课程目录。')
        units = records(client.task(scope.task_id).get('ttdList'))
    unit_seen = set()
    for unit in units:
        check_cancel(cancel)
        unit_id = str(unit.get('unitid') or unit.get('unitId') or unit.get('id') or '').split(',')[-1]
        name = plain(unit.get('unitName')) or '课程'
        if not unit_id:
            issues.append({'unit_id': '', 'name': name, 'reason': '平台没有返回课程编号。'})
            continue
        if unit_id in unit_seen:
            continue
        unit_seen.add(unit_id)
        progress(f'正在读取课程「{name}」的全部关卡。')
        try:
            data = client.unit(scope.task_id, unit_id)
        except SessionExpired:
            raise
        except ApiError as error:
            issues.append({'unit_id': unit_id, 'name': name, 'reason': str(error)})
        else:
            for row in records(data.get('levelList')):
                add(unit_id, name, row)
        if checkpoint:
            checkpoint(jobs, issues)
    return jobs, issues


class AutomationStore:
    def __init__(self, root, account_id, user_id, scope):
        scope.validate()
        self.scope = scope
        self.context = {'account_id': account_id, 'user_id': user_id,
                        'scope': {key: value for key, value in asdict(scope).items() if key != 'title'}}
        key = hashlib.sha256(json.dumps(self.context, sort_keys=True).encode()).hexdigest()
        self.path = root / key / 'queue.json'
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding='utf-8'))
                if (not isinstance(self.data, dict) or self.data.get('version') != 1 or
                        self.data.get('context') != self.context or not isinstance(self.data.get('jobs'), list)):
                    raise ValueError()
            except (ValueError, TypeError) as error:
                raise ApiError('自动任务队列文件无法读取。') from error
        else:
            self.data = {'version': 1, 'context': self.context, 'title': scope.title, 'created': time.time(),
                         'stage': 'ready', 'jobs': [], 'discovery_issues': [], 'skip_qualified': True}

    def save(self):
        self.data['updated'] = time.time()
        save_json(self.path, self.data)

    def configure(self, settings, voice, ffmpeg, skip_qualified):
        self.data.update(settings=asdict(settings), voice=str(voice.resolve()) if voice else None,
                         voice_sha256=file_hash(voice) if voice else None,
                         ffmpeg=ffmpeg, skip_qualified=bool(skip_qualified))
        self.save()

    def merge(self, discovered, issues, *, complete):
        previous = {job_key(job): job for job in self.data['jobs']}
        rows = []
        for job in discovered:
            old = previous.pop(job_key(job), None)
            if old:
                job = {**old, **{key: job[key] for key in ('name', 'unit_name', 'remote_score')}}
            rows.append(job)
        if not complete:
            rows.extend(previous.values())
        self.data.update(jobs=rows, discovery_issues=list(issues))
        self.save()


def queue_counts(store):
    counts = {name: 0 for name in STATUS}
    for job in store.data['jobs']:
        counts[job.get('state', 'pending')] = counts.get(job.get('state', 'pending'), 0) + 1
    counts['discovery_errors'] = len(store.data.get('discovery_issues', []))
    return counts


def run_queue(client, store, data_dir, cancel, progress):
    if client.user_id != store.context['user_id']:
        raise ApiError('当前登录身份与自动任务队列不一致。')
    active = None
    try:
        check_cancel(cancel)
        store.data['stage'] = 'discovering'
        store.save()
        jobs, issues = enumerate_levels(client, store.scope, cancel, progress,
            lambda jobs, issues: store.merge(jobs, issues, complete=False))
        store.merge(jobs, issues, complete=True)
        progress(f'目录查询完成，共 {len(jobs)} 个关卡，{len(issues)} 项目录问题。')
        for issue in issues:
            progress(f"目录问题 · {issue['name']}：{issue['reason']}")
        settings = SpeechSettings(**store.data['settings'])
        settings.validate(neural=False)
        profile = client.profile()
        language_ok = profile.get('language') in (2, '2')
        store.data['stage'] = 'running'
        store.save()
        for index, job in enumerate(store.data['jobs'], 1):
            check_cancel(cancel)
            active = job
            if job['state'] == 'confirmed':
                progress(f"第 {index}/{len(jobs)} 关「{job['name']}」已有确认记录，保留结果。")
                continue
            job.update(state='running', message='正在检查关卡。')
            job.pop('score', None)
            job.pop('report_id', None)
            store.save()
            prefix = f"[{index}/{len(jobs)} · {job['name']}] "
            emit = lambda message, prefix=prefix: progress(prefix + message)
            try:
                attempt = AttemptStore(data_dir / 'challenges', store.context['account_id'], client.user_id,
                                       job['task_id'], job['unit_id'], job['level_id'])
                job['attempt'] = str(attempt.path.resolve()) if attempt.path else None
                recovering = attempt.data.get('stage') in RECOVER
                remote = job.get('remote_score')
                if not recovering and store.data.get('skip_qualified') and remote is not None and remote > 0 and remote >= int(settings.target_score):
                    job.update(state='skipped', score=remote, message=f'平台分数 {remote:g}，已达优化目标。')
                    store.save()
                    emit(job['message'])
                    continue
                if recovering:
                    items = []
                else:
                    if not language_ok:
                        raise UnsupportedQuestion('当前自动评测仅支持英文关卡。')
                    level_data = client.level(job['level_id'])
                    job['kind'] = challenge_kind(level_data)
                    items = challenge_items(level_data)
                remaining = [item for item in items if item.question_id not in attempt.data.get('completed', {})
                             and item.question_id not in attempt.data.get('prepared', {})]
                voice = Path(store.data['voice']) if store.data.get('voice') else None
                needs_voice = needs_reference_voice(remaining, settings)
                if needs_voice and (voice is None or file_hash(voice) != store.data.get('voice_sha256')):
                    raise ApiError('该关卡需要账号参考 WAV，请选择音色后重试。')
                ffmpeg = store.data.get('ffmpeg', '')
                if not recovering and (not ffmpeg or not Path(ffmpeg).is_file()):
                    raise ApiError('请在队列窗口选择可用的 ffmpeg.exe。')
                if attempt.data and not recovering:
                    previous = attempt.data.get('source') or {}
                    updated = {**previous, 'mode': 'speech', 'backend': settings.backend, 'strategy': asdict(settings)}
                    if needs_voice:
                        updated.update(voice=str(voice.resolve()), voice_sha256=file_hash(voice))
                    else:
                        updated.pop('voice', None)
                        updated.pop('voice_sha256', None)
                    if previous != updated:
                        attempt.data.setdefault('strategy_revisions', []).append({'time': time.time(), 'source': previous})
                        attempt.data['source'] = updated
                        attempt.save()
                result = run_automatic_challenge(client, attempt, items, settings,
                    voice if needs_voice else None, data_dir, store.context['account_id'], ffmpeg, cancel, emit)
                job.update(state='confirmed', score=result['score'], report_id=result['report_id'],
                           attempt=str(attempt.path.resolve()) if attempt.path else None,
                           message='本次录音路径与平台报告已核对。')
            except (Cancelled, SessionExpired):
                raise
            except UnsupportedQuestion as error:
                job.update(state='unsupported', message=str(error))
            except ChallengePending as error:
                job.update(state='review', message=str(error))
            except ApiError as error:
                job.update(state='error', message=str(error))
            store.save()
            emit(f"{STATUS[job['state']]} · {job['message']}")
        counts = queue_counts(store)
        unfinished = any(job['state'] not in {'confirmed', 'skipped'} for job in store.data['jobs']) or bool(issues)
        store.data['stage'] = 'review' if unfinished else 'complete'
        store.save()
        progress(f"执行结束：确认 {counts['confirmed']}，达标跳过 {counts['skipped']}，待复核 {counts['review']}，"
                 f"未支持 {counts['unsupported']}，失败 {counts['error']}，目录问题 {counts['discovery_errors']}。")
        return counts
    except Cancelled:
        if active and active['state'] == 'running':
            active.update(state='paused', message='已暂停，保留单关进度。')
        store.data['stage'] = 'paused'
        store.save()
        raise
    except SessionExpired:
        if active and active['state'] == 'running':
            active.update(state='paused', message='会话已过期，登录后继续。')
        store.data['stage'] = 'needs_login'
        store.save()
        raise
    except Exception:
        if active and active['state'] == 'running':
            active.update(state='error', message='执行中断，保留单关进度，继续时将先检查提交状态。')
        store.data['stage'] = 'error'
        store.save()
        raise
