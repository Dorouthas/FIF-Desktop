# 维护工具说明

以下命令均在项目根目录执行。客户端安装与使用见 [项目 README](../README.md)。

## 本地源码发布包

`package_release.py` 只生成本地 ZIP，不上传 GitHub。它按白名单包含源码、前端、安装/诊断脚本与许可，排除账号、音色、录音、模型、虚拟环境和运行数据。

先检查待发布文件：

```powershell
.\.venv\Scripts\python.exe scripts/package_release.py --version 1.0.0-beta.2 --check-only
```

运行测试与 JavaScript 语法检查，通过后生成源码包：

```powershell
.\.venv\Scripts\python.exe scripts/package_release.py --version 1.0.0-beta.2 --test
```

源码包命名为 `fif-oral-<版本号>-source.zip`，与 SHA-256 文件一同保存在 `data/releases/`。脚本拒绝覆盖已有同版本发布包，重新打包时请使用新的版本号。第三方来源与打包时需保留的声明见 [THIRD_PARTY.md](../THIRD_PARTY.md)。

## Python 发行包

```powershell
uv sync --frozen --extra web --extra dev
uv run --no-sync python -m build
```

`dist/` 中生成 wheel 和源码发行包。发布前应在独立环境安装 wheel，验证命令入口、包资源与数据目录，并核对 ZIP、wheel、源码发行包内均未包含账号、录音、模型、个人路径或凭据。构建输出与运行数据不提交到 Git；GitHub Release 附件可提供通过检查的发行包及 SHA-256 文件。
