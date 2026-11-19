"""Native UI for account-bound automatic task queues."""
import queue
import shutil
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Event

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from fif_oral.audio.assessment import Cancelled, challenge_items
from fif_oral.automation import AutomationStore, STATUS, job_key, run_queue
from fif_oral.ui.challenge_dialog import ChallengeDialog
from fif_oral.api.client import ApiError, SessionExpired
from fif_oral.ui.login import LoginDialog
from fif_oral.audio.speech import MODEL_FILES, BACKEND_NAMES, SpeechSettings
from fif_oral.ui.speech_dialog import SpeechSettingsDialog
from fif_oral.storage import VaultError
from fif_oral.environment.manager import default_ffmpeg


class AutomationDialog(QDialog):
    def __init__(self, client, account, scope, vault, preview, parent=None, *, on_connected=None):
        super().__init__(parent)
        self.setWindowTitle(f'全自动完成 · {scope.title}')
        self.resize(1000, 760)
        self.client, self.account, self.vault, self.preview = client, account, vault, preview
        self.on_connected = on_connected
        self.data_dir = vault.path.parent
        self.store = AutomationStore(self.data_dir / 'queues', account.id, client.user_id, scope)
        self.settings = (SpeechSettings(**self.store.data['settings']) if 'settings' in self.store.data
                         else SpeechSettings.load(self.data_dir))
        self.future = None
        self.operation = None
        self.cancel = Event()
        self.messages = queue.Queue()
        self.close_result = None
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='fif-automation')
        self.timer = QTimer(self, interval=80)
        self.timer.timeout.connect(self._poll)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)
        heading = QLabel(scope.title, textFormat=Qt.TextFormat.PlainText, objectName='title')
        layout.addWidget(heading)
        layout.addWidget(QLabel(f'{account.name} · {client.name} · 串行执行，进度自动保存',
                               textFormat=Qt.TextFormat.PlainText, objectName='muted'))
        note = QLabel('自动查询完整目录，逐关生成音频、评测、上传并核对报告。待复核和失败的关卡会保留进度，其他关卡继续执行。')
        note.setWordWrap(True)
        layout.addWidget(note)
        settings = QHBoxLayout()
        settings.addWidget(QLabel('参考音色'))
        self.voices = QComboBox()
        self.voices.addItem('不使用参考音色（Edge / 系统语音）', None)
        for voice in account.voices:
            self.voices.addItem(voice.name, voice.id)
        selected = account.default_voice
        if 'voice' in self.store.data:
            selected = None
            for voice in account.voices:
                if self.store.data['voice'] and str(vault.voice_path(voice).resolve()) == self.store.data['voice']:
                    selected = voice.id
        self.voices.setCurrentIndex(max(0, self.voices.findData(selected)))
        settings.addWidget(self.voices, 1)
        self.models = QPushButton(f'模型设置 · {BACKEND_NAMES[self.settings.backend]}')
        self.models.clicked.connect(self._models)
        settings.addWidget(self.models)
        layout.addLayout(settings)
        tools = QHBoxLayout()
        tools.addWidget(QLabel('FFmpeg'))
        saved_ffmpeg = self.store.data.get('ffmpeg', '')
        self.ffmpeg = QLineEdit(saved_ffmpeg if saved_ffmpeg and Path(saved_ffmpeg).is_file() else default_ffmpeg(self.data_dir))
        tools.addWidget(self.ffmpeg, 1)
        self.browse = QPushButton('浏览')
        self.browse.clicked.connect(self._ffmpeg)
        tools.addWidget(self.browse)
        layout.addLayout(tools)
        self.skip = QCheckBox('跳过平台分数已达目标的关卡')
        self.skip.setChecked(self.store.data.get('skip_qualified', True))
        self.skip.setToolTip('按模型设置中的目标分数判断；已核实的本地提交始终保留，避免重复提交。')
        layout.addWidget(self.skip)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['课程', '关卡', '状态', '分数'])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        for column in range(4):
            self.table.horizontalHeader().setSectionResizeMode(column,
                QHeaderView.ResizeMode.Stretch if column < 2 else QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._selection)
        layout.addWidget(self.table, 1)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.detail = QLabel('', wordWrap=True, textFormat=Qt.TextFormat.PlainText, objectName='muted')
        layout.addWidget(self.detail)
        self.log = QPlainTextEdit(readOnly=True)
        self.log.setMaximumBlockCount(400)
        self.log.setMaximumHeight(100)
        layout.addWidget(self.log)
        self.status = QLabel('', wordWrap=True, textFormat=Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        controls = QHBoxLayout()
        self.inspect = QPushButton('打开所选关卡')
        self.inspect.clicked.connect(self._inspect)
        controls.addWidget(self.inspect)
        self.login = QPushButton('重新登录')
        self.login.clicked.connect(self._login)
        controls.addWidget(self.login)
        controls.addStretch()
        self.pause = QPushButton('暂停')
        self.pause.clicked.connect(self._pause)
        controls.addWidget(self.pause)
        self.start = QPushButton('开始全自动完成', objectName='primary')
        self.start.setMinimumWidth(182)
        self.start.clicked.connect(self._begin)
        controls.addWidget(self.start)
        close = QPushButton('关闭')
        close.clicked.connect(self.reject)
        controls.addWidget(close)
        layout.addLayout(controls)
        self.snapshot = deepcopy(self.store.data)
        self._render(self.snapshot)
        self._idle()
        self.status.setText('参考音色和模型可在上方调整；音色文件请在主窗口的账号管理中导入。')

    def _models(self):
        dialog = SpeechSettingsDialog(self.settings, self.data_dir, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.settings = dialog.settings
            self.models.setText(f'模型设置 · {BACKEND_NAMES[self.settings.backend]}')
        dialog.deleteLater()

    def _ffmpeg(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择 FFmpeg', self.ffmpeg.text(), 'FFmpeg (ffmpeg.exe);;所有文件 (*)')
        if path:
            self.ffmpeg.setText(path)

    def _busy(self):
        for widget in (self.start, self.voices, self.models, self.ffmpeg, self.browse, self.skip, self.login, self.inspect):
            widget.setEnabled(False)
        self.pause.setEnabled(self.operation == 'run')

    def _idle(self):
        for widget in (self.start, self.voices, self.models, self.ffmpeg, self.browse, self.skip, self.login):
            widget.setEnabled(True)
        self.pause.setEnabled(False)
        self.start.setText('继续 / 重试未完成关卡' if self.store.data['jobs'] else '开始全自动完成')
        self._selection()

    def _begin(self):
        if self.future:
            return
        if self.client.expired() or self.store.data['stage'] == 'needs_login':
            self.status.setText('会话已过期，请重新登录后继续。')
            return
        try:
            self.settings.validate(neural=False)
            voice_id = self.voices.currentData()
            voice = next((voice for voice in self.account.voices if voice.id == voice_id), None)
            requires_reference = self.settings.backend in MODEL_FILES or self.settings.fallback_model != 'off'
            path = self.vault.voice_path(voice) if voice and requires_reference else None
            self.store.configure(self.settings, path, self.ffmpeg.text().strip(), self.skip.isChecked())
        except (ApiError, VaultError, OSError) as error:
            self.status.setText(str(error))
            return
        self.cancel.clear()
        self.close_result = None
        self.operation = 'run'
        self._busy()
        self.status.setText('正在启动自动队列…')
        self.future = self.pool.submit(run_queue, self.client, self.store, self.data_dir, self.cancel,
            lambda text: self.messages.put((text, deepcopy(self.store.data))))
        self.timer.start()

    def _pause(self):
        self.cancel.set()
        self.pause.setEnabled(False)
        self.status.setText('正在暂停并保存进度；正在进行的提交会先完成状态记录。')

    def _render(self, data):
        self.snapshot = data
        current = self.table.currentRow()
        self.table.setRowCount(len(data['jobs']))
        for row, job in enumerate(data['jobs']):
            score = job.get('score')
            values = (job['unit_name'] or '当前课程', job['name'], STATUS.get(job['state'], job['state']),
                      f'{score:g}' if isinstance(score, (int, float)) else '—')
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(job.get('message', ''))
                self.table.setItem(row, column, item)
        if data['jobs']:
            self.table.setCurrentCell(min(max(0, current), len(data['jobs']) - 1), 0)
        processed = sum(job['state'] not in {'pending', 'running', 'paused'} for job in data['jobs'])
        self.progress.setRange(0, max(1, len(data['jobs'])))
        self.progress.setValue(processed)
        counts = {state: sum(job['state'] == state for job in data['jobs']) for state in STATUS}
        self.progress.setFormat(f"已处理 {processed}/{len(data['jobs'])} · 确认 {counts['confirmed']} · "
                                f"跳过 {counts['skipped']} · 待复核 {counts['review']} · "
                                f"未支持 {counts['unsupported']} · 失败 {counts['error']} · 目录问题 {len(data.get('discovery_issues', []))}")
        self._selection()

    def _selection(self):
        row = self.table.currentRow()
        jobs = getattr(self, 'snapshot', {}).get('jobs', [])
        valid = 0 <= row < len(jobs)
        if hasattr(self, 'inspect'):
            self.inspect.setEnabled(valid and self.future is None)
        if valid:
            job = jobs[row]
            report = f" · 报告 {job['report_id']}" if job.get('report_id') else ''
            self.detail.setText(f"{job['name']} · {job.get('message', '')}{report}")

    def _inspect(self):
        row = self.table.currentRow()
        if self.future or not 0 <= row < len(self.store.data['jobs']):
            return
        self.inspect_job = dict(self.store.data['jobs'][row])
        self.operation = 'inspect'
        self._busy()
        self.status.setText('正在读取所选关卡…')
        self.future = self.pool.submit(lambda: challenge_items(self.client.level(self.inspect_job['level_id'])))
        self.timer.start()

    def _show_challenge(self, items):
        job = self.inspect_job
        dialog = ChallengeDialog(self.client, self.account.id, job['task_id'], job['unit_id'], job['level_id'],
            job['name'], items, self.data_dir, self.preview, self, account=self.account, vault=self.vault)
        dialog.ffmpeg.setText(self.ffmpeg.text())
        voice_index = dialog.voices.findData(self.voices.currentData())
        if voice_index >= 0:
            dialog.voices.setCurrentIndex(voice_index)
        dialog.exec()
        if dialog.result_data:
            key = job_key(job)
            current = next(job for job in self.store.data['jobs'] if job_key(job) == key)
            current.update(state='confirmed', score=dialog.result_data['score'],
                           report_id=dialog.result_data['report_id'], message='本次录音路径与平台报告已核对。')
            self.store.save()
        dialog.deleteLater()
        self._render(deepcopy(self.store.data))

    def _login(self):
        dialog = LoginDialog(self.account, self)
        dialog.exec()
        if dialog.client:
            if dialog.client.user_id != self.store.context['user_id']:
                self.status.setText('登录账号与队列绑定身份不一致，未更换会话。')
            else:
                self.client = dialog.client
                self.store.data['stage'] = 'paused'
                self.store.save()
                if self.on_connected:
                    self.on_connected(self.account, self.client)
                self.status.setText('已登录，可继续队列。')
        dialog.deleteLater()

    def _poll(self):
        while True:
            try:
                text, snapshot = self.messages.get_nowait()
            except queue.Empty:
                break
            self.log.appendPlainText(text)
            self._render(snapshot)
            if not self.cancel.is_set():
                self.status.setText(text)
        if self.future is None or not self.future.done():
            return
        future, self.future = self.future, None
        self.timer.stop()
        try:
            result = future.result()
            if self.operation == 'inspect' and self.close_result is None:
                self._show_challenge(result)
            elif self.operation == 'run':
                self.status.setText('全部关卡已处理。' if self.store.data['stage'] == 'complete' else
                                    '本轮已结束，待复核、未支持或失败的项目请查看下方原因，可调整后继续。')
        except SessionExpired as error:
            self.store.data['stage'] = 'needs_login'
            self.store.save()
            self.status.setText(f'{error} 请重新登录后继续。')
        except (Cancelled, ApiError, VaultError) as error:
            self.status.setText(str(error))
        except OSError:
            self.status.setText('队列进度无法读写，请检查数据目录权限。')
        except Exception as error:
            self.status.setText(f'执行中断（{type(error).__name__}），已保留可恢复进度。')
        self.operation = None
        self._render(deepcopy(self.store.data))
        self._idle()
        if self.close_result is not None:
            result, self.close_result = self.close_result, None
            self.done(result)

    def done(self, result):
        self.preview.stop()
        if self.future:
            self.close_result = result
            self._pause()
            return
        self.timer.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        super().done(result)
