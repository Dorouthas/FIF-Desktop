"""Build a source release from an allowlist. Never publishes or packages runtime data."""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path


BASE = Path(__file__).resolve().parents[1]
SAFE_SUFFIXES = {'.py', '.md', '.txt', '.js', '.css', '.html', '.svg', '.json', '.ps1', '.cmd', '.yml', '.yaml'}
FORBIDDEN = {'data', '.venv', '.git', '.idea', '__pycache__', 'node_modules', 'profile', 'profiles'}
ROOT_FILES = ('README.md', 'LICENSE', 'THIRD_PARTY.md', 'CHANGELOG.md', '.gitignore',
              '.gitattributes', '.editorconfig', 'pyproject.toml', 'uv.lock', 'MANIFEST.in', 'requirements.txt', 'requirements-web.txt',
              'bootstrap.ps1', 'setup.cmd', 'start_webui.cmd', 'diagnose.cmd')


def release_files(root):
    root = Path(root).resolve()
    files = []
    for name in ROOT_FILES:
        path = root / name
        if not path.is_file():
            raise ValueError(f'缺少发布文件：{name}')
        files.append(path)
    for directory in ('src/fif_oral', 'scripts', 'tests', 'docs', '.github'):
        files.extend(p for p in (root / directory).rglob('*') if p.is_file() and p.suffix in SAFE_SUFFIXES)
    selected = []
    secrets = re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}|AKIA[A-Z0-9]{16}')
    private_path = re.compile(r'(?i)\b[A-Z]:[/\\](?:Users|Project)[/\\][^\s\"\']+')
    for path in sorted(set(files)):
        relative = path.relative_to(root)
        if FORBIDDEN.intersection(relative.parts) or any(part.endswith('.egg-info') for part in relative.parts):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f'发布目录包含外部链接：{relative}')
        content = path.read_text(encoding='utf-8-sig')
        if secrets.search(content):
            raise ValueError(f'发布文件中存在疑似凭据：{relative}')
        if private_path.search(content):
            raise ValueError(f'发布文件中存在本机个人路径：{relative}')
        selected.append(path)
    return selected


def main():
    parser = argparse.ArgumentParser(description='FIF 口语本地源码发布包')
    parser.add_argument('--version', required=True)
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--test', action='store_true', help='打包前运行单元测试与 JavaScript 语法检查')
    args = parser.parse_args()
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?', args.version):
        parser.error('版本号格式应为 1.0.0 或 1.0.0-beta.1。')
    if args.test:
        subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q'], cwd=BASE, check=True)
        subprocess.run(['node', '--check', 'src/fif_oral/web/static/app.js'], cwd=BASE, check=True)
    paths = release_files(BASE)
    summary = {'version': args.version, 'files': len(paths), 'includes_runtime_data': False, 'published': False}
    if not args.check_only:
        output = BASE / 'data' / 'releases'
        output.mkdir(parents=True, exist_ok=True)
        archive = output / f'fif-oral-{args.version}-source.zip'
        if archive.exists():
            parser.error('同版本发布包已存在，请使用新版本号。')
        temporary = archive.with_suffix('.zip.partial')
        try:
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as package:
                for path in paths:
                    package.write(path, f'fif-oral-{args.version}/' + path.relative_to(BASE).as_posix())
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        archive.with_suffix('.zip.sha256').write_text(digest + '  ' + archive.name + '\n', encoding='ascii')
        summary.update(archive=str(archive), sha256=digest)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
