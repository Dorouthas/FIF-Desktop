import json
import math
import shutil
import struct
import tempfile
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import patch

import fif_oral.audio.assessment as assessment
import fif_oral.challenge as challenge
from fif_oral.audio.adaptive import AdaptiveAudio, select_profile
from fif_oral.audio.speech import SpeechSettings, generate_audio
from fif_oral.audio.text import normalize
from test_challenge import Platform, REJECTED_WORD_XML, word_level


ALL = {"base", "padding", "slower", "faster", "quieter"}
VALID_WORD = REJECTED_WORD_XML.replace(b'is_rejected="true"', b'is_rejected="false"').replace(b'28676', b'0')


class SelectionTests(unittest.TestCase):
    def test_legacy_profile_rules_and_scale(self):
        cases = [([], "base"), ([("base", 4.2, 0)], "base"), ([("base", 3, 0)], "slower"),
                 ([("base", 0, 28673)], "padding"), ([("base", 0, 28689)], "padding"),
                 ([("base", 0, 28690)], "quieter"), ([("base", 0, 28676)], "slower"),
                 ([("base", 3, 0), ("slower", 3.5, 0)], "faster"),
                 ([("base", 3, 0), ("slower", 4.5, 0)], "slower"),
                 ([("base", 4.1, 0), ("faster", 5, 28676)], "base")]
        for history, expected in cases:
            with self.subTest(history=history):
                self.assertEqual(select_profile(history, ALL, 80), expected)

    def test_old_currency_ordinal_and_decimal_normalization(self):
        self.assertEqual(normalize('The 21st costs $12.50 and 3.14.'),
                         'The twenty-first costs twelve dollars and fifty cents and three point one four.')
        self.assertIn('one thousand', normalize('1,000'))
        self.assertEqual(normalize('X12'), 'X12')


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
class AdaptiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "base.wav"
        with wave.open(str(self.source), "wb") as file:
            file.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            file.writeframes(b"".join(struct.pack("<h", int(12000 * math.sin(i * 2 * math.pi * 440 / 16000)))
                                      for i in range(8000)))
        self.item = assessment.challenge_items(word_level())[0]
        self.context = {"account": "a", "level": "level"}
        self.cancel = Event()
        self.ffmpeg = shutil.which("ffmpeg")
        self.policy = AdaptiveAudio(self.root / "adaptive", self.context, self.ffmpeg)
        self.store = challenge.AttemptStore(self.root / "attempts", "a", "student", "task", "unit", "level")
        self.platform = Platform(word_level())

    def run_flow(self, xmls, upload):
        with patch.object(challenge, "evaluate", side_effect=xmls) as evaluate, \
                patch.object(challenge, "upload", side_effect=upload):
            result = challenge.run_challenge(self.platform, self.store, [self.item], [self.source],
                                             self.ffmpeg, self.cancel, lambda m: None, audio_policy=self.policy)
        return result, evaluate.call_count

    def test_rejection_selects_variant_then_uploads_matching_xml_only_once(self):
        uploads = []

        def upload(client, profile, item, wav, xml, *args):
            uploads.append(xml)
            return "selected.mp3"

        result, count = self.run_flow([REJECTED_WORD_XML, VALID_WORD], upload)
        self.assertEqual(count, 2)
        self.assertEqual(uploads, [VALID_WORD])
        self.assertEqual(len(self.platform.submissions), 1)
        self.assertEqual(self.platform.submissions[0][0]["score"], 84)
        self.assertEqual(self.store.data["evaluations"][self.item.question_id]["profile"], "slower")
        self.assertEqual(len(self.store.data["evaluation_history"][self.item.question_id]), 2)
        next_policy = AdaptiveAudio(self.root / "adaptive", self.context, self.ffmpeg)
        self.assertEqual(next_policy.prepare(self.item, self.source, set(), self.cancel).profile, "slower")

    def test_all_rejected_is_bounded_and_keeps_every_raw_xml(self):
        uploads = []
        with self.assertRaises(assessment.EvaluationRejected):
            self.run_flow([REJECTED_WORD_XML] * 3, lambda *a: uploads.append(a))
        self.assertFalse(uploads)
        self.assertFalse(self.platform.submissions)
        history = self.store.data["evaluation_history"][self.item.question_id]
        self.assertEqual([entry["profile"] for entry in history], ["base", "slower", "faster"])
        self.assertEqual(len({entry["xml"] for entry in history}), 3)
        for entry in history:
            self.assertEqual((self.store.folder / entry["xml"]).read_bytes(), REJECTED_WORD_XML)

    def test_valid_low_score_is_kept_if_other_profiles_are_rejected(self):
        low = VALID_WORD.replace(b'4.2', b'3.2')
        uploaded = []
        self.run_flow([low, REJECTED_WORD_XML, REJECTED_WORD_XML],
                      lambda client, profile, item, wav, xml, *a: uploaded.append(xml) or "low.mp3")
        self.assertEqual(uploaded, [low])
        self.assertEqual(self.store.data["evaluations"][self.item.question_id]["profile"], "base")
        self.assertFalse(self.store.data["evaluations"][self.item.question_id]["rejected"])
        self.assertEqual(self.platform.submissions[0][0]["score"], 64)

    def test_clipping_signal_selects_quieter_without_changing_raw_rejection_flag(self):
        clipped = VALID_WORD.replace(b'except_info="0"', b'except_info="28690"')
        self.run_flow([clipped, VALID_WORD], lambda *args: "quiet.mp3")
        history = self.store.data["evaluation_history"][self.item.question_id]
        self.assertFalse(history[0]["rejected"])
        self.assertFalse(history[0]["usable"])
        self.assertEqual(history[1]["profile"], "quieter")
        self.assertTrue(history[1]["usable"])
        self.assertEqual((self.store.folder / history[0]["xml"]).read_bytes(), clipped)

    def test_cancel_between_trials_preserves_history_and_resume_uses_next_profile(self):
        low = VALID_WORD.replace(b'4.2', b'3.2')

        def cancelled(*args):
            self.cancel.set()
            return low

        with patch.object(challenge, 'evaluate', side_effect=cancelled), patch.object(challenge, 'upload') as upload:
            with self.assertRaises(assessment.Cancelled):
                challenge.run_challenge(self.platform, self.store, [self.item], [self.source], self.ffmpeg,
                                         self.cancel, lambda m: None, audio_policy=self.policy)
        upload.assert_not_called()
        self.assertFalse(self.store.data['completed'])
        self.cancel.clear()
        self.policy = AdaptiveAudio(self.root / 'adaptive', self.context, self.ffmpeg)
        self.run_flow([VALID_WORD], lambda *a: 'resumed.mp3')
        self.assertEqual(self.store.data['evaluations'][self.item.question_id]['profile'], 'slower')
        self.assertEqual(len(self.platform.submissions), 1)

    def test_padding_and_volume_are_applied_to_original_audio(self):
        key = self.policy._key(self.item, self.source)
        self.policy.history[key] = [["base", 0, 28673]]
        padding = self.policy.prepare(self.item, self.source, set(), self.cancel)
        self.assertEqual(padding.profile, "padding")
        with wave.open(str(padding.path), "rb") as file:
            self.assertEqual(file.getnframes(), 16000)
        self.policy.history[key] = [["base", 0, 28690]]
        quiet = self.policy.prepare(self.item, self.source, set(), self.cancel)
        self.assertEqual(quiet.profile, "quieter")
        with wave.open(str(quiet.path), "rb") as file:
            samples = struct.unpack('<' + 'h' * file.getnframes(), file.readframes(file.getnframes()))
        self.assertLess(max(abs(x) for x in samples), 8500)

    def test_adaptive_history_separates_account_and_changed_base_audio(self):
        original = self.policy.prepare(self.item, self.source, set(), self.cancel)
        self.policy.observe(original, {"except_info": "0", "rejected": False}, 90)
        other = AdaptiveAudio(self.root / "adaptive", {**self.context, "account": "b"}, self.ffmpeg)
        self.assertNotEqual(other.folder, self.policy.folder)
        self.source.write_bytes(self.source.read_bytes() + b"changed")
        self.assertNotEqual(self.policy.prepare(self.item, self.source, set(), self.cancel).key, original.key)

    def test_system_word_route_needs_neither_model_nor_reference_voice(self):
        class System:
            voice_id = "local-US-English"

            def __init__(self, cancel):
                pass

            def synthesize(inner, text, path, cancel):
                self.assertEqual(text, 'thank')
                shutil.copyfile(self.source, path)

        with patch('fif_oral.audio.speech.SystemSpeech', System), patch('fif_oral.audio.speech.SpeechEngine', side_effect=AssertionError('no neural model')):
            files = generate_audio(SpeechSettings(backend='your_tts', word_mode='system'), None, [self.item], self.root, 'a', self.cancel, lambda m: None)
        metadata = json.loads(files[0].with_suffix('.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata['engine'], 'system')
        self.assertIsNone(metadata['identity']['voice'])
        self.assertEqual(metadata['identity']['system_voice'], 'local-US-English')


if __name__ == "__main__":
    unittest.main()
