import tempfile
import unittest
from pathlib import Path

from scripts.package_release import ROOT_FILES, release_files


class ReleasePackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for name in ROOT_FILES:
            (self.root / name).write_text('fixture', encoding='utf-8')

    def write(self, name, content='fixture'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')

    def test_source_package_excludes_runtime_data_and_keeps_optional_engines(self):
        expected = {'src/fif_oral/ui/desktop.py', 'src/fif_oral/audio/edge.py', 'src/fif_oral/audio/speech_worker.py',
                    'src/fif_oral/resources/requirements-tts.lock.txt', 'src/fif_oral/web/static/app.js',
                    'src/fif_oral/resources/assets/check.svg', 'src/fif_oral/environment/doctor.py', 'docs/DEVELOPMENT.md'}
        forbidden = {'data/accounts.json', 'data/voices/speaker.py', '.venv/lib.py',
                     'tests/__pycache__/cache.py', 'src/fif_oral/web/static/profile/session.json',
                     'src/fif_oral_desktop.egg-info/PKG-INFO', 'secrets.json', 'private-script.py'}
        for name in expected | forbidden:
            self.write(name)
        paths = {p.relative_to(self.root).as_posix() for p in release_files(self.root)}
        self.assertTrue(expected <= paths)
        self.assertFalse(forbidden & paths)

    def test_credential_literal_blocks_archive_before_creation(self):
        self.write('src/fif_oral/__init__.py', 'token = "ghp_' + 'x' * 30 + '"')
        with self.assertRaisesRegex(ValueError, '凭据'):
            release_files(self.root)

    def test_personal_machine_path_is_rejected(self):
        self.write('docs/debug.md', 'C:' + '/Users/' + 'fixture-user/private.wav')
        with self.assertRaisesRegex(ValueError, '个人路径'):
            release_files(self.root)


if __name__ == '__main__':
    unittest.main()
