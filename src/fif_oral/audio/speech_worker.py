"""Local Coqui worker. Its stdout is reserved for the JSON-lines protocol."""
import contextlib
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path


def load_engine(settings):
    # Models are explicitly selected local checkpoints; never invoke a downloader.
    os.environ["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import torch
    from TTS.utils.synthesizer import Synthesizer

    device = settings["device"]
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("当前 TTS 环境无法使用 CUDA，请选择 CPU 或自动。")
    folder = Path(settings["model_dir"])
    if settings["backend"] == "your_tts":
        config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
        # Downloaded configurations may contain paths from a different computer.
        for target in (config, config.get("model_args", {})):
            if "d_vector_file" in target:
                target["d_vector_file"] = [str(folder / "speakers.json")]
            for key, filename in (("speaker_encoder_model_path", "model_se.pth"),
                                  ("speaker_encoder_config_path", "config_se.json"),
                                  ("language_ids_file", "language_ids.json")):
                if key in target and (folder / filename).is_file():
                    target[key] = str(folder / filename)
        with tempfile.TemporaryDirectory(prefix="fif-speech-config-") as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            engine = Synthesizer(tts_checkpoint=str(folder / "model_file.pth"),
                                 tts_config_path=str(path), use_cuda=device == "cuda")
    else:
        # Coqui's XTTS loader uses torchaudio.load, which requires TorchCodec DLLs
        # with recent PyTorch. Our references are local PCM WAVs: decode them
        # directly and retain XTTS's mono/resampling behavior.
        import soundfile
        import torchaudio
        from TTS.tts.models import xtts

        def load_reference(path, sample_rate):
            samples, original_rate = soundfile.read(path, dtype="float32", always_2d=True)
            audio = torch.from_numpy(samples.T.copy()).mean(dim=0, keepdim=True)
            if original_rate != sample_rate:
                audio = torchaudio.functional.resample(audio, original_rate, sample_rate)
            return audio.clamp_(-1, 1)

        xtts.load_audio = load_reference
        engine = Synthesizer(model_dir=str(folder), use_cuda=device == "cuda")
    return engine, device


def main():
    protocol = sys.stdout

    def reply(value):
        protocol.write(json.dumps(value, ensure_ascii=True) + "\n")
        protocol.flush()

    with contextlib.redirect_stdout(sys.stderr):
        try:
            settings = json.loads(sys.stdin.readline())
            engine, device = load_engine(settings)
            import soundfile
            reply({"event": "ready", "device": device})
            for line in sys.stdin:
                job = json.loads(line)
                if job.get("cmd") == "close":
                    break
                import random
                import numpy
                import torch
                seed = int(job.get("seed", 0))
                random.seed(seed)
                numpy.random.seed(seed % (2 ** 32))
                torch.manual_seed(seed)
                speed = float(job.get("speed", 1.0))
                if not 0.6 <= speed <= 1.4:
                    raise ValueError("Speech speed is outside the supported range")
                model = engine.tts_model
                previous = getattr(model, "length_scale", None)
                try:
                    if previous is not None:
                        model.length_scale = previous / speed
                    options = {"speed": speed} if settings["backend"] == "xtts_v2" else {}
                    wav = engine.tts(text=job["text"], speaker_name=None, language_name="en", speaker_wav=settings["voice"],
                                     split_sentences=True, **options)
                finally:
                    if previous is not None:
                        model.length_scale = previous
                soundfile.write(job["path"], wav, engine.output_sample_rate, subtype="PCM_16")
                reply({"event": "done", "path": job["path"]})
        except Exception as error:
            traceback.print_exc(file=sys.stderr)
            reply({"event": "error", "error": f"{type(error).__name__}: {error}"})
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
