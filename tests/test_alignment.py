import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from threading import Event
from unittest.mock import patch

from fif_oral.audio.alignment import carrier_sentence, extract_word
from fif_oral.audio.assessment import challenge_items
from fif_oral.audio.candidates import CandidateGenerator, CandidatePolicy
from fif_oral.challenge import AttemptStore, file_hash, run_challenge
from fif_oral.api.client import ApiError
from fif_oral.audio.speech import SpeechSettings
from test_candidates import result_xml, words
from test_challenge import Platform
from test_speech import write_wav


XML = b'''<xml_result><read_sentence><rec_paper><read_chapter total_score="4" is_rejected="false" except_info="0">
<sentence><word content="Please" beg_pos="0" end_pos="10"/>
<word content="thin" beg_pos="30" end_pos="70" dp_message="0"/>
<word content="clearly" beg_pos="80" end_pos="100"/></sentence>
</read_chapter></rec_paper></read_sentence></xml_result>'''


class AlignmentTests(unittest.TestCase):
    def test_unique_boundaries_crop_with_consonant_margin_and_padding(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target = root / 'sentence.wav', root / 'word.wav'
            with wave.open(str(source), 'wb') as file:
                file.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                file.writeframes(b'\1\0' * 32000)
            bounds = extract_word(source, target, XML, 'thin')
            self.assertEqual(bounds['begin_frame'], 30)
            with wave.open(str(target), 'rb') as file:
                self.assertEqual(file.getnframes(), round((0.4 + 0.08 + 0.4) * 16000))
            with self.assertRaises(ApiError):
                extract_word(source, target, XML.replace(b'is_rejected="false"', b'is_rejected="true"'), 'thin')
            with self.assertRaises(ApiError):
                extract_word(source, target, XML, 'absent')

    def test_carrier_does_not_repeat_target(self):
        self.assertEqual(carrier_sentence('thin').split().count('thin'), 1)
        self.assertIn('They said please', carrier_sentence('please'))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
    def test_alignment_and_target_each_consume_a_budget_slot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = root / 'base.wav'
            write_wav(base)
            data = words('thin')
            items = challenge_items(data)
            platform = Platform(data)
            store = AttemptStore(root / 'attempts', 'a', 'student', 'task', 'unit', 'level')
            store.start(items, [base], [file_hash(base)])
            store.data['evaluation_history'] = {items[0].question_id: [{
                'usable': False, 'rejected': True, 'audio_sha256': file_hash(base),
                'audio': str(base), 'except_info': '28676'}]}

            class System:
                voice_id = 'test-English'

                def __init__(self, cancel):
                    pass

                def synthesize(self, text, target, cancel, **options):
                    if text == 'thin':
                        shutil.copyfile(base, target)
                    else:
                        with wave.open(str(target), 'wb') as file:
                            file.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                            file.writeframes(b'\1\1' * 32000)

            with patch('fif_oral.audio.speech.SystemSpeech', System):
                generator = CandidateGenerator(SpeechSettings(backend='your_tts', word_mode='system'), None, root, items, Event())
            policy = CandidatePolicy(root / 'candidates', store.context, shutil.which('ffmpeg'), generator, 80, 2)
            with patch('fif_oral.audio.candidates.evaluate', return_value=XML) as align, \
                    patch('fif_oral.challenge.evaluate', return_value=result_xml('thin')) as target, \
                    patch('fif_oral.challenge.upload', return_value='aligned.mp3'):
                run_challenge(platform, store, items, [base], shutil.which('ffmpeg'), Event(), lambda m: None,
                               audio_policy=policy)
            self.assertEqual((align.call_count, target.call_count), (1, 1))
            entry = next(iter(policy.ledger['questions'].values()))
            self.assertEqual(entry['evaluations_used'], 2)
            self.assertEqual(store.data['evaluations'][items[0].question_id]['profile'], 'context-word-v1')
            self.assertTrue(Path(entry['specs']['context-word-v1']['alignment_xml']).is_file())


if __name__ == '__main__':
    unittest.main()
