import hashlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Thread

from fif_oral.audio.assessment import Cancelled, check_cancel
from fif_oral.challenge import file_hash, run_challenge
from fif_oral.api.client import ApiError
from fif_oral.audio.text import SINGLE_WORD, VERSION, normalize
from fif_oral.audio.system import SystemSpeech
from fif_oral.audio.edge import EdgeSpeech


MODEL_FILES = {
    "your_tts": ("config.json", "model_file.pth", "model_se.pth", "config_se.json", "speakers.json", "language_ids.json"),
    "xtts_v2": ("config.json", "model.pth", "vocab.json", "speakers_xtts.pth"),
}
MODEL_NAMES = {"your_tts": "YourTTS", "xtts_v2": "XTTS v2"}
BACKEND_NAMES = {'edge': 'Edge TTS（联网）', **MODEL_NAMES}


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        except BaseException:
            file.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class SpeechSettings:
    python: str = ""
    backend: str = "edge"
    device: str = "auto"
    your_tts: str = ""
    xtts_v2: str = ""
    word_mode: str = "model"
    adaptive: str = "on"
    target_score: str = "80"
    max_trials: str = "3"
    fallback_model: str = "off"
    context_mode: str = "on"
    edge_voice: str = 'en-US-AriaNeural'
    edge_rate: str = '+0%'

    @property
    def model_dir(self):
        return Path(getattr(self, self.backend))

    @classmethod
    def load(cls, data_dir):
        path = data_dir / "speech.json"
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                data.setdefault('backend', 'your_tts')
                data.setdefault('word_mode', 'model' if data['backend'] == 'edge' else 'system')
                settings = cls(**data)
                if settings.backend not in BACKEND_NAMES or settings.device not in {"auto", "cpu", "cuda"}:
                    raise ValueError()
                if any(not isinstance(value, str) for value in asdict(settings).values()):
                    raise ValueError()
                settings.validate(neural=False)
                return settings
            except (ValueError, TypeError, ApiError) as error:
                raise ApiError("语音模型配置无法读取，请检查 data/speech.json。") from error
        cache = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "tts"
        directories = {name: cache / f"tts_models--multilingual--multi-dataset--{name}" for name in MODEL_FILES}
        return cls(python="" if getattr(sys, "frozen", False) else sys.executable,
                   **{name: str(path) if path.is_dir() else "" for name, path in directories.items()})

    def save(self, data_dir):
        save_json(data_dir / "speech.json", asdict(self))

    def validate(self, *, neural=True):
        if self.backend not in BACKEND_NAMES or self.device not in {"auto", "cpu", "cuda"}:
            raise ApiError("语音模型或运行设备无效。")
        if self.word_mode not in {"system", "model"} or self.adaptive not in {"on", "off"}:
            raise ApiError("单词语音或动态调整策略无效。")
        if self.fallback_model not in {"off", *MODEL_FILES}:
            raise ApiError("备用模型无效。")
        if self.context_mode not in {'on', 'off'}:
            raise ApiError("上下文候选策略无效。")
        from fif_oral.audio.edge import validate_voice
        validate_voice(self.edge_voice, self.edge_rate)
        try:
            if not 0 <= int(self.target_score) <= 100 or not 1 <= int(self.max_trials) <= 12:
                raise ValueError()
        except (ValueError, TypeError) as error:
            raise ApiError("目标分数需为 0–100，每题评测预算需为 1–12。") from error
        if not neural:
            return
        if self.backend == 'edge':
            from fif_oral.audio.edge import available
            if not available():
                raise ApiError('请通过「环境与下载」准备 Edge TTS 组件。')
            return
        if not self.python or not Path(self.python).is_file():
            raise ApiError("请在「模型设置」选择安装了 coqui-tts 的 Python 解释器。")
        if not getattr(self, self.backend):
            raise ApiError("请在「模型设置」选择已下载的模型目录。")
        missing = [name for name in MODEL_FILES[self.backend] if not (self.model_dir / name).is_file()]
        if missing:
            raise ApiError("模型目录缺少：" + "、".join(missing))

    def validate_for(self, items):
        self.validate(neural=any(not uses_system_voice(item, self) for item in items))
        if self.fallback_model != "off":
            from dataclasses import replace
            replace(self, backend=self.fallback_model).validate()


