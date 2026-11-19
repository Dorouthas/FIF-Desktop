import hashlib
import io
import json
import shutil
import stat
import tempfile
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from urllib.request import Request, build_opener as real_opener
from unittest.mock import patch

import fif_oral.environment.manager as setup
from fif_oral.audio.assessment import Cancelled
from fif_oral.api.client import ApiError
from fif_oral.audio.speech import MODEL_FILES, SpeechSettings


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                server = self.server
                value = self.headers.get('Range')
                server.requests.append(value)
                offset = int(value.partition('=')[2].partition('-')[0]) if value else 0
                partial = bool(value and not server.ignore_range)
                if not partial:
                    offset = 0
                self.send_response(206 if partial else 200)
                if partial:
                    self.send_header('Content-Range', f'bytes {offset + int(server.wrong_range)}-{len(server.payload)-1}/{len(server.payload)}')
                self.send_header('Content-Length', str(len(server.payload) - offset))
                self.end_headers()
                data = server.payload[offset:]
                if server.truncate_once:
                    server.truncate_once = False
                    data = data[:200000]
                    self.close_connection = True
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / 'resource.bin'
        self.cancel = Event()
        self.server.payload = b'fixture-download-' * 45000
        self.server.requests = []
        self.server.ignore_range = False
        self.server.wrong_range = False
        self.server.truncate_once = False
        self.spec = {'url': 'https://fixtures.invalid/resource', 'size': len(self.server.payload),
                     'sha256': hashlib.sha256(self.server.payload).hexdigest()}
        address = self.server.server_address
        class Transport:
            def open(transport, request, timeout):
                # Real HTTP streaming/ranges to a local server; production still requires HTTPS.
                return real_opener().open(Request(f'http://{address[0]}:{address[1]}/resource',
                    headers=dict(request.header_items())), timeout=timeout)
        mocked = patch.object(setup, 'build_opener', return_value=Transport())
        mocked.start()
        self.addCleanup(mocked.stop)

    def download(self, progress=lambda **values: None):
        return setup.download(self.spec, self.target, self.cancel, progress)

    def seed_partial(self):
        self.target.with_name('resource.bin.part').write_bytes(self.server.payload[:100000])
        self.target.with_name('resource.bin.part.json').write_text(json.dumps(self.spec), encoding='utf-8')

    def test_cancel_then_resume_verifies_file_and_reuses_complete_cache(self):
        def stop(**values):
            if values.get('received', 0):
                self.cancel.set()
        with self.assertRaises(Cancelled):
            self.download(stop)
        partial = self.target.with_name('resource.bin.part')
        offset = partial.stat().st_size
        self.assertGreater(offset, 0)
        self.assertFalse(self.target.exists())
        self.cancel.clear()
        self.download()
        self.assertIn(f'bytes={offset}-', self.server.requests)
        self.assertEqual(self.target.read_bytes(), self.server.payload)
        count = len(self.server.requests)
        self.download()
        self.assertEqual(len(self.server.requests), count)
        self.assertFalse(partial.exists())

    def test_range_ignored_restarts_without_appending_duplicate_bytes(self):
        self.seed_partial()
        self.server.ignore_range = True
        self.download()
        self.assertEqual(self.target.read_bytes(), self.server.payload)

    def test_wrong_range_and_hash_do_not_replace_previous_file(self):
        self.target.write_bytes(b'previous working file')
        self.seed_partial()
        self.server.wrong_range = True
        with self.assertRaisesRegex(ApiError, '续传'):
            self.download()
        self.assertEqual(self.target.read_bytes(), b'previous working file')
        self.server.wrong_range = False
        self.spec['sha256'] = '0' * 64
        with self.assertRaisesRegex(ApiError, '校验失败'):
            self.download()
        self.assertEqual(self.target.read_bytes(), b'previous working file')
        self.assertFalse(self.target.with_name('resource.bin.part').exists())

    def test_truncated_connection_retries_remaining_bytes(self):
        self.server.truncate_once = True
        self.download()
        self.assertEqual(self.target.read_bytes(), self.server.payload)
        self.assertEqual(self.server.requests, [None, 'bytes=200000-'])

    def test_plain_http_is_rejected(self):
        self.spec['url'] = 'http://not-allowed.invalid/resource'
        with self.assertRaisesRegex(ApiError, 'HTTPS'):
            self.download()
        self.assertEqual(self.server.requests, [])


class InstallationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cancel = Event()
        self.messages = []

    def emit(self, **values):
        self.messages.append(values)

    def archive(self, entries):
        path = self.root / 'test.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, value in entries:
                archive.writestr(name, value)
        return path

    def test_archive_rejects_traversal_symlinks_and_duplicate_names(self):
        link = zipfile.ZipInfo('root/model.pth')
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for entries in ([('../escape', b'x'), ('root/model.pth', b'x')],
                        [(link, b'/escape')], [('one/model.pth', b'x'), ('two/model.pth', b'x')]):
            with self.subTest(entries=entries):
                with self.assertRaises(ApiError):
                    setup.extract_selected(self.archive(entries), self.root / 'output', ['model.pth'], self.cancel)
        self.assertFalse((self.root / 'escape').exists())

    def model(self):
        rows = []
        entries = []
        for name in MODEL_FILES['your_tts']:
            data = ('fixture:' + name).encode()
            entries.append(('folder/' + name, data))
            rows.append({'name': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        return {'name': 'Fixture model', 'version': 'test', 'license': 'Fixture only',
                'license_url': 'https://fixtures.invalid/license', 'url': 'https://fixtures.invalid/model.zip', 'files': rows}, self.archive(entries)

    def test_model_requires_consent_and_only_updates_path_after_verification(self):
        model, archive = self.model()
        with patch.dict(setup.MANIFEST['models'], your_tts=model), patch.object(setup, 'download', return_value=archive) as fetch:
            with self.assertRaisesRegex(ApiError, '许可'):
                setup.install_component('your_tts', self.root, self.cancel, self.emit)
            fetch.assert_not_called()
            original = SpeechSettings(backend='xtts_v2', your_tts='previous-model')
            original.save(self.root)
            with patch('fif_oral.environment.manager.platform.system', return_value='Windows'), patch('fif_oral.environment.manager.platform.machine', return_value='AMD64'):
                result = setup.install_component('your_tts', self.root, self.cancel, self.emit, accept_license=True)
        updated = SpeechSettings.load(self.root)
        self.assertEqual(updated.backend, original.backend)
        self.assertEqual(updated.your_tts, result['your_tts'])
        self.assertTrue(setup.verify_model(Path(result['your_tts']), model, self.cancel))
        self.assertTrue((self.root / 'model-licenses' / 'your_tts.json').is_file())

    def test_bad_model_does_not_replace_configuration(self):
        model, archive = self.model()
        model['files'][0]['sha256'] = '0' * 64
        SpeechSettings(your_tts='previous-model').save(self.root)
        with patch.dict(setup.MANIFEST['models'], your_tts=model), patch.object(setup, 'download', return_value=archive), \
                patch('fif_oral.environment.manager.platform.system', return_value='Windows'), patch('fif_oral.environment.manager.platform.machine', return_value='AMD64'):
            with self.assertRaisesRegex(ApiError, '校验不符'):
                setup.install_component('your_tts', self.root, self.cancel, self.emit, accept_license=True)
        self.assertEqual(SpeechSettings.load(self.root).your_tts, 'previous-model')
        self.assertFalse((self.root / 'models' / 'your_tts').exists())

    def test_existing_verified_model_is_reused_without_download(self):
        model, archive = self.model()
        folder = self.root / 'existing'
        setup.extract_selected(archive, folder, MODEL_FILES['your_tts'], self.cancel)
        SpeechSettings(your_tts=str(folder)).save(self.root)
        with patch.dict(setup.MANIFEST['models'], your_tts=model), patch.object(setup, 'download') as fetch, \
                patch('fif_oral.environment.manager.platform.system', return_value='Windows'), patch('fif_oral.environment.manager.platform.machine', return_value='AMD64'):
            result = setup.install_component('your_tts', self.root, self.cancel, self.emit, accept_license=True)
        fetch.assert_not_called()
        self.assertEqual(Path(result['your_tts']).resolve(), folder.resolve())

    def test_runtime_install_failure_preserves_previous_interpreter(self):
        SpeechSettings(python='previous-python').save(self.root)
        with patch.object(setup, 'probe_runtime', side_effect=ApiError('missing')), \
                patch.object(setup.shutil, 'which', return_value='uv.exe'), \
                patch.object(setup, 'run_command', side_effect=ApiError('pip failed')), \
                patch('fif_oral.environment.manager.platform.system', return_value='Windows'), patch('fif_oral.environment.manager.platform.machine', return_value='AMD64'):
            with self.assertRaisesRegex(ApiError, 'pip failed'):
                setup.install_component('runtime', self.root, self.cancel, self.emit)
        self.assertEqual(SpeechSettings.load(self.root).python, 'previous-python')
        self.assertFalse((self.root / 'runtime-ready.json').exists())

    def test_manager_rejects_overlap_and_cancels_worker(self):
        manager = setup.InstallationManager(self.root)
        started = Event()
        def run(component, root, cancel, progress, **options):
            started.set()
            cancel.wait(3)
            raise Cancelled('cancelled')
        try:
            with patch.object(setup, 'install_component', side_effect=run):
                manager.start('ffmpeg')
                self.assertTrue(started.wait(1))
                with self.assertRaisesRegex(ApiError, '正在运行'):
                    manager.start('runtime')
                manager.close()
            self.assertEqual(manager.snapshot()['stage'], 'paused')
        finally:
            manager.close()


if __name__ == '__main__':
    unittest.main()
