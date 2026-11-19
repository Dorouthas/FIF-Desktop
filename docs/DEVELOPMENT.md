# 开发与项目结构

需要 Python 3.11–3.13。Windows x64 为当前主要支持平台。

```powershell
uv sync --frozen --extra web --extra dev
uv run --no-sync python -m unittest discover -s tests -v
node --check src/fif_oral/web/static/app.js
uv run --no-sync python -m build
```

没有 uv 时，可在虚拟环境中执行 `python -m pip install -e ".[web,dev]"`。依赖修改后使用 `uv lock` 更新锁文件，并一并提交 `pyproject.toml` 和 `uv.lock`。

```text
src/fif_oral/
├── api/            登录与平台接口
├── audio/          合成、录音检查、评测与候选策略
├── environment/    环境下载、安装及诊断
├── ui/             PySide6 桌面界面
├── web/            FastAPI、本机 WebUI 和静态资源
├── resources/      下载清单、可选 TTS 依赖及界面图标
├── automation.py   可恢复任务队列
├── challenge.py    单关挑战、上传与结果核对
├── storage.py      加密账号库和参考音色管理
└── paths.py        只读资源与可写数据目录
```

## 启动入口

| 命令 | 用途 |
| --- | --- |
| `fif-oral` / `python -m fif_oral` | 桌面客户端 |
| `fif-oral-web` / `python -m fif_oral.web` | 本机 WebUI |
| `fif-oral-doctor` | 本地诊断，`--online` 可测试固定句联网合成 |
| `fif-oral-env` | 环境检查与按需安装 |

源码或可编辑安装继续使用项目根目录的 `data/`。wheel 安装在 Windows 上默认使用 `%LOCALAPPDATA%/FIF-Oral/data/`，其他平台使用用户数据目录。可通过 `FIF_ORAL_DATA_DIR` 或入口的 `--data-dir` 指定独立目录。不要将可写数据放入已安装的包目录。

## 测试与反馈

`tests/` 使用临时账号库和模拟服务验证业务流程，不需要真实账号或平台令牌。音频转换与部分界面流程测试需要 FFmpeg；没有该组件时会跳过对应测试。发布验收应安装 FFmpeg 后运行完整测试。

`python tests/web_preview.py` 可启动隔离的界面演示，所有平台操作均模拟。反馈时提供版本、题型、脱敏错误和复现步骤；不要提交个人身份、令牌、带鉴权地址、原始录音或整个 `data/`。

维护者打包流程见 [scripts/README.md](../scripts/README.md)。
