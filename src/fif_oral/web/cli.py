"""Run the local WebUI: python -m fif_oral.web [--port 8765] [--data-dir ...]."""
import argparse
from pathlib import Path

import uvicorn
from PySide6.QtCore import QLockFile

from fif_oral.web.app import create_app
from fif_oral.paths import default_data_dir


def main():
    parser = argparse.ArgumentParser(description='FIF 口语本机 WebUI')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir', type=Path, default=default_data_dir())
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('端口需在 1024–65535 之间。')
    data = args.data_dir.resolve()
    data.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(data / 'accounts.lock'))
    if not lock.tryLock(0):
        parser.exit(1, '账号库正在被桌面客户端或另一个 WebUI 使用，请先关闭它。\n')
    try:
        print(f'FIF 口语 WebUI: http://127.0.0.1:{args.port}', flush=True)
        app = create_app(data, allowed_hosts={f'127.0.0.1:{args.port}', f'localhost:{args.port}'})
        uvicorn.run(app, host='127.0.0.1', port=args.port, workers=1, proxy_headers=False, access_log=False)
    finally:
        lock.unlock()


if __name__ == '__main__':
    main()
