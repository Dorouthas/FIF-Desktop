"""Per-recording feedback profiles, adapted from fif_gui's Speaker/assessment."""
import hashlib
import json
import os
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

from fif_oral.audio.assessment import check_cancel
from fif_oral.api.client import ApiError


FILTERS = {"slower": "atempo=0.88", "faster": "atempo=1.10", "quieter": "volume=0.70"}
NAMES = {"base": "原音频", "padding": "补充静音", "slower": "放慢语速", "faster": "加快语速", "quieter": "降低音量"}


def select_profile(history, available, target_score):
    successful, tried = {}, set()
    for profile, score, error in history:
        if profile in available:
            tried.add(profile)
            if error == 0 and score is not None:
                successful.setdefault(profile, []).append(score)
    averages = {profile: sum(scores) / len(scores) for profile, scores in successful.items()}
    best = max(averages, key=averages.get, default="base")
    if averages.get(best, 0) >= target_score / 20:
        return best
    if "base" not in tried:
        return "base"
    last_error = history[-1][2] if history else 0
    if last_error in {28673, 28689} and "padding" in available and "padding" not in tried:
        return "padding"
    if last_error == 28690 and "quieter" in available and "quieter" not in tried:
        return "quieter"
    return next((profile for profile in ("slower", "faster") if profile in available and profile not in tried), best)


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


@dataclass(frozen=True)
class Recording:
    key: str
    profile: str
    path: Path
    label: str = ""
    cached: dict | None = None


class AdaptiveAudio:
    def __init__(self, root, context, ffmpeg, target=80, trials=3):
        identity = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        self.folder = root / identity
        self.folder.mkdir(parents=True, exist_ok=True)
        self.history_path = self.folder / "history.json"
        self.history = json.loads(self.history_path.read_text(encoding="utf-8")) if self.history_path.exists() else {}
        if not isinstance(self.history, dict):
            raise ApiError("动态语音历史文件格式异常。")
        self.ffmpeg = ffmpeg
        self.target = target
        self.trials = trials

    def _key(self, item, source):
        value = [item.question_id, item.category, item.reference, digest(source), "profiles-v1"]
        return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()

    def prepare(self, item, source, tried, cancel):
        check_cancel(cancel)
        key = self._key(item, source)
        available = {"base", "padding", *FILTERS}
        if available <= tried:
            return None
        history = self.history.get(key, [])
        profile = select_profile(history, available, self.target)
        if profile in tried:
            error = history[-1][2] if history else 0
            priority = ["padding"] if error in {28673, 28689} else ["quieter"] if error == 28690 else []
            profile = next(p for p in [*priority, "base", "slower", "faster", "padding", "quieter"] if p not in tried)
        path = source
        if profile != "base":
            path = self.folder / f"{key}-{profile}.wav"
            self._variant(source, path, profile, cancel)
        return Recording(key, profile, path)

    def observe(self, recording, details, score):
        error = int(details.get("except_info") or 0)
        if details.get("rejected") and error == 0:
            error = -1
        rows = self.history.setdefault(recording.key, [])
        rows.append([recording.profile, score / 20, error])
        self.history[recording.key] = rows[-100:]
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.folder, suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            try:
                json.dump(self.history, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            except BaseException:
                file.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            temporary.replace(self.history_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _variant(self, source, path, profile, cancel):
        # Always rebuild from the base WAV; never stack adjustments.
        temporary = path.with_suffix(".pending.wav")
        try:
            if profile == "padding":
                with wave.open(str(source), "rb") as original, wave.open(str(temporary), "wb") as output:
                    output.setparams(original.getparams())
                    silence = b"\x80" if original.getsampwidth() == 1 else b"\0"
                    padding = silence * (original.getframerate() // 4 * original.getnchannels() * original.getsampwidth())
                    output.writeframes(padding)
                    output.writeframes(original.readframes(original.getnframes()))
                    output.writeframes(padding)
            else:
                process = subprocess.Popen(
                    [self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                     "-af", FILTERS[profile], "-c:a", "pcm_s16le", str(temporary)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                try:
                    for _ in range(300):
                        check_cancel(cancel)
                        try:
                            process.wait(timeout=0.1)
                            break
                        except subprocess.TimeoutExpired:
                            continue
                    else:
                        raise ApiError("动态音频调整超时。")
                    if process.returncode:
                        raise ApiError("动态音频调整失败，请检查 FFmpeg。")
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
            check_cancel(cancel)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