class SpeechEngine:
    def __init__(self, settings, voice, log_path, cancel):
        self.process = None
        self.log = None
        self.reader = None
        self.messages = queue.Queue()
        self.log_path = log_path
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log = log_path.open("w", encoding="utf-8")
            environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
            self.process = subprocess.Popen(
                [settings.python, "-u", str(Path(__file__).with_name("speech_worker.py"))],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                text=True, encoding="utf-8", env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

            def read():
                for line in self.process.stdout:
                    self.messages.put(line)
                self.messages.put(None)

            self.reader = Thread(target=read, name="fif-speech-output", daemon=True)
            self.reader.start()
            self.send({"backend": settings.backend, "model_dir": str(settings.model_dir.resolve()),
                       "device": settings.device, "voice": str(voice.resolve())})
            ready = self.receive(cancel)
            if ready.get("event") != "ready":
                raise ApiError("语音模型初始化响应异常。")
            self.device = ready["device"]
        except BaseException:
            self.close()
            raise

    def send(self, data):
        try:
            self.process.stdin.write(json.dumps(data, ensure_ascii=True) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as error:
            raise ApiError(f"语音进程已退出，详情见 {self.log_path}。") from error

    def receive(self, cancel):
        deadline = time.monotonic() + 600
        while True:
            check_cancel(cancel)
            if time.monotonic() > deadline:
                raise ApiError(f"语音模型等待超时，详情见 {self.log_path}。")
            try:
                line = self.messages.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                raise ApiError(f"语音进程提前退出，详情见 {self.log_path}。")
            try:
                message = json.loads(line)
            except ValueError as error:
                raise ApiError("语音进程返回了无效响应。") from error
            if not isinstance(message, dict):
                raise ApiError("语音进程返回了无效响应。")
            if message.get("event") == "error":
                raise ApiError(f"生成音频失败：{str(message.get('error', ''))[:200]}\n详情见 {self.log_path}")
            return message

    def synthesize(self, text, target, cancel, *, speed=1.0, seed=0):
        self.send({"text": text, "path": str(target.resolve()), "speed": speed, "seed": seed})
        message = self.receive(cancel)
        if message.get("event") != "done" or message.get("path") != str(target.resolve()):
            raise ApiError("语音合成响应与当前题目不一致。")
        return text

    def close(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            if self.reader:
                self.reader.join(timeout=2)
            if self.process.stdin:
                self.process.stdin.close()
            if self.process.stdout:
                self.process.stdout.close()
        if self.log:
            self.log.close()


def valid_wav(path):
    try:
        with wave.open(str(path), "rb") as file:
            return file.getsampwidth() == 2 and file.getnframes() > 0
    except (OSError, EOFError, wave.Error):
        return False


def uses_system_voice(item, settings):
    return settings.word_mode == "system" and bool(SINGLE_WORD.fullmatch(item.prompt.text.strip()))


def needs_reference_voice(items, settings):
    return bool(items) and (settings.fallback_model != 'off' or
        (settings.backend in MODEL_FILES and any(not uses_system_voice(item, settings) for item in items)))


def synthesis_text(item, settings=None):
    text = item.prompt.text.strip()
    if settings is not None and uses_system_voice(item, settings):
        return text
    text = normalize(text)
    if item.category == "read_word" and SINGLE_WORD.fullmatch(text) and text[-1] not in ".!?":
        # A sentence boundary gives neural TTS an ending prosody for isolated words.
        # The assessment reference remains the original word.
        return text + "."
    return text


def generate_audio(settings, voice, items, data_dir, account_id, cancel, progress, *, failures=None, ffmpeg=None):
    settings.validate_for(items)
    uses_backend = any(not uses_system_voice(item, settings) for item in items)
    needs_neural = settings.backend in MODEL_FILES and uses_backend
    if needs_neural and (voice is None or not valid_wav(voice)):
        raise ApiError("账号的参考音色无法读取，请重新导入有效的 PCM WAV。")
    progress("正在核对模型和参考音色。")
    check_cancel(cancel)
    system = SystemSpeech(cancel) if any(uses_system_voice(item, settings) for item in items) else None
    edge = None
    if settings.backend == 'edge' and uses_backend:
        from fif_oral.environment.manager import default_ffmpeg
        edge = EdgeSpeech(settings, ffmpeg or default_ffmpeg(data_dir))
    # Include all model contents: moving or changing a model/voice cannot reuse stale audio.
    signatures = {}
    for name in MODEL_FILES[settings.backend] if needs_neural else ():
        check_cancel(cancel)
        signatures[name] = file_hash(settings.model_dir / name)
    identity = {"version": 2, "backend": settings.backend if uses_backend else "system",
                "device": settings.device if needs_neural else "cpu", "models": signatures,
                "voice": file_hash(voice) if needs_neural else None, "text_policy": VERSION,
                "word_mode": settings.word_mode, "system_voice": system.voice_id if system else None,
                "worker": file_hash(Path(__file__).with_name("speech_worker.py")) if needs_neural else None}
    if edge:
        identity['edge'] = {'voice': settings.edge_voice, 'rate': settings.edge_rate, 'version': edge.version,
                            'code': file_hash(Path(__file__).with_name('edge.py'))}
    signature = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    account = hashlib.sha256(account_id.encode()).hexdigest()[:24]
    folder = data_dir / "generated" / account / signature
    folder.mkdir(parents=True, exist_ok=True)
    engine = None
    files = []
    try:
        for index, item in enumerate(items, 1):
            check_cancel(cancel)
            text = synthesis_text(item, settings)
            key = hashlib.sha256(json.dumps([item.question_id, text], ensure_ascii=False).encode()).hexdigest()
            target = folder / f"{key}.wav"
            metadata = target.with_suffix(".json")
            cached = False
            if valid_wav(target) and metadata.is_file():
                try:
                    cached = json.loads(metadata.read_text(encoding="utf-8")).get("sha256") == file_hash(target)
                except (ValueError, ApiError):
                    pass
            if cached:
                progress(f"已恢复第 {index}/{len(items)} 题生成的音频。")
            else:
                temporary = target.with_suffix(".pending.wav")
                try:
                    system_word = uses_system_voice(item, settings)
                    if not system_word and edge is None and engine is None:
                        progress(f"正在加载 {MODEL_NAMES[settings.backend]}，首次加载可能需要一些时间。")
                        engine = SpeechEngine(settings, voice, data_dir / "speech-worker.log", cancel)
                    active = system if system_word else edge or engine
                    title = "Windows 美式英语（慢速，非账号音色）" if system_word else (f'Edge TTS · {settings.edge_voice}（联网）' if edge else f"{MODEL_NAMES[settings.backend]}（{engine.device}）")
                    progress(f"正在用 {title} 生成第 {index}/{len(items)} 题音频。")
                    active.synthesize(text, temporary, cancel)
                    check_cancel(cancel)
                    if not valid_wav(temporary):
                        raise ApiError("模型没有生成有效的 WAV 音频。")
                    temporary.replace(target)
                    save_json(metadata, {"sha256": file_hash(target), "identity": identity,
                                         "synthesis_text": text, "reference_text": item.prompt.text,
                                         "engine": "system" if system_word else settings.backend})
                except Cancelled:
                    raise
                except ApiError as error:
                    if failures is None:
                        raise
                    failures[item.question_id] = str(error)
                    progress(f"第 {index}/{len(items)} 题基础合成失败，将交给候选生成处理。")
                    if engine:
                        engine.close()
                        engine = None
                finally:
                    temporary.unlink(missing_ok=True)
            files.append(target)
    finally:
        if engine:
            engine.close()
    return files


def run_automatic_challenge(client, store, items, settings, voice, data_dir, account_id, ffmpeg, cancel, progress):
    from fif_oral.audio.candidates import CandidateGenerator, CandidatePolicy
    source = {"mode": "speech", "backend": settings.backend, "strategy": asdict(settings)}
    remaining = [item for item in items if item.question_id not in store.data.get('completed', {})
                 and item.question_id not in store.data.get('prepared', {})]
    if store.data:
        # A started attempt always resumes its original files, even if settings changed.
        files = [Path(path) for path in store.data["files"]]
        saved = (store.data.get("source") or {}).get("strategy")
        if saved:
            settings = SpeechSettings(**saved)
        source = store.data.get("source") or source
        if remaining and needs_reference_voice(remaining, settings) and voice is None and source.get("voice"):
            voice = Path(source["voice"])
            if file_hash(voice) != source.get("voice_sha256"):
                raise ApiError("本轮参考音色被修改，请为待复核题重新选择音色。")
    else:
        failures = {}
        files = generate_audio(settings, voice, items, data_dir, account_id, cancel, progress, failures=failures, ffmpeg=ffmpeg)
        if failures:
            source['generation_failed'] = failures
    if remaining and not needs_reference_voice(remaining, settings):
        voice = None
        source = {key: value for key, value in source.items() if key not in {'voice', 'voice_sha256'}}
    if voice is not None:
        source = {**source, "voice": str(voice.resolve()), "voice_sha256": file_hash(voice)}
    if "strategy" not in source:
        source = {**source, "strategy": asdict(settings)}
    if store.data and source != store.data.get("source"):
        store.data["source"] = source
        store.save()
    policy = None
    generator = None
    if remaining and settings.adaptive == "on" and store.data.get("stage") not in {"submitting", "submitted", "confirmed"}:
        generator = CandidateGenerator(settings, voice, data_dir, remaining, cancel)
        policy = CandidatePolicy(data_dir / "candidates", store.context, ffmpeg, generator,
                                  target=int(settings.target_score), trials=int(settings.max_trials))
    try:
        return run_challenge(client, store, items, files, ffmpeg, cancel, progress,
                             source=source, audio_policy=policy)
    finally:
        if generator:
            generator.close()
