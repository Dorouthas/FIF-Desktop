import os
import shutil
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from fif_oral.audio.assessment import challenge_items
from fif_oral.challenge import file_hash
from fif_oral.ui.challenge_dialog import ChallengeDialog
from fif_oral.ui.desktop import STYLE
from fif_oral.storage import Account, Voice
from test_candidates import result_xml, words
from test_challenge import Platform
from test_speech import FakeEngine, fixture_settings, write_wav


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
class CandidateUiTests(unittest.TestCase):
    def test_review_budget_can_be_changed_without_redoing_accepted_question(self):
        app = QApplication.instance() or QApplication([])
        font = Path('C:/Windows/Fonts/msyh.ttc')
        if not QFontDatabase.families() and font.is_file():
            QFontDatabase.addApplicationFont(str(font))
        app.setStyleSheet(STYLE)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            audio = root / 'voice.wav'
            write_wav(audio)
            voice = Voice('voice', 'Reference', 'voice.wav', file_hash(audio))
            account = Account(id='account', name='Test', voices=[voice], default_voice=voice.id)
            vault = SimpleNamespace(voice_path=lambda selected: audio)
            preview = SimpleNamespace(stop=lambda: None, play=lambda url: None)
            settings = replace(fixture_settings(root), adaptive='on', max_trials='1')
            settings.save(root)
            data = words('bad', 'good')
            platform = Platform(data)
            items = challenge_items(data)
            dialog = ChallengeDialog(platform, 'account', 'task', 'unit', 'level', 'Test', items, root,
                                     preview, account=account, vault=vault)
            self.addCleanup(dialog.reject)
            dialog.show()
            app.processEvents()
            phase = [1]
            assessed, uploaded = [], []

            def evaluate(auth, pcm, item, cancel):
                assessed.append(item.prompt.text)
                return result_xml(item.prompt.text, phase[0] == 1 and item.prompt.text == 'bad')

            def wait():
                deadline = time.monotonic() + 6
                while dialog.future is not None and time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(0.01)
                self.assertIsNone(dialog.future, dialog.status.text())

            with patch('fif_oral.audio.speech.SpeechEngine', FakeEngine), patch('fif_oral.challenge.evaluate', side_effect=evaluate), \
                    patch('fif_oral.challenge.upload', side_effect=lambda *a: uploaded.append(a) or f'{len(uploaded)}.mp3'):
                dialog.start.click()
                wait()
                self.assertEqual(assessed, ['bad', 'good'])
                self.assertFalse(uploaded)
                self.assertEqual(dialog.table.item(0, 2).text(), '待复核')
                self.assertEqual(dialog.table.item(1, 2).text(), '84')
                self.assertEqual(dialog.start.text(), '重试待复核题')
                self.assertTrue(dialog.model_button.isEnabled())
                original_files = list(dialog.store.data['files'])

                def increase_budget():
                    editor = app.activeModalWidget()
                    editor.trials.setValue(2)
                    editor._save()

                QTimer.singleShot(0, increase_budget)
                dialog.model_button.click()
                self.assertEqual(dialog.store.data['source']['strategy']['max_trials'], '2')
                self.assertEqual(len(dialog.store.data['strategy_revisions']), 1)
                self.assertEqual(dialog.store.data['files'], original_files)
                phase[0] = 2
                dialog.start.click()
                wait()
                self.assertEqual(assessed, ['bad', 'good', 'bad'])
                self.assertEqual(len(uploaded), 2)
                self.assertEqual(len(platform.submissions), 1)
                self.assertEqual(dialog.result_data['score'], 72)
            dialog.reject()
            app.processEvents()


if __name__ == '__main__':
    unittest.main()
