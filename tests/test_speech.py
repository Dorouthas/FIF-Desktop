import tempfile
import unittest
import wave
import math
import struct
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import patch

from fif_oral.audio.assessment import Cancelled, challenge_items
from fif_oral.challenge import AttemptStore, file_hash
from fif_oral.audio.speech import MODEL_FILES, SpeechSettings, generate_audio, run_automatic_challenge
from test_challenge import level, word_level


def write_wav(path, frequency=440):
    with wave.open(str(path), "wb") as file:
        file.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        file.writeframes(b"".join(struct.pack('<h', int(10000 * math.sin(i * 2 * math.pi * frequency / 16000)))
                                  for i in range(1600)))


def fixture_settings(root):
    import sys
    model = root / "model"
    model.mkdir()
    for name in MODEL_FILES["your_tts"]:
        (model / name).write_bytes(b"fixture model")
    return SpeechSettings(backend='your_tts', python=sys.executable, your_tts=str(model), word_mode="model", adaptive="off")


class FakeEngine:
    device = "cpu"
    requests = []
    closed = 0
    cancel_at = None

    def __init__(self, settings, voice, log_path, cancel):
        self.voice = voice

    def synthesize(self, text, target, cancel, **options):
        self.requests.append((text, self.voice))
        write_wav(target, 440 + options.get('seed', 0))
        if self.cancel_at == len(self.requests):
            cancel.set()

    def close(self):
        type(self).closed += 1


class SpeechTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = fixture_settings(self.root)
        self.voice = self.root / "voice.wav"
        write_wav(self.voice)
        self.cancel = Event()
        self.items = challenge_items(level(("First sentence", "Second sentence")))
        FakeEngine.requests = []
        FakeEngine.closed = 0
        FakeEngine.cancel_at = None
        patched = patch("fif_oral.audio.speech.SpeechEngine", FakeEngine)
        patched.start()
        self.addCleanup(patched.stop)

    def generate(self, account="account"):
        return generate_audio(self.settings, self.voice, self.items, self.root, account, self.cancel, lambda m: None)

    def test_cache_separates_accounts_models_voice_and_corrupted_audio(self):
        files = self.generate()
        self.assertEqual(len(FakeEngine.requests), 2)
        self.assertEqual(self.generate(), files)
        self.assertEqual(len(FakeEngine.requests), 2)
        files[0].write_bytes(files[0].read_bytes() + b"tampered")
        self.generate()
        self.assertEqual(len(FakeEngine.requests), 3)
        self.assertNotEqual(self.generate("another"), files)
        self.assertEqual(len(FakeEngine.requests), 5)
        self.voice.write_bytes(self.voice.read_bytes() + b"changed")
        changed_voice = self.generate()
        self.assertNotEqual(changed_voice, files)
        (self.settings.model_dir / "model_file.pth").write_bytes(b"new model")
        self.assertNotEqual(self.generate(), changed_voice)
        self.assertTrue(all(voice == self.voice for text, voice in FakeEngine.requests))

    def test_cancel_keeps_only_completed_wavs_then_resumes(self):
        FakeEngine.cancel_at = 2
        with self.assertRaises(Cancelled):
            self.generate()
        self.assertEqual(len(list((self.root / "generated").rglob("*.wav"))), 1)
        self.assertFalse(list(self.root.rglob("*.pending.wav")))
        self.assertEqual(FakeEngine.closed, 1)
        self.cancel.clear()
        FakeEngine.cancel_at = None
        self.generate()
        self.assertEqual(len(FakeEngine.requests), 3)

    def test_resume_uses_attempt_files_without_loading_model(self):
        store = AttemptStore(self.root / "challenges", "account", "student", "task", "unit", "level")
        files = self.generate()
        store.start(self.items, files, [file_hash(path) for path in files], {"mode": "speech", "backend": "your_tts"})
        with patch("fif_oral.audio.speech.generate_audio", side_effect=AssertionError("must not regenerate")), \
                patch("fif_oral.audio.speech.run_challenge", return_value={"score": 72}) as run:
            result = run_automatic_challenge(None, store, self.items, replace(self.settings, python=""), None,
                                             self.root, "account", "ffmpeg", self.cancel, lambda m: None)
        self.assertEqual(result["score"], 72)
        self.assertEqual(run.call_args.args[3], [path.resolve() for path in files])

    def test_settings_roundtrip_and_missing_model_errors(self):
        self.settings.save(self.root)
        self.assertEqual(SpeechSettings.load(self.root), self.settings)
        from fif_oral.api.client import ApiError
        with self.assertRaisesRegex(ApiError, "缺少"):
            replace(self.settings, your_tts=str(self.root)).validate()

    def test_word_synthesis_has_sentence_ending_without_changing_assessment_reference(self):
        import json
        self.items = challenge_items(word_level())
        files = self.generate()
        self.assertEqual(FakeEngine.requests[0][0], "thank.")
        self.assertEqual(self.items[0].prompt.text, "thank")
        self.assertEqual(self.items[0].reference, "[word]\nthank")
        metadata = json.loads(files[0].with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["synthesis_text"], "thank.")
        self.assertEqual(metadata["reference_text"], "thank")

    def test_base_synthesis_failure_does_not_prevent_other_questions_from_generating(self):
        from fif_oral.api.client import ApiError
        self.items = challenge_items(level(('bad input', 'good input')))

        class Engine(FakeEngine):
            def synthesize(inner, text, target, cancel, **options):
                if text == 'bad input':
                    raise ApiError('generation failed')
                write_wav(target)

        failures = {}
        with patch('fif_oral.audio.speech.SpeechEngine', Engine):
            files = generate_audio(self.settings, self.voice, self.items, self.root, 'account', self.cancel,
                                   lambda m: None, failures=failures)
        self.assertFalse(files[0].exists())
        self.assertTrue(files[1].exists())
        self.assertEqual(failures, {self.items[0].question_id: 'generation failed'})


if __name__ == "__main__":
    unittest.main()
