"""Microsoft Edge online synthesis; separate from local voice-cloning engines."""
import asyncio
import importlib.metadata
import importlib.util
import re
import tempfile
from pathlib import Path

from fif_oral.audio.assessment import Cancelled, check_cancel, convert_audio
from fif_oral.api.client import ApiError


def available():
    return importlib.util.find_spec('edge_tts') is not None


def validate_voice(voice, rate):
    if not re.fullmatch(r'en-[A-Za-z]{2,5}-[A-Za-z0-9]+Neural', voice):
        raise ApiError('请选择有效的英文 Edge TTS 音色名称。')
    if not re.fullmatch(r'[+-]\d{1,3}%', rate) or not -50 <= int(rate[:-1]) <= 100:
        raise ApiError('Edge TTS 语速需为 -50% 到 +100%，并包含正负号。')


async def cancellable(coroutine, cancel, timeout=120):
    task = asyncio.create_task(coroutine)
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while not task.done():
            check_cancel(cancel)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ApiError('Edge TTS 等待超时，请检查网络或切换本地引擎。')
            await asyncio.wait({task}, timeout=min(.1, remaining))
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class EdgeSpeech:
    device = '联网服务'

    def __init__(self, settings, ffmpeg):
        validate_voice(settings.edge_voice, settings.edge_rate)
        if not available():
            raise ApiError('尚未安装 Edge TTS 组件，请通过「环境与下载」准备或重新运行启动脚本。')
        if not ffmpeg or not Path(ffmpeg).is_file():
            raise ApiError('Edge TTS 音频转换需要 FFmpeg，请通过「环境与下载」准备。')
        self.voice_id = settings.edge_voice
        self.rate = settings.edge_rate
        self.ffmpeg = ffmpeg
        self.version = importlib.metadata.version('edge-tts')

    def synthesize(self, text, target, cancel, *, rate=None):
        import edge_tts
        check_cancel(cancel)
        selected_rate = rate or self.rate
        validate_voice(self.voice_id, selected_rate)
        with tempfile.TemporaryDirectory(prefix='fif-edge-') as folder:
            mp3 = Path(folder) / 'speech.mp3'
            async def synthesize():
                communication = edge_tts.Communicate(text, self.voice_id, rate=selected_rate)
                await communication.save(str(mp3))
            try:
                asyncio.run(cancellable(synthesize(), cancel))
                check_cancel(cancel)
                if not mp3.is_file() or not mp3.stat().st_size:
                    raise ApiError('Edge TTS 没有返回有效音频。')
                convert_audio(self.ffmpeg, mp3, target)
                check_cancel(cancel)
            except (Cancelled, ApiError):
                raise
            except Exception as error:
                raise ApiError('Edge TTS 联网合成失败，请检查网络或切换本地引擎。') from error

    def close(self):
        pass


def english_voices(cancel):
    if not available():
        raise ApiError('请先安装 Edge TTS 组件。')
    import edge_tts
    try:
        rows = asyncio.run(cancellable(edge_tts.list_voices(), cancel, timeout=30))
    except (Cancelled, ApiError):
        raise
    except Exception as error:
        raise ApiError('暂时无法获取 Edge 音色列表，请检查网络。') from error
    return [{'name': row['ShortName'], 'locale': row['Locale'], 'gender': row['Gender']}
            for row in rows if row.get('Locale', '').startswith('en-')]
