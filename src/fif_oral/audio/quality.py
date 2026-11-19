"""Conservative checks; no trimming of low-energy consonants."""
import math
import sys
import wave
from array import array

from fif_oral.api.client import ApiError


def inspect_audio(path):
    try:
        with wave.open(str(path), "rb") as file:
            rate, channels, width, count = file.getframerate(), file.getnchannels(), file.getsampwidth(), file.getnframes()
            if width != 2 or rate <= 0 or channels < 1:
                raise ApiError("候选音频必须是有效的 16 位 PCM WAV。")
            if count > rate * 300:
                raise ApiError("候选音频超过 300 秒。")
            samples = array("h", file.readframes(count))
    except (OSError, EOFError, wave.Error) as error:
        raise ApiError("候选音频无法读取。") from error
    if sys.byteorder != "little":
        samples.byteswap()
    peak = max((abs(value) for value in samples), default=0)
    rms = math.sqrt(sum(value * value for value in samples) / max(1, len(samples)))
    duration = count / rate
    clipping = sum(abs(value) >= 32760 for value in samples) / max(1, len(samples))
    problem = "silence" if peak < 32 or duration < 0.1 else "clipping" if clipping > 0.005 else ""
    return {"duration": duration, "rate": rate, "channels": channels, "peak": peak / 32768,
            "rms": rms / 32768, "clipping_ratio": clipping, "problem": problem}
