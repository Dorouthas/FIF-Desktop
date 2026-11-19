import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QDialog

from fif_oral.ui.desktop import STYLE
from fif_oral.ui.setup_dialog import SetupDialog
from fif_oral.audio.speech import MODEL_FILES, SpeechSettings
from fif_oral.ui.speech_dialog import SpeechSettingsDialog
from test_speech import fixture_settings


class SpeechUiTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        if not QFontDatabase.families():
            QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
        self.app.setStyleSheet(STYLE)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = fixture_settings(self.root)
        xtts = self.root / 'xtts'
        xtts.mkdir()
        for name in MODEL_FILES['xtts_v2']:
            (xtts / name).write_bytes(b'fixture model')
        self.settings = replace(self.settings, xtts_v2=str(xtts))

    def test_all_three_choices_preserve_both_local_paths_on_save(self):
        current = self.settings
        for backend in ('edge', 'your_tts', 'xtts_v2'):
            with self.subTest(backend=backend):
                dialog = SpeechSettingsDialog(current, self.root)
                self.assertEqual([dialog.backend.itemData(i) for i in range(dialog.backend.count())],
                                 ['edge', 'your_tts', 'xtts_v2'])
                dialog.backend.setCurrentIndex(dialog.backend.findData(backend))
                self.assertEqual(dialog.directory.isEnabled(), backend != 'edge')
                self.assertEqual(dialog.edge_voice.isEnabled(), backend == 'edge')
                dialog._save()
                self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted, dialog.error.text())
                current = SpeechSettings.load(self.root)
                self.assertEqual(current.backend, backend)
                self.assertEqual((current.your_tts, current.xtts_v2),
                                 (self.settings.your_tts, self.settings.xtts_v2))
                dialog.deleteLater()

    def test_environment_requires_model_license_before_starting_download(self):
        self.settings.save(self.root)
        dialog = SetupDialog(self.root)
        try:
            self.assertTrue({'edge', 'ffmpeg', 'runtime', 'your_tts', 'xtts_v2'} <= dialog.labels.keys())
            dialog._start('xtts_v2')
            self.assertIn('许可', dialog.status.text())
            self.assertIsNone(dialog.manager.future)
            capture = os.environ.get('FIF_SETUP_CAPTURE')
            if capture:
                Path(capture).parent.mkdir(parents=True, exist_ok=True)
                dialog.show()
                self.app.processEvents()
                self.assertTrue(dialog.grab().save(capture))
        finally:
            dialog.reject()


if __name__ == '__main__':
    unittest.main()
