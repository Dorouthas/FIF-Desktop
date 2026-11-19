import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fif_oral import paths


class PackagePathsTests(unittest.TestCase):
    def test_bundled_resources_exist(self):
        for name in ('dependency-manifest.json', 'requirements-tts.lock.txt', 'assets/check.svg'):
            self.assertTrue((paths.RESOURCE_ROOT / name).is_file(), name)
        self.assertTrue((paths.PACKAGE_ROOT / 'web/static/index.html').is_file())

    def test_explicit_data_directory_takes_precedence(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, FIF_ORAL_DATA_DIR=folder):
            self.assertEqual(paths.default_data_dir(), Path(folder).resolve())

    def test_wheel_install_uses_writable_user_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(paths, 'PACKAGE_ROOT', root / 'site-packages/fif_oral'), \
                    patch.dict(os.environ, {'LOCALAPPDATA': str(root / 'user'), 'FIF_ORAL_DATA_DIR': ''}):
                self.assertEqual(paths.default_data_dir(), root / 'user/FIF-Oral/data')
