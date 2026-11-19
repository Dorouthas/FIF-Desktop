import asyncio
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from threading import Event, Timer
from unittest.mock import patch

from fif_oral.audio.assessment import Cancelled, challenge_items
from fif_oral.audio.candidates import CandidateGenerator
from fif_oral.audio.edge import EdgeSpeech
from fif_oral.api.client import ApiError
from fif_oral.audio.speech import SpeechSettings, generate_audio, needs_reference_voice, run_automatic_challenge
from fif_oral.challenge import AttemptStore, file_hash
from test_challenge import level, word_level
from test_speech import FakeEngine, fixture_settings, write_wav


class FakeEdge:
    version = 'fixture'
    requests = []

    def __init__(self, settings, ffmpeg):
        self.settings = settings

    def synthesize(self, text, target, cancel, *, rate=None):
        self.requests.append((text, self.settings.edge_voice, rate or self.settings.edge_rate))
        write_wav(target, 440 + int((rate or self.settings.edge_rate)[:-1]))


class EdgeSpeechTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cancel = Event()
        self.items = challenge_items(level())
        FakeEdge.requests = []

    def test_new_defaults_and_old_model_configuration_are_preserved(self):
        self.assertEqual(SpeechSettings.load(self.root).backend, 'edge')
        self.assertEqual(SpeechSettings.load(self.root).word_mode, 'model')
        for backend in ('your_tts', 'xtts_v2'):
            (self.root / 'speech.json').write_text(json.dumps({'backend': backend, backend: 'custom-folder', 'python': 'custom-python'}))
            loaded = SpeechSettings.load(self.root)
            self.assertEqual((loaded.backend, loaded.word_mode, getattr(loaded, backend)), (backend, 'system', 'custom-folder'))
            loaded.save(self.root)
            self.assertEqual(SpeechSettings.load(self.root), loaded)

    def test_edge_generation_needs_no_model_python_or_reference_and_cache_binds_voice(self):
        settings = SpeechSettings(python='', your_tts='', xtts_v2='')
        self.assertFalse(needs_reference_voice(self.items, settings))
        with patch('fif_oral.audio.speech.EdgeSpeech', FakeEdge), patch('fif_oral.audio.speech.SpeechEngine', side_effect=AssertionError('local model should not load')):
            first = generate_audio(settings, None, self.items, self.root, 'account', self.cancel, lambda m: None)
            self.assertTrue(first[0].is_file())
            self.assertEqual(generate_audio(settings, None, self.items, self.root, 'account', self.cancel, lambda m: None), first)
            self.assertEqual(len(FakeEdge.requests), 1)
            changed = generate_audio(replace(settings, edge_voice='en-US-JennyNeural'), None,
                                     self.items, self.root, 'account', self.cancel, lambda m: None)
            self.assertNotEqual(first, changed)
            self.assertNotEqual(first, generate_audio(settings, None, self.items, self.root, 'other', self.cancel, lambda m: None))

    def test_local_model_generation_and_reference_requirement_still_work(self):
        settings = fixture_settings(self.root)
        voice = self.root / 'voice.wav'
        write_wav(voice)
        self.assertTrue(needs_reference_voice(self.items, settings))
        with patch('fif_oral.audio.speech.SpeechEngine', FakeEngine), patch('fif_oral.audio.speech.EdgeSpeech', side_effect=AssertionError('Edge should not load')):
            result = generate_audio(settings, voice, self.items, self.root, 'account', self.cancel, lambda m: None)
            self.assertTrue(result[0].is_file())
        self.assertTrue(needs_reference_voice(self.items, replace(settings, backend='xtts_v2')))

    def test_edge_candidates_keep_voice_and_allow_explicit_local_fallback(self):
        settings = fixture_settings(self.root)
        settings = replace(settings, backend='edge', fallback_model='your_tts', context_mode='off')
        voice = self.root / 'voice.wav'
        write_wav(voice)
        with patch('fif_oral.audio.speech.EdgeSpeech', FakeEdge), patch('fif_oral.audio.speech.SpeechEngine', FakeEngine):
            generator = CandidateGenerator(settings, voice, self.root, self.items, self.cancel)
            try:
                generator.ffmpeg = 'fixture'
                specs = generator.specs(self.items[0])
                for index, spec in enumerate(specs):
                    generator.generate(self.items[0], spec, self.root / f'{index}.wav', self.cancel)
                self.assertEqual(len(FakeEdge.requests), 3)
                self.assertEqual({request[1] for request in FakeEdge.requests}, {settings.edge_voice})
                self.assertEqual(len({request[2] for request in FakeEdge.requests}), 3)
                self.assertTrue(any(s.get('backend') == 'your_tts' for s in specs))
            finally:
                generator.close()

    def test_edge_context_uses_edge_and_local_budget_signature_ignores_inactive_edge_fields(self):
        item = challenge_items(word_level())[0]
        settings = SpeechSettings()
        with patch('fif_oral.audio.speech.EdgeSpeech', FakeEdge), patch('fif_oral.audio.candidates.convert_audio', return_value=(b'pcm', 1)), \
                patch('fif_oral.audio.candidates.extract_word', return_value={'start': 0, 'end': 1}):
            generator = CandidateGenerator(settings, None, self.root, [item], self.cancel)
            generator.client, generator.ffmpeg = object(), 'fixture'
            generator.align_request = lambda *args: b'fixture-xml'
            result = generator.generate(item, {'kind': 'context'}, self.root / 'word.wav', self.cancel)
            self.assertIn('thank', result['context_text'])
            self.assertEqual(len(FakeEdge.requests), 1)
        local = fixture_settings(self.root)
        voice = self.root / 'voice.wav'
        write_wav(voice)
        first = CandidateGenerator(local, voice, self.root, [item], self.cancel)
        second = CandidateGenerator(replace(local, edge_voice='en-US-JennyNeural'), voice, self.root, [item], self.cancel)
        self.assertEqual(first.signature, second.signature)

    def test_pending_switch_to_edge_does_not_require_previous_reference_file(self):
        settings = SpeechSettings()
        audio = self.root / 'base.wav'
        write_wav(audio)
        store = AttemptStore(self.root / 'challenges', 'account', 'student', 'task', 'unit', 'level')
        from dataclasses import asdict
        store.start(self.items, [audio], [file_hash(audio)], {'mode': 'speech', 'backend': 'edge',
                    'strategy': asdict(settings), 'voice': str(self.root / 'removed-reference.wav'), 'voice_sha256': 'old'})
        with patch('fif_oral.audio.speech.generate_audio', side_effect=AssertionError('started audio should be preserved')), \
                patch('fif_oral.audio.speech.run_challenge', return_value={'score': 72}) as run:
            run_automatic_challenge(None, store, self.items, settings, None, self.root, 'account',
                                    'ffmpeg', self.cancel, lambda message: None)
        self.assertNotIn('voice', store.data['source'])
        self.assertEqual(run.call_args.args[3], [audio.resolve()])

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
    def test_cancel_stops_edge_request_without_converting_or_saving_output(self):
        class Communicate:
            def __init__(self, *args, **kwargs):
                pass
            async def save(self, path):
                await asyncio.sleep(10)
        timer = Timer(.05, self.cancel.set)
        timer.start()
        try:
            with patch('edge_tts.Communicate', Communicate), patch('fif_oral.audio.edge.convert_audio') as convert:
                engine = EdgeSpeech(SpeechSettings(), shutil.which('ffmpeg'))
                with self.assertRaises(Cancelled):
                    engine.synthesize('Hello world.', self.root / 'cancelled.wav', self.cancel)
                convert.assert_not_called()
            self.assertFalse((self.root / 'cancelled.wav').exists())
        finally:
            timer.cancel()


if __name__ == '__main__':
    unittest.main()
