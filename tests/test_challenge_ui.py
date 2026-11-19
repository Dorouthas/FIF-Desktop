import os
import shutil
import tempfile
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

import fif_oral.challenge as challenge
from fif_oral.ui.challenge_dialog import ChallengeDialog
from fif_oral.ui.desktop import STYLE
from fif_oral.storage import Account, Voice
from test_speech import FakeEngine, fixture_settings
from test_speech import write_wav
from test_challenge import Platform, REJECTED_WORD_XML, XML, level, word_level
from fif_oral.ui.workbench import ROLE, Workbench


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is needed for conversion coverage")
class ChallengeUiTests(unittest.TestCase):
    def test_main_window_opens_challenge_and_shows_current_result(self):
        self.run_window("files")

    def test_automatic_generation_then_submit_from_main_window(self):
        self.run_window("speech")

    def test_rejection_is_displayed_as_rejection_instead_of_zero(self):
        self.run_window("speech", rejected=True)

    def test_dynamic_retry_runs_from_gui_and_displays_selected_profile(self):
        self.run_window("speech", dynamic=True)

    def test_system_words_work_without_importing_reference_voice(self):
        self.run_window("speech", system=True)

    def run_window(self, mode, *, rejected=False, dynamic=False, system=False):
        app = QApplication.instance() or QApplication([])
        font = Path("C:/Windows/Fonts/msyh.ttc")
        if not QFontDatabase.families() and font.is_file():
            QFontDatabase.addApplicationFont(str(font))
        app.setStyleSheet(STYLE)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            audio = root / "input.wav"
            write_wav(audio)
            vault = SimpleNamespace(path=root / "accounts.json", accounts=[], active="account")
            window = Workbench(vault)
            self.addCleanup(window.close)
            platform = Platform(word_level() if rejected or dynamic or system else level())
            voice = Voice("voice", "Account voice", "voice.wav", challenge.file_hash(audio))
            vault.accounts = [Account(id="account", name="UI test", voices=[voice], default_voice=voice.id)]
            vault.voice_path = lambda selected: audio
            settings = fixture_settings(root)
            if dynamic:
                settings = replace(settings, adaptive='on')
            if system:
                settings = replace(settings, word_mode='system', python='', your_tts='')
                vault.accounts[0].voices = []
                vault.accounts[0].default_voice = ''
            settings.save(root)

            class System:
                voice_id = 'test-system-English'

                def __init__(self, cancel):
                    pass

                def synthesize(self, text, target, cancel):
                    shutil.copyfile(audio, target)
            window.sessions["account"] = platform
            unit = window._item(window.tree, "unit", "Course", "unit", "task", {})
            node = window._item(unit, "level", "Reading", "level", "task", {"levelScore": 91})
            node.data(0, ROLE).detail = platform.data
            window.tree.setCurrentItem(node)
            window.show()
            app.processEvents()
            outcome = {}
            poll = QTimer(interval=10)

            def drive_dialog():
                dialog = app.activeModalWidget()
                if not isinstance(dialog, ChallengeDialog):
                    return
                if not outcome.get("started"):
                    outcome["started"] = True
                    dialog.mode.setCurrentIndex(dialog.mode.findData(mode))
                    if mode == "files":
                        dialog.choose_buttons[0].click()
                    dialog.start.click()
                elif dialog.future is None:
                    outcome["result"] = dialog.result_data
                    outcome["status"] = dialog.status.text()
                    outcome["cell"] = dialog.table.item(0, 2).text()
                    outcome['profile'] = dialog.store.data.get('evaluations', {}).get('question#0#0', {}).get('profile')
                    poll.stop()
                    dialog.reject()

            def timeout():
                poll.stop()
                dialog = app.activeModalWidget()
                if dialog:
                    outcome["timeout"] = dialog.status.text()
                    dialog.reject()

            watchdog = QTimer(singleShot=True, interval=5000)
            watchdog.timeout.connect(timeout)
            poll.timeout.connect(drive_dialog)
            poll.start()
            watchdog.start()
            button = next(button for button in window.findChildren(QPushButton) if button.text() == "音频挑战")
            with patch("fif_oral.ui.challenge_dialog.QFileDialog.getOpenFileName", return_value=(str(audio), "")), \
                    patch("fif_oral.audio.speech.SpeechEngine", FakeEngine), \
                    patch('fif_oral.audio.speech.SystemSpeech', System), \
                    patch.object(challenge, "evaluate", side_effect=([REJECTED_WORD_XML,
                         REJECTED_WORD_XML.replace(b'is_rejected="true"', b'is_rejected="false"').replace(b'28676', b'0')]
                         if dynamic else None), return_value=REJECTED_WORD_XML if rejected else XML), \
                    patch.object(challenge, "upload", return_value="our/0.mp3"):
                button.click()
            watchdog.stop()
            poll.stop()
            self.assertNotIn("timeout", outcome, outcome)
            if rejected:
                self.assertIsNone(outcome["result"])
                self.assertEqual(outcome["cell"], "待复核")
                self.assertIn("28676", outcome["status"])
                self.assertFalse(platform.submissions)
                window.close()
                app.processEvents()
                return
            self.assertEqual(outcome["result"]["score"], 72, outcome)
            self.assertEqual(len(platform.submissions), 1)
            if dynamic:
                self.assertEqual(outcome['profile'], 'your_tts-seed-17-speed-1.0')
                self.assertEqual(outcome['cell'], '84')
            self.assertEqual(node.data(0, ROLE).summary["levelScore"], 91)
            texts = [label.text() for label in window.detail.findChildren(QLabel)]
            self.assertIn("91 / 100", texts)
            self.assertIn("本次挑战  72 / 100", texts)
            window.close()
            app.processEvents()


if __name__ == "__main__":
    unittest.main()
