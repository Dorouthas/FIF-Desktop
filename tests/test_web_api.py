import io
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
    import fif_oral.web.app as web_app
except ImportError:
    TestClient = None

from fif_oral.api.auth import CaptchaRequired
from fif_oral.audio.assessment import Cancelled
from fif_oral.api.client import FiFClient
from fif_oral.storage import Vault
from test_automation import Directory
from test_challenge import XML, REJECTED_WORD_XML, level, word_level
from test_speech import FakeEngine, fixture_settings, write_wav


class WebPlatform(Directory):
    def tasks(self, status):
        return [{'taskId': 'task', 'taskName': '口语任务 <示例>', 'score': 91, 'complete': '50'}]


@unittest.skipUnless(TestClient, 'Install requirements-web.txt for WebUI coverage')
class WebApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = web_app.create_app(self.root, allowed_hosts={'testserver'})
        self.http = TestClient(self.app, headers={'X-FIF-Client': '1'})
        self.http.__enter__()
        self.addCleanup(self.http.__exit__, None, None, None)
        self.assertEqual(self.http.post('/api/unlock', json={'password': 'test-master', 'confirmation': 'test-master'}).status_code, 200)
        response = self.http.post('/api/accounts', json={'name': '测试账号', 'username': 'student', 'password': 'stored-secret'})
        self.assertEqual(response.status_code, 200, response.text)
        self.account = response.json()
        self.platform = WebPlatform()
        with patch('fif_oral.web.app.LoginFlow.sign_in', return_value=self.platform):
            self.assertEqual(self.http.post(f'/api/accounts/{self.account["id"]}/login', json={}).status_code, 200)

    def start(self):
        return self.http.post('/api/queues', json={'account_id': self.account['id'], 'scope': {
            'kind': 'task', 'task_id': 'task', 'title': '批量口语任务'}, 'voice_id': getattr(self, 'voice_id', None)})

    def upload_voice(self):
        audio = self.root / 'source.wav'
        write_wav(audio)
        response = self.http.post(f'/api/accounts/{self.account["id"]}/voices', files={'file': ('speaker.wav', audio.read_bytes(), 'audio/wav')})
        self.assertEqual(response.status_code, 200, response.text)
        self.voice_id = response.json()['voices'][0]['id']
        return self.voice_id

    def test_real_client_login_remains_connected_and_can_load_tasks(self):
        client = FiFClient('fixture-token', '10003', 'student', 'Test student', time.time() + 3600)
        self.assertFalse(client.expired())
        with patch('fif_oral.web.app.LoginFlow.sign_in', return_value=client):
            response = self.http.post(f'/api/accounts/{self.account["id"]}/login', json={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['connected'])
        self.assertTrue(response.json()['account']['connected'])
        public = self.http.get('/api/accounts').json()['accounts'][0]
        self.assertTrue(public['connected'])
        with patch.object(FiFClient, 'tasks', return_value=self.platform.tasks(1)) as tasks:
            response = self.http.get(f'/api/catalog/{self.account["id"]}/tasks')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()), 1)
        tasks.assert_called_once_with(1)

    def test_real_client_expiry_is_evaluated_in_account_and_api_guards(self):
        for expiry, connected in ((None, True), (time.time() + 3600, True), (time.time() - 1, False)):
            with self.subTest(expiry=expiry):
                self.app.state.local.sessions[self.account['id']] = FiFClient(
                    'fixture-token', '10003', 'student', 'Test student', expiry)
                public = self.http.get('/api/accounts').json()['accounts'][0]
                self.assertEqual(public['connected'], connected)
                with patch.object(FiFClient, 'tasks', return_value=[]) as tasks:
                    response = self.http.get(f'/api/catalog/{self.account["id"]}/tasks')
                self.assertEqual(response.status_code, 200 if connected else 428, response.text)
                self.assertEqual(tasks.call_count, int(connected))

    def test_same_origin_authentication_and_static_boundary(self):
        self.assertEqual(self.http.get('/').status_code, 200)
        self.assertEqual(self.http.get('/static/app.js').status_code, 200)
        self.assertEqual(self.http.get('/api/accounts', headers={'X-FIF-Client': ''}).status_code, 403)
        self.assertEqual(self.http.get('/api/accounts', headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.http.get('/api/accounts', headers={'Host': 'evil.example'}).status_code, 403)
        self.assertEqual(self.http.get('/static/%2e%2e/accounts.json').status_code, 404)
        self.assertEqual(self.http.get('/data/accounts.json').status_code, 404)
        self.assertEqual(self.http.post('/api/lock').status_code, 200)
        self.assertEqual(self.http.get('/api/accounts').status_code, 401)
        self.assertFalse(self.http.get('/api/bootstrap').json()['unlocked'])
        self.assertEqual(self.http.post('/api/unlock', json={'password': 'wrong'}).status_code, 400)

    def test_accounts_encrypted_and_credentials_never_returned(self):
        response = self.http.get('/api/accounts')
        self.assertNotIn('stored-secret', response.text)
        self.assertNotIn('test-master', (self.root / 'accounts.json').read_text())
        self.assertNotIn('stored-secret', (self.root / 'accounts.json').read_text())
        self.assertTrue(response.json()['accounts'][0]['has_password'])
        persisted = Vault(self.root / 'accounts.json', 'test-master')
        self.assertEqual(persisted.accounts[0].password, 'stored-secret')
        duplicate = self.http.post('/api/accounts', json={'name': '重复', 'username': 'student'})
        self.assertEqual(duplicate.status_code, 409)
        invalid = self.http.post('/api/accounts', json={'name': 'x', 'username': 'x', 'unexpected': 'must-not-echo'})
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn('must-not-echo', invalid.text)

    def test_captcha_continuation_and_binding_changes_clear_session(self):
        class Flow:
            def sign_in(flow, username, password, expected, answer):
                if answer != 'answer':
                    raise CaptchaRequired(b'png-image', '请输入验证码')
                return self.platform
        self.app.state.local.flows.clear()
        with patch('fif_oral.web.app.LoginFlow', Flow):
            first = self.http.post(f'/api/accounts/{self.account["id"]}/login', json={}).json()
            self.assertFalse(first['connected'])
            self.assertTrue(first['captcha'].startswith('data:image/png;base64,'))
            second = self.http.post(f'/api/accounts/{self.account["id"]}/login', json={'answer': 'answer'}).json()
            self.assertTrue(second['connected'])
            self.assertEqual(second['account']['user_id'], 'student')
        changed = self.http.put(f'/api/accounts/{self.account["id"]}', json={'name': 'new', 'username': 'different'})
        self.assertEqual(changed.status_code, 200)
        self.assertFalse(changed.json()['connected'])
        self.assertEqual(changed.json()['user_id'], '')
        self.assertEqual(self.http.get(f'/api/catalog/{self.account["id"]}/tasks').status_code, 428)

    def test_voice_upload_preview_default_and_account_isolation(self):
        voice = self.upload_voice()
        base = f'/api/accounts/{self.account["id"]}/voices/{voice}'
        self.assertTrue(self.http.get(base + '/audio').content.startswith(b'RIFF'))
        other = self.http.post('/api/accounts', json={'name': 'other', 'username': 'other'}).json()
        self.assertEqual(self.http.get(f'/api/accounts/{other["id"]}/voices/{voice}/audio').status_code, 404)
        self.assertEqual(self.http.post(base + '/default').status_code, 200)
        invalid = self.http.post(f'/api/accounts/{self.account["id"]}/voices', files={'file': ('bad.wav', b'not-a-wav')})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(self.http.delete(base).status_code, 200)
        self.assertEqual(self.http.get(base + '/audio').status_code, 404)

    def test_catalog_all_levels_and_unsupported_type_are_exposed(self):
        prefix = f'/api/catalog/{self.account["id"]}'
        tasks = self.http.get(prefix + '/tasks').json()
        self.assertEqual(tasks[0]['title'], '口语任务')
        units = self.http.get(prefix + '/tasks/task').json()
        self.assertEqual([u['id'] for u in units], ['unit1', 'unit2'])
        levels = self.http.get(prefix + '/tasks/task/units/unit1').json()
        self.assertEqual([l['id'] for l in levels], ['a', 'b'])
        detail = self.http.get(prefix + '/levels/a').json()
        self.assertTrue(detail['supported'])
        self.assertEqual(detail['groups'][0]['prompts'][0]['text'], 'thank')
        self.platform.children['a'].data['content']['moshi'][0]['showtype'] = 't12'
        detail = self.http.get(prefix + '/levels/a').json()
        self.assertFalse(detail['supported'])
        self.assertIn('t12', detail['reason'])

    def test_settings_are_shared_with_native_client(self):
        config = self.http.get('/api/settings').json()
        config['settings']['max_trials'] = '5'
        config['settings']['target_score'] = '88'
        config['ffmpeg'] = 'local-ffmpeg.exe'
        response = self.http.put('/api/settings', json=config)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads((self.root / 'speech.json').read_text())['max_trials'], '5')
        self.assertEqual(json.loads((self.root / 'web.json').read_text())['ffmpeg'], 'local-ffmpeg.exe')
        config['settings']['max_trials'] = '1000'
        self.assertEqual(self.http.put('/api/settings', json=config).status_code, 400)

    def test_pause_rejects_concurrent_writes_and_waits_for_save(self):
        started = Event()
        def runner(client, store, data_dir, cancel, progress):
            store.data['stage'] = 'running'
            store.save()
            progress('running')
            started.set()
            cancel.wait(5)
            store.data['stage'] = 'paused'
            store.save()
            raise Cancelled('已暂停')
        with patch('fif_oral.web.app.run_queue', side_effect=runner):
            first = self.start()
            self.assertEqual(first.status_code, 200, first.text)
            self.assertTrue(started.wait(2))
            self.assertEqual(self.start().status_code, 409)
            self.assertEqual(self.http.post('/api/lock').status_code, 409)
            self.assertEqual(self.http.delete('/api/accounts/' + self.account['id']).status_code, 409)
            identity = first.json()['id']
            self.assertEqual(self.http.post('/api/queues/' + identity + '/pause').status_code, 200)
            self.app.state.local.jobs[identity]['future'].result(timeout=3)
            view = self.http.get('/api/queues/' + identity).json()
            self.assertEqual(view['data']['stage'], 'paused')
            self.assertFalse(view['busy'])
            changed = self.http.put('/api/accounts/' + self.account['id'], json={'name': '新身份', 'username': 'new-user'})
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(self.http.get('/api/queues/' + identity).status_code, 404)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
    def test_http_batch_full_chain_resume_and_no_duplicate_submission(self):
        self.upload_voice()
        self.platform.children['a'].data = level()
        self.platform.children['c'].data = level()
        settings = fixture_settings(self.root)
        self.assertEqual(self.http.put('/api/settings', json={'settings': asdict_settings(settings), 'ffmpeg': shutil.which('ffmpeg')}).status_code, 200)
        FakeEngine.cancel_at = None
        def evaluation(auth, pcm, item, cancel):
            return REJECTED_WORD_XML if item.showtype == 't1' else XML
        with patch('fif_oral.audio.speech.SpeechEngine', FakeEngine), patch('fif_oral.challenge.evaluate', side_effect=evaluation), \
                patch('fif_oral.challenge.upload', side_effect=lambda client, profile, item, wav, xml, stamp, task: f'{stamp}/{item.index}.mp3'):
            response = self.start()
            self.assertEqual(response.status_code, 200, response.text)
            identity = response.json()['id']
            self.app.state.local.jobs[identity]['future'].result(timeout=6)
        view = self.http.get('/api/queues/' + identity).json()
        self.assertEqual([j['state'] for j in view['data']['jobs']], ['confirmed', 'review', 'confirmed'])
        self.assertEqual([len(p.submissions) for p in self.platform.children.values()], [1, 0, 1])
        self.assertEqual(len(self.http.get('/api/queues', params={'account_id': self.account['id']}).json()), 1)
        # Reopening the service reads the same persistent queue and confirmed per-level attempts.
        self.http.__exit__(None, None, None)
        self.app = web_app.create_app(self.root, allowed_hosts={'testserver'})
        self.http = TestClient(self.app, headers={'X-FIF-Client': '1'})
        self.http.__enter__()
        self.addCleanup(self.http.__exit__, None, None, None)
        self.http.post('/api/unlock', json={'password': 'test-master'})
        self.app.state.local.sessions[self.account['id']] = self.platform
        self.assertFalse(self.http.get('/api/queues/' + identity).json()['busy'])
        with patch('fif_oral.challenge.evaluate', side_effect=AssertionError('must not regrade accepted or identical rejected audio')), \
                patch('fif_oral.challenge.upload', side_effect=AssertionError('must not reupload')):
            resumed = self.http.post('/api/queues/' + identity + '/resume')
            self.assertEqual(resumed.status_code, 200, resumed.text)
            self.app.state.local.jobs[identity]['future'].result(timeout=6)
        self.assertEqual([len(p.submissions) for p in self.platform.children.values()], [1, 0, 1])

    def test_queue_ownership_and_missing_scope_are_checked(self):
        response = self.http.post('/api/queues', json={'account_id': self.account['id'], 'scope': {'kind': 'level', 'task_id': 'task'}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.http.get('/api/queues/' + 'a' * 64).status_code, 404)
        self.upload_voice()
        other = self.http.post('/api/accounts', json={'name': 'other', 'username': 'other'}).json()
        with patch('fif_oral.web.app.LoginFlow.sign_in', return_value=self.platform):
            self.http.post(f'/api/accounts/{other["id"]}/login', json={"password":"x"})
        response = self.http.post('/api/queues', json={'account_id': other['id'], 'scope': {'kind': 'task', 'task_id': 'task'}, 'voice_id': self.voice_id})
        self.assertEqual(response.status_code, 400)

    def test_environment_install_blocks_overlapping_queue_and_can_cancel(self):
        started = Event()
        def install(component, root, cancel, progress, **options):
            started.set()
            cancel.wait(5)
            raise Cancelled('cancelled')
        with patch('fif_oral.environment.manager.install_component', side_effect=install):
            result = self.http.post('/api/environment', json={'component': 'ffmpeg'})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertTrue(started.wait(2))
            self.assertEqual(self.start().status_code, 409)
            self.assertEqual(self.http.post('/api/lock').status_code, 409)
            self.assertEqual(self.http.post('/api/environment/cancel').status_code, 200)
            self.app.state.local.installer.future.result(timeout=3)
        self.assertEqual(self.http.get('/api/environment').json()['task']['stage'], 'paused')
        denied = self.http.post('/api/environment', json={'component': 'xtts_v2'})
        self.assertEqual(denied.status_code, 400)

    def test_three_tts_backends_round_trip_and_edge_voice_catalog(self):
        config = self.http.get('/api/settings').json()
        for backend in ('edge', 'your_tts', 'xtts_v2'):
            config['settings']['backend'] = backend
            response = self.http.put('/api/settings', json=config)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['settings']['backend'], backend)
        with patch('fif_oral.audio.edge.english_voices', return_value=[{'name': 'en-US-AriaNeural', 'locale': 'en-US', 'gender': 'Female'}]):
            self.assertEqual(self.http.get('/api/edge/voices').json()[0]['name'], 'en-US-AriaNeural')


def asdict_settings(settings):
    from dataclasses import asdict
    return asdict(settings)


if __name__ == '__main__':
    unittest.main()
