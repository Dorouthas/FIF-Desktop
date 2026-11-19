import json
import shutil
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

import fif_oral.challenge as challenge
from fif_oral.audio.assessment import EvaluationInterrupted, challenge_items
from fif_oral.audio.candidates import CandidatePolicy
from test_adaptive import VALID_WORD
from test_challenge import Platform, REJECTED_WORD_XML, level
from test_speech import write_wav


def words(*texts):
    data = level(texts)
    data['content']['moshi'][0]['showtype'] = 't1'
    return data


def result_xml(word, rejected=False):
    return (REJECTED_WORD_XML if rejected else VALID_WORD).replace(b'content="thank"',
                                                                 f'content="{word}"'.encode())


class Generator:
    signature = 'test-generator-v1'

    def __init__(self, source):
        self.source = source
        self.duplicates = False
        self.generated = []

    def specs(self, item):
        return [{'id': f'native-{i}', 'kind': 'test', 'label': f'new synthesis {i}', 'index': i} for i in range(4)]

    def generate(self, item, spec, target, cancel):
        self.generated.append((item.prompt.text, spec['id']))
        if self.duplicates:
            shutil.copyfile(self.source, target)
        else:
            write_wav(target, 550 + spec['index'] * 50)


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
class CandidateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.audio = self.root / 'base.wav'
        write_wav(self.audio)
        self.cancel = Event()
        self.ffmpeg = shutil.which('ffmpeg')
        self.generator = Generator(self.audio)
        self.data = words('bad', 'good')
        self.platform = Platform(self.data)
        self.items = challenge_items(self.data)
        self.store = challenge.AttemptStore(self.root / 'attempts', 'account', 'student', 'task', 'unit', 'level')

    def policy(self, trials=2):
        return CandidatePolicy(self.root / 'candidates', self.store.context, self.ffmpeg,
                               self.generator, target=80, trials=trials)

    def run_flow(self, policy, evaluate, upload, source=None):
        with patch('fif_oral.challenge.evaluate', side_effect=evaluate), patch('fif_oral.challenge.upload', side_effect=upload):
            return challenge.run_challenge(self.platform, self.store, self.items, [self.audio] * len(self.items),
                                            self.ffmpeg, self.cancel, lambda m: None, audio_policy=policy, source=source)

    def test_failed_question_continues_and_restart_preserves_budget_and_successes(self):
        calls, uploads = [], []

        def evaluation(auth, pcm, item, cancel):
            calls.append(item.prompt.text)
            return result_xml(item.prompt.text, item.prompt.text == 'bad')

        with self.assertRaises(challenge.ChallengePending):
            self.run_flow(self.policy(), evaluation, lambda *a: uploads.append(a))
        self.assertEqual(calls, ['bad', 'bad', 'good'])
        self.assertFalse(uploads)
        self.assertFalse(self.platform.submissions)
        self.assertIn(self.items[0].question_id, self.store.data['pending'])
        self.assertIn(self.items[1].question_id, self.store.data['prepared'])
        with self.assertRaises(challenge.ChallengePending):
            self.run_flow(self.policy(), lambda *a: self.fail('spent budget must not reevaluate'), lambda *a: uploads.append(a))
        self.assertFalse(uploads)
        # Increasing the explicit budget allows one genuinely new candidate.
        self.run_flow(self.policy(3), lambda auth, pcm, item, cancel: result_xml(item.prompt.text),
                      lambda *args: uploads.append(args) or f"audio-{len(uploads)}.mp3")
        self.assertEqual(len(uploads), 2)
        self.assertEqual(len(self.platform.submissions), 1)
        self.assertEqual(len(self.store.data['evaluation_history'][self.items[1].question_id]), 1)

    def test_duplicate_synthesis_is_not_sent_for_assessment(self):
        self.items = self.items[:1]
        self.platform.data = words('bad')
        self.generator.duplicates = True
        count = []

        def evaluation(*args):
            count.append(1)
            return REJECTED_WORD_XML

        with self.assertRaises(challenge.ChallengePending):
            self.run_flow(self.policy(3), evaluation, lambda *a: self.fail('no upload'))
        self.assertEqual(len(count), 1)
        ledger = self.policy(3).ledger['questions']
        self.assertEqual(next(iter(ledger.values()))['evaluations_used'], 1)
        self.assertTrue(any(row['status'] == 'duplicate' for row in next(iter(ledger.values()))['specs'].values()))

    def test_verified_cache_reuses_only_matching_wav_and_original_xml(self):
        self.items = self.items[:1]
        self.platform.data = words('bad')
        self.run_flow(self.policy(), lambda *a: VALID_WORD, lambda *a: 'first.mp3')
        previous_submissions = len(self.platform.submissions)
        self.platform = Platform(words('bad'))
        self.store.path, self.store.data = None, {}
        self.run_flow(self.policy(), lambda *a: self.fail('qualified cache should supply XML'), lambda *a: 'cached.mp3')
        self.assertTrue(self.store.data['evaluations'][self.items[0].question_id]['cached'])
        self.assertEqual(previous_submissions + len(self.platform.submissions), 2)

    def test_unknown_network_outcome_spends_one_candidate_budget_across_restart(self):
        self.items = self.items[:1]
        self.platform.data = words('bad')
        self.cancel = type('Cancel', (), {'is_set': lambda self: False, 'wait': lambda self, delay: False})()
        calls = []

        def evaluation(*args):
            calls.append(1)
            raise EvaluationInterrupted('lost response')

        with self.assertRaises(challenge.ChallengePending):
            self.run_flow(self.policy(1), evaluation, lambda *a: self.fail('no upload'))
        self.assertEqual(len(calls), 3)  # bounded transport retries, one candidate reservation
        with self.assertRaises(challenge.ChallengePending):
            self.run_flow(self.policy(1), lambda *a: self.fail('do not repeat unknown candidate'), lambda *a: None)
        self.assertEqual(next(iter(self.policy(1).ledger['questions'].values()))['evaluations_used'], 1)

    def test_tampered_prepared_audio_blocks_all_uploads(self):
        with self.assertRaises(challenge.ChallengePending):
            self.run_flow(self.policy(), lambda auth, pcm, item, cancel: result_xml(item.prompt.text, item.prompt.text == 'bad'),
                          lambda *a: self.fail('no upload'))
        prepared = self.store.data['prepared'][self.items[1].question_id]
        path = Path(prepared['audio'])
        path.write_bytes(path.read_bytes() + b'tampered')
        with self.assertRaisesRegex(challenge.ApiError, '被修改'):
            self.run_flow(self.policy(3), lambda *a: VALID_WORD, lambda *a: self.fail('all pairs checked before uploading'))
        self.assertFalse(self.platform.submissions)

    def test_changed_prepared_score_must_match_original_xml_before_upload(self):
        with self.assertRaises(challenge.ChallengePending):
            self.run_flow(self.policy(), lambda auth, pcm, item, cancel: result_xml(item.prompt.text, item.prompt.text == 'bad'),
                          lambda *a: self.fail('no upload'))
        self.store.data['prepared'][self.items[1].question_id]['result']['score'] = 100
        with self.assertRaisesRegex(challenge.ApiError, '分数与原始 XML 不一致'):
            self.run_flow(self.policy(3), lambda *a: VALID_WORD, lambda *a: self.fail('no upload'))
        self.assertFalse(self.platform.submissions)

    def test_old_rejected_hashes_are_migrated_without_spending_new_budget(self):
        self.items = self.items[:1]
        self.platform.data = words('bad')
        self.store.start(self.items, [self.audio], [challenge.file_hash(self.audio)])
        self.store.data['evaluation_history'] = {self.items[0].question_id: [{
            'usable': False, 'rejected': True, 'audio_sha256': challenge.file_hash(self.audio),
            'audio': str(self.audio), 'except_info': '28676'}]}
        calls = []
        self.run_flow(self.policy(1), lambda auth, pcm, item, cancel: calls.append(pcm) or VALID_WORD,
                      lambda *a: 'new.mp3')
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.generator.generated, [('bad', 'native-0')])
        self.assertEqual(self.store.data['evaluations'][self.items[0].question_id]['profile'], 'native-0')

    def test_missing_failed_base_is_replaced_by_real_candidate(self):
        self.items = self.items[:1]
        self.platform.data = words('bad')
        self.audio = self.root / 'failed-base.wav'
        source = {'mode': 'speech', 'generation_failed': {self.items[0].question_id: 'base synthesis failed'}}
        self.run_flow(self.policy(1), lambda *a: VALID_WORD, lambda *a: 'generated.mp3', source=source)
        self.assertEqual(self.store.data['hashes'], [None])
        self.assertTrue(Path(self.store.data['prepared'][self.items[0].question_id]['audio']).is_file())
        self.assertEqual(self.generator.generated, [('bad', 'native-0')])

    def test_digital_silence_is_rejected_locally_without_spending_ise_budget(self):
        import wave
        self.items = self.items[:1]
        self.platform.data = words('bad')
        with wave.open(str(self.audio), 'wb') as file:
            file.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            file.writeframes(b'\0' * 6400)
        calls = []
        self.run_flow(self.policy(1), lambda *a: calls.append(a) or VALID_WORD, lambda *a: 'real.mp3')
        self.assertEqual(len(calls), 1)
        self.assertEqual(next(iter(self.policy(1).ledger['questions'].values()))['evaluations_used'], 1)


if __name__ == '__main__':
    unittest.main()
