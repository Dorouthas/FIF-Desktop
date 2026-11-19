"""The old client's explicit, slow US-English single-word route."""
import json
import subprocess

from fif_oral.audio.assessment import check_cancel
from fif_oral.api.client import ApiError


SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$settings = [Console]::In.ReadToEnd() | ConvertFrom-Json
$speech = New-Object -ComObject SAPI.SpVoice
$voices = $speech.GetVoices('Language=409')
if (-not $settings.path) {
    if ($voices.Count) { $voices.Item(0).Id }
    return
}
$selected = $null
for ($index = 0; $index -lt $voices.Count; $index++) {
    $candidate = $voices.Item($index)
    if ($candidate.Id -eq $settings.voice) { $selected = $candidate; break }
}
if (-not $selected) { throw 'Selected US English voice is unavailable.' }
$speech.Voice = $selected
$speech.Rate = if ($null -ne $settings.rate) { [int]$settings.rate } else { -5 }
$word = [System.Security.SecurityElement]::Escape([string]$settings.text)
$xml = '<silence msec="200"/>' + $word + '<silence msec="350"/>'
$stream = New-Object -ComObject SAPI.SpFileStream
$stream.Open([string]$settings.path, 3)
try {
    $speech.AudioOutputStream = $stream
    [void]$speech.Speak($xml, 136)
} finally { $stream.Close() }
'''


def run(settings, cancel):
    check_cancel(cancel)
    process = None
    try:
        process = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", SCRIPT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        request = json.dumps(settings)
        # communicate retains its input buffer across timeout retries.
        for turn in range(300):
            check_cancel(cancel)
            try:
                output, error = process.communicate(input=request if turn == 0 else None, timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
        else:
            raise ApiError("Windows 单词合成超时。")
        if process.returncode:
            raise ApiError("Windows 英语音色无法使用，请安装美式英语语音包，或在模型设置中改用账号音色。")
        return output.strip()
    except OSError as error:
        raise ApiError("无法启动 Windows 语音服务，请在模型设置中改用账号音色。") from error
    finally:
        if process:
            if process.poll() is None:
                process.kill()
            process.communicate()


class SystemSpeech:
    def __init__(self, cancel):
        self.voice_id = run({}, cancel)
        if not self.voice_id:
            raise ApiError("未安装美式英语音色，请安装 Windows 英语语音包，或在模型设置中改用账号音色。")

    def synthesize(self, text, target, cancel, *, rate=-5, voice_id=None):
        if not -10 <= rate <= 10:
            raise ApiError("Windows 语速需在 -10 到 10 之间。")
        run({"text": text, "path": str(target.resolve()), "voice": voice_id or self.voice_id, "rate": rate}, cancel)
        return text
