import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import patch

import fif_oral.challenge as challenge
from fif_oral.audio.assessment import Cancelled
from fif_oral.automation import AutomationScope, AutomationStore, enumerate_levels, run_queue
from fif_oral.challenge import AttemptStore, ChallengePending
from fif_oral.api.client import ApiError, SessionExpired
from fif_oral.audio.speech import SpeechSettings
from test_challenge import Platform, REJECTED_WORD_XML, XML, level, word_level
from test_speech import FakeEngine, fixture_settings, write_wav


class Directory:
    user_id = 'student'
    name = 'Queue student'

    def expired(self):
        return False

    def __init__(self):
        self.units = {'unit1': [self.row('a'), self.row('b')], 'unit2': [self.row('c')]}
        self.children = {name: Platform(word_level()) for name in ('a', 'b', 'c')}
        self.reads = []
        self.unit_error = None

    @staticmethod
    def row(identity, score=None):
        return {'levelId': identity, 'levelName': identity, 'levelScore': score}

    def task(self, task):
        self.reads.append(('task', task))
        return {'ttdList': [{'unitid': 'prefix,' + identity, 'unitName': identity} for identity in self.units]}

    def unit(self, task, unit):
        self.reads.append(('unit', unit))
        if self.unit_error and unit == 'unit1':
            raise self.unit_error
        return {'levelList': self.units[unit]}

    def level(self, identity):
        self.reads.append(('level', identity))
        return self.children[identity].data

    def profile(self):
        return {'language': 2, 'schoolId': 'school'}

    def assessment(self, task, unit, identity):
        return self.children[identity]


class QueueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.client = Directory()
        self.cancel = Event()
        self.scope = AutomationScope('task', 'task', title='Task')
        self.store = self.new_store()
        self.ffmpeg = self.root / 'ffmpeg.exe'
        self.ffmpeg.touch()
        self.store.configure(SpeechSettings(backend='your_tts', word_mode='system', adaptive='off'), None, str(self.ffmpeg), True)

    def new_store(self, account='account', scope=None):
        return AutomationStore(self.root / 'queues', account, self.client.user_id, scope or self.scope)

    def run_queue(self, runner=None):
        if runner is None:
            runner = lambda *args: {'score': 72, 'report_id': 'report'}
        with patch('fif_oral.automation.run_automatic_challenge', side_effect=runner) as mock:
            result = run_queue(self.client, self.store, self.root, self.cancel, lambda text: None)
        return result, mock

    def test_all_scopes_use_server_catalog_and_deduplicate(self):
        self.client.units['unit1'].append(self.client.row('a'))
        jobs, issues = enumerate_levels(self.client, self.scope, self.cancel, lambda text: None)
        self.assertEqual([job['level_id'] for job in jobs], ['a', 'b', 'c'])
        self.assertEqual(issues, [])
        self.client.reads.clear()
        jobs, _ = enumerate_levels(self.client, AutomationScope('unit', 'task', 'unit2'), self.cancel, lambda t: None)
        self.assertEqual([job['level_id'] for job in jobs], ['c'])
        self.assertEqual(self.client.reads, [('unit', 'unit2')])
        jobs, _ = enumerate_levels(self.client, AutomationScope('level', 'task', 'unit1', 'b'), self.cancel, lambda t: None)
        self.assertEqual([job['level_id'] for job in jobs], ['b'])

    def test_directory_failure_is_visible_and_other_units_continue(self):
        self.client.unit_error = ApiError('directory unavailable')
        counts, mock = self.run_queue()
        self.assertEqual(counts['confirmed'], 1)
        self.assertEqual(mock.call_count, 1)
        self.assertEqual(self.store.data['discovery_issues'][0]['unit_id'], 'unit1')
        self.assertEqual(self.store.data['stage'], 'review')
        self.client.unit_error = None
        counts, mock = self.run_queue()
        self.assertEqual((counts['confirmed'], mock.call_count), (3, 2))
        self.assertEqual(self.store.data['stage'], 'complete')

    def test_remote_scores_skip_only_qualified_positive_numbers(self):
        self.client.units['unit1'][0]['levelScore'] = '95'
        self.client.units['unit1'][1]['levelScore'] = 'NaN'
        counts, mock = self.run_queue()
        self.assertEqual((counts['skipped'], counts['confirmed'], mock.call_count), (1, 2, 2))
        self.assertNotIn(('level', 'a'), self.client.reads)
        self.assertEqual(self.store.data['jobs'][0]['score'], 95)

    def test_skip_option_can_be_disabled(self):
        self.client.units['unit1'][0]['levelScore'] = 100
        self.store.data['skip_qualified'] = False
        counts, mock = self.run_queue()
        self.assertEqual((counts['confirmed'], mock.call_count), (3, 3))

    def test_missing_reference_for_chapter_does_not_block_system_words(self):
        self.client.children['a'].data = level()
        counts, mock = self.run_queue()
        self.assertEqual((counts['error'], counts['confirmed'], mock.call_count), (1, 2, 2))
        self.assertIn('参考 WAV', self.store.data['jobs'][0]['message'])

    def test_corrupt_level_progress_is_reported_and_other_levels_continue(self):
        attempt = AttemptStore(self.root / 'challenges', 'account', 'student', 'task', 'unit1', 'a')
        attempt.folder.mkdir(parents=True)
        (attempt.folder / 'broken.json').write_text('{invalid', encoding='utf-8')
        counts, mock = self.run_queue()
        self.assertEqual((counts['error'], counts['confirmed'], mock.call_count), (1, 2, 2))
        self.assertIn('进度文件', self.store.data['jobs'][0]['message'])

    def test_unexpected_failure_persists_interrupted_state(self):
        with self.assertRaises(RuntimeError):
            self.run_queue(lambda *args: (_ for _ in ()).throw(RuntimeError('unexpected')))
        restored = self.new_store()
        self.assertEqual(restored.data['stage'], 'error')
        self.assertEqual([job['state'] for job in restored.data['jobs']], ['error', 'pending', 'pending'])

    def test_review_error_and_unsupported_do_not_block_later_levels(self):
        unsupported = level()
        unsupported['content']['moshi'][0]['showtype'] = 't12'
        self.client.children['c'].data = unsupported
        self.client.units['unit2'].append(self.client.row('d'))
        self.client.children['d'] = Platform(word_level())
        def runner(client, attempt, *args):
            identity = attempt.context['level_id']
            if identity == 'a':
                raise ChallengePending('rejected 28676')
            if identity == 'b':
                raise ApiError('temporary upload error')
            return {'score': 72, 'report_id': identity}
        counts, mock = self.run_queue(runner)
        self.assertEqual([job['state'] for job in self.store.data['jobs']], ['review', 'error', 'unsupported', 'confirmed'])
        self.assertEqual(mock.call_count, 3)
        self.assertEqual(counts['confirmed'], 1)

    def test_pause_restart_retains_confirmed_progress(self):
        def runner(client, attempt, *args):
            self.cancel.set()
            return {'score': 72, 'report_id': attempt.context['level_id']}
        with self.assertRaises(Cancelled):
            self.run_queue(runner)
        self.store = self.new_store()
        self.assertEqual(self.store.data['stage'], 'paused')
        self.assertEqual(self.store.data['jobs'][0]['state'], 'confirmed')
        self.cancel.clear()
        counts, mock = self.run_queue()
        self.assertEqual((counts['confirmed'], mock.call_count), (3, 2))

    def test_expired_session_pauses_queue_and_rejects_another_user(self):
        with self.assertRaises(SessionExpired):
            self.run_queue(lambda *args: (_ for _ in ()).throw(SessionExpired('expired')))
        self.assertEqual(self.new_store().data['stage'], 'needs_login')
        self.assertEqual(self.store.data['jobs'][0]['state'], 'paused')
        self.client.user_id = 'someone-else'
        with self.assertRaisesRegex(ApiError, '身份'):
            self.run_queue()

    def test_queues_are_bound_to_account_user_and_scope_without_secrets(self):
        self.assertNotEqual(self.store.path, self.new_store(account='other').path)
        self.assertNotEqual(self.store.path, self.new_store(scope=AutomationScope('unit', 'task', 'unit1')).path)
        self.client.user_id = 'other-user'
        self.assertNotEqual(self.store.path, self.new_store().path)
        raw = self.store.path.read_text(encoding='utf-8')
        self.assertNotIn('password', raw)
        self.assertNotIn('token', raw)

    def test_cancellation_during_discovery_keeps_existing_progress(self):
        self.run_queue()
        original_unit = self.client.unit
        def unit(task, identity):
            data = original_unit(task, identity)
            self.cancel.set()
            return data
        self.client.unit = unit
        with self.assertRaises(Cancelled):
            self.run_queue()
        restored = self.new_store()
        self.assertEqual(len(restored.data['jobs']), 3)
        self.assertTrue(all(job['state'] == 'confirmed' for job in restored.data['jobs']))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
    def test_full_batch_generates_grades_uploads_and_reconciles_each_level(self):
        settings = fixture_settings(self.root)
        voice = self.root / 'voice.wav'
        write_wav(voice)
        self.store.configure(settings, voice, shutil.which('ffmpeg'), False)
        self.client.children['a'].data = level()
        self.client.children['c'].data = level()
        self.client.children['b'].data = word_level()
        def evaluate(auth, pcm, item, cancel):
            return REJECTED_WORD_XML if item.showtype == 't1' else XML
        def upload(client, profile, item, wav, xml, timestamp, task):
            self.assertTrue(wav.is_file())
            self.assertEqual(xml, XML)
            return f'{timestamp}/{item.group}_{item.index}.mp3'
        with patch('fif_oral.audio.speech.SpeechEngine', FakeEngine), patch.object(challenge, 'evaluate', side_effect=evaluate) as grading, \
                patch.object(challenge, 'upload', side_effect=upload) as uploads:
            counts = run_queue(self.client, self.store, self.root, self.cancel, lambda text: None)
        self.assertEqual((counts['confirmed'], counts['review']), (2, 1))
        self.assertEqual(grading.call_count, 3)
        self.assertEqual(uploads.call_count, 2)
        self.assertEqual([len(child.submissions) for child in self.client.children.values()], [1, 0, 1])
        for job in self.store.data['jobs']:
            attempt = AttemptStore(self.root / 'challenges', 'account', 'student', 'task', job['unit_id'], job['level_id'])
            self.assertEqual(attempt.data['stage'], 'review' if job['level_id'] == 'b' else 'confirmed')
            if job['state'] == 'confirmed':
                self.assertEqual((job['score'], job['report_id']), (72, 'ours'))
        # A broader/reopened queue must reconcile an existing final submission before remote-score skipping,
        # even when local generators and FFmpeg are unavailable. It must never submit it a second time.
        self.client.units = {'unit1': [self.client.row('a', 100)]}
        self.store.data['jobs'][0]['state'] = 'pending'
        uncertain = AttemptStore(self.root / 'challenges', 'account', 'student', 'task', 'unit1', 'a')
        uncertain.data['stage'] = 'submitting'
        uncertain.data.pop('report_id')
        uncertain.save()
        self.store.configure(replace(settings, python='', your_tts=''), None, '', True)
        with patch.object(challenge, 'evaluate', side_effect=AssertionError('regraded')), \
                patch.object(challenge, 'upload', side_effect=AssertionError('reuploaded')):
            result = run_queue(self.client, self.store, self.root, self.cancel, lambda text: None)
        self.assertEqual(result['confirmed'], 1)
        self.assertEqual(result['skipped'], 0)
        self.assertEqual(len(self.client.children['a'].submissions), 1)


if __name__ == '__main__':
    unittest.main()
