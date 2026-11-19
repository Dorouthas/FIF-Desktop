import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

import fif_oral.challenge as challenge
from fif_oral.audio.assessment import Cancelled
from fif_oral.automation import AutomationScope, enumerate_levels
from fif_oral.ui.automation_dialog import AutomationDialog
from fif_oral.api.client import FiFClient
from fif_oral.ui.desktop import STYLE
from fif_oral.storage import Account, Voice
from test_automation import Directory
from test_challenge import XML
from test_speech import FakeEngine, fixture_settings, write_wav
from fif_oral.ui.workbench import ROLE, Workbench


class AutomationUiTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        if not QFontDatabase.families():
            QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
        self.app.setStyleSheet(STYLE)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        audio = self.root / 'voice.wav'
        write_wav(audio)
        voice = Voice('voice', '测试参考音色', 'voice.wav', challenge.file_hash(audio))
        account = Account(id='account', name='功能测试账号', voices=[voice], default_voice=voice.id)
        self.vault = SimpleNamespace(path=self.root / 'accounts.json', accounts=[account], active=account.id,
                                     voice_path=lambda selected: audio)
        self.settings = fixture_settings(self.root)
        self.settings.save(self.root)
        self.client = Directory()
        self.window = Workbench(self.vault)
        self.window.sessions[account.id] = self.client
        self.addCleanup(self.window.close)
        self.task = self.window._item(self.window.tree, 'task', '口语任务', 'task', 'task', {})
        self.unit = self.window._item(self.task, 'unit', '语音课程', 'unit1', 'task', {})
        self.level = self.window._item(self.unit, 'level', '单词关卡', 'a', 'task', {})
        self.window.show()

    def test_every_tree_level_has_correct_automatic_action(self):
        self.assertEqual(self.window.tree.contextMenuPolicy(), Qt.ContextMenuPolicy.CustomContextMenu)
        with patch.object(self.window, '_automate') as automate:
            for item, kind, unit, level in ((self.task, 'task', '', ''), (self.unit, 'unit', 'unit1', ''),
                                             (self.level, 'level', 'unit1', 'a')):
                menu = self.window._automation_menu(item)
                self.assertEqual([action.text() for action in menu.actions()], ['全自动完成'])
                menu.actions()[0].trigger()
                scope = automate.call_args.args[0]
                self.assertEqual((scope.kind, scope.task_id, scope.unit_id, scope.level_id), (kind, 'task', unit, level))
                menu.deleteLater()

    def test_real_unexpired_client_opens_queue_without_relogin(self):
        client = FiFClient('fixture-token', '10003', 'student', 'Test student', time.time() + 3600)
        self.window.sessions[self.vault.active] = client
        with patch.object(self.window, 'login') as login, patch('fif_oral.ui.workbench.AutomationDialog') as factory:
            dialog = factory.return_value
            dialog.store.data = {'jobs': []}
            dialog.status.text.return_value = '已结束'
            self.window._automate(self.window._scope(self.task))
        login.assert_not_called()
        factory.assert_called_once()

    def test_real_unexpired_client_starts_queue_dialog(self):
        client = FiFClient('fixture-token', '10003', 'student', 'Test student', time.time() + 3600)
        dialog = AutomationDialog(client, self.vault.accounts[0], AutomationScope('task', 'task', title='有效会话'),
                                  self.vault, self.window.preview, self.window)
        def run(client, store, *args):
            store.data['stage'] = 'complete'
            store.save()
            return {}
        try:
            with patch('fif_oral.ui.automation_dialog.run_queue', side_effect=run) as runner:
                dialog._begin()
                self.assertIsNotNone(dialog.future, dialog.status.text())
                dialog.future.result(timeout=3)
                dialog._poll()
                runner.assert_called_once()
        finally:
            dialog.reject()

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
    def test_right_click_task_runs_unexpanded_catalog_and_updates_scores(self):
        outcome = {}
        poll = QTimer(interval=10)
        def drive():
            dialog = self.app.activeModalWidget()
            if not isinstance(dialog, AutomationDialog):
                return
            if dialog.future is None and dialog.store.data['stage'] in {'complete', 'review', 'error'}:
                outcome.update(stage=dialog.store.data['stage'], jobs=list(dialog.store.data['jobs']),
                               status=dialog.status.text(), rows=dialog.table.rowCount())
                capture = os.environ.get('FIF_UI_CAPTURE')
                if capture:
                    target = Path(capture)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    dialog.grab().save(str(target))
                poll.stop()
                dialog.reject()
        def timeout():
            outcome['timeout'] = True
            dialog = self.app.activeModalWidget()
            if dialog:
                outcome['status'] = dialog.status.text()
                dialog.reject()
        watchdog = QTimer(singleShot=True, interval=8000)
        watchdog.timeout.connect(timeout)
        poll.timeout.connect(drive)
        poll.start()
        watchdog.start()
        # All actual audio generation, conversion, attempt persistence and report matching execute;
        # only the TTS model and remote evaluation/upload endpoints are fixtures.
        with patch('fif_oral.audio.speech.SpeechEngine', FakeEngine), patch.object(challenge, 'evaluate', return_value=XML), \
                patch.object(challenge, 'upload', side_effect=lambda client, profile, item, wav, xml, stamp, task:
                             f'{stamp}/{item.group}_{item.index}.mp3'):
            menu = self.window._automation_menu(self.task)
            menu.actions()[0].trigger()
            menu.deleteLater()
        poll.stop()
        watchdog.stop()
        self.assertNotIn('timeout', outcome, outcome)
        self.assertEqual((outcome['stage'], outcome['rows']), ('complete', 3), outcome)
        self.assertEqual([job['state'] for job in outcome['jobs']], ['confirmed'] * 3)
        self.assertEqual(self.level.data(0, ROLE).last_result['score'], 72)
        self.assertEqual([len(child.submissions) for child in self.client.children.values()], [1, 1, 1])

    def test_close_waits_for_worker_to_save_pause(self):
        dialog = AutomationDialog(self.client, self.vault.accounts[0], AutomationScope('task', 'task', title='暂停测试'),
                                  self.vault, self.window.preview, self.window)
        self.addCleanup(dialog.deleteLater)
        outcome = {}
        def runner(client, store, root, cancel, progress):
            jobs, issues = enumerate_levels(client, store.scope, cancel, progress)
            store.merge(jobs, issues, complete=True)
            store.data['stage'] = 'running'
            store.save()
            progress('worker running')
            cancel.wait(3)
            store.data['stage'] = 'paused'
            store.save()
            raise Cancelled('已暂停')
        poll = QTimer(interval=10)
        def drive():
            if dialog.future and dialog.store.data['stage'] == 'running' and not outcome:
                dialog.reject()
                outcome['waited'] = dialog.future is not None and dialog.isVisible()
        poll.timeout.connect(drive)
        poll.start()
        watchdog = QTimer(singleShot=True, interval=5000)
        watchdog.timeout.connect(dialog.reject)
        watchdog.start()
        QTimer.singleShot(0, dialog._begin)
        with patch('fif_oral.ui.automation_dialog.run_queue', side_effect=runner):
            dialog.exec()
        poll.stop()
        watchdog.stop()
        self.assertTrue(outcome.get('waited'))
        self.assertIsNone(dialog.future)
        self.assertEqual(dialog.store.data['stage'], 'paused')
        self.assertTrue(dialog.store.path.is_file())


if __name__ == '__main__':
    unittest.main()
