"""Local diagnosis; online synthesis is opt-in and never submits a FIF Oral task."""
import argparse
import importlib.metadata
import importlib.util
import json
from pathlib import Path
from threading import Event

from fif_oral.environment.manager import default_ffmpeg, install_component, run_command
from fif_oral.audio.speech import MODEL_FILES, SpeechSettings, save_json
from fif_oral.paths import default_data_dir


def main():
    parser = argparse.ArgumentParser(description='FIF 口语环境诊断')
    parser.add_argument('--data-dir', type=Path, default=default_data_dir())
    parser.add_argument('--online', action='store_true', help='通过 Edge 合成固定测试句，不提交 FIF 口语')
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    from PySide6.QtCore import QLockFile
    lock = QLockFile(str((args.data_dir / 'accounts.lock').resolve()))
    if not lock.tryLock(0):
        parser.exit(1, '请关闭正在使用此数据目录的客户端后再运行命令行诊断。\n')
    report = {'backend': None, 'ok': False, 'uploads': 0, 'submissions': 0, 'checks': {}, 'errors': []}
    cancel = Event()
    quiet = lambda **values: None
    try:
        settings = SpeechSettings.load(args.data_dir)
        report['backend'] = settings.backend
        report['checks']['edge_package'] = importlib.util.find_spec('edge_tts') is not None
        if report['checks']['edge_package']:
            report['checks']['edge_version'] = importlib.metadata.version('edge-tts')
        ffmpeg = default_ffmpeg(args.data_dir)
        report['checks']['ffmpeg'] = bool(ffmpeg)
        if ffmpeg:
            output = run_command([ffmpeg, '-version'], cancel, quiet, timeout=20)
            report['checks']['ffmpeg_version'] = output.splitlines()[0]
        if settings.backend in MODEL_FILES:
            settings.validate()
            report['checks']['local_runtime'] = install_component('check', args.data_dir, cancel, quiet)
            if report['checks']['local_runtime'].get('runtime_error'):
                report['errors'].append(report['checks']['local_runtime']['runtime_error'])
        elif not report['checks']['edge_package']:
            report['errors'].append('缺少 Edge TTS 组件，请重新运行 setup.cmd。')
        if not ffmpeg:
            report['errors'].append('缺少 FFmpeg，请在环境与下载中准备。')
        if args.online:
            report['checks']['edge_online'] = install_component('edge_test', args.data_dir, cancel, quiet)
        report['ok'] = not report['errors']
    except Exception as error:
        report['errors'].append(str(error))
    finally:
        lock.unlock()
    path = args.data_dir / 'diagnostics' / 'doctor.json'
    save_json(path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
