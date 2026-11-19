# 来源与第三方组件

本项目源码采用根目录的 MIT 许可。第三方库、在线服务、模型及下载的二进制分别适用其自身条款，项目许可不替代这些条款。

- [1z2y3x4w5/fif_gui](https://github.com/1z2y3x4w5/fif_gui)：原项目与行为参考，MIT，Copyright (c) 2025 1z2y3x4w5。朗读文本处理、语音策略的历史行为参考了该项目。
- [Piggywu981/dsh-fif-oral](https://github.com/Piggywu981/dsh-fif-oral)：参考其轻量 Edge TTS、诊断与发布工作流。该项目声明 MIT；当前 Python 引擎和安装器按本项目接口实现，未导入其浏览器麦克风注入代码。
- [rany2/edge-tts](https://github.com/rany2/edge-tts)：通过安装 Python 包使用，适用其 LGPL 许可。语音由微软在线服务提供，使用 Edge 引擎时朗读文本会发送到该服务。可用性与服务条款由服务方决定。
- [Qt for Python](https://doc.qt.io/qtforpython-6/licenses.html)：PySide6 及 Qt 组件适用各自的 LGPL/GPL/商业许可和第三方声明。当前源码包不包含 Qt 二进制；制作 EXE 时需一并检查并提供适用声明。
- [FFmpeg](https://ffmpeg.org/legal.html)：自动安装选择 FFmpeg 下载页列出的 Gyan Windows 构建及其官方镜像；该 essentials 构建启用 GPL。安装时保留压缩包中的 LICENSE 和来源元数据。
- [Coqui TTS](https://github.com/idiap/coqui-ai-TTS)：仅可选本地引擎需要其独立 Python 环境。模型权重单独下载。
- YourTTS：Coqui 模型索引标为 **CC BY-NC-ND 4.0**，许可见 <https://creativecommons.org/licenses/by-nc-nd/4.0/>。
- XTTS v2：适用 **Coqui Public Model License**，包括非商业用途限制，许可见 <https://huggingface.co/coqui/XTTS-v2/blob/main/LICENSE.txt>。

自动下载前须确认所选模型许可。本项目源码的 MIT 许可不意味着外部模型及其输出也采用 MIT 或可任意商用。

## 原项目 MIT 声明

MIT License

Copyright (c) 2025 1z2y3x4w5

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
