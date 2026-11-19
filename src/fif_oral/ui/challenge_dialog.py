import queue
import shutil
import time
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from fif_oral.challenge import AttemptStore, file_hash, run_challenge
from fif_oral.audio.assessment import rejected_evaluation
from fif_oral.api.client import ApiError
from fif_oral.ui.practice import recording_paths
from fif_oral.audio.speech import BACKEND_NAMES, SpeechSettings, run_automatic_challenge, uses_system_voice, needs_reference_voice
from fif_oral.ui.speech_dialog import SpeechSettingsDialog
from fif_oral.storage import VaultError
from fif_oral.environment.manager import default_ffmpeg


RECOVER = {"submitting", "submitted", "confirmed"}


class ChallengeDialog(QDialog):
    def __init__(self, client, account_id, task_id, unit_id, level_id, title, items, data_dir, preview, parent=None,
                 *, account=None, vault=None):
        super().__init__(parent)
        self.setWindowTitle(f"挑战 · {title}")
        self.resize(900, 590)
        self.client = client
        self.items = items
        self.preview = preview
        self.account = account
        self.vault = vault
        self.data_dir = data_dir
        self.account_id = account_id
        settings_error = ""
        try:
            self.settings = SpeechSettings.load(data_dir)
        except ApiError as error:
            self.settings = SpeechSettings()
            settings_error = str(error)
        self.store = AttemptStore(data_dir / "challenges", account_id, client.user_id, task_id, unit_id, level_id)
        if self.store.data and self.store.data.get('stage') not in RECOVER:
            migrated = False
            for item in items:
                details = self.store.data.get('evaluations', {}).get(item.question_id, {})
                if (item.question_id not in self.store.data.get('completed', {}) and
                        item.question_id not in self.store.data.get('prepared', {}) and
                        rejected_evaluation(item, details)):
                    pending = self.store.data.setdefault('pending', {})
                    if item.question_id not in pending:
                        pending[item.question_id] = {'reason': '旧版候选被拒识，等待新的生成策略处理。'}
                        migrated = True
            if migrated:
                self.store.data['stage'] = 'review'
                self.store.save()
        self.files = recording_paths(data_dir / "recordings", account_id, task_id, level_id, "挑战",
                                     [item.prompt for item in items])
        stored_files = self.store.data.get("files", [])
        if len(stored_files) == len(items):
            self.files = [Path(file) for file in stored_files]
        self.future = None
        self.cancel = Event()
        self.messages = queue.Queue()
        self.close_result = None
        self.result_data = None
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fif-challenge")
        self.timer = QTimer(self, interval=80)
        self.timer.timeout.connect(self._poll)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)
        heading = QLabel(title, textFormat=Qt.TextFormat.PlainText)
        heading.setObjectName("title")
        layout.addWidget(heading)
        context = QLabel(f"{client.name} · {len(items)} 题", textFormat=Qt.TextFormat.PlainText)
        context.setObjectName("muted")
        layout.addWidget(context)
        controls = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItem("自动生成音频", "speech")
        self.mode.addItem("选择本地音频", "files")
        if self.store.data and self.store.data.get("source", {}).get("mode") != "speech":
            self.mode.setCurrentIndex(1)
        controls.addWidget(self.mode)
        self.voice_label = QLabel("参考音色")
        controls.addWidget(self.voice_label)
        self.voices = QComboBox()
        if account:
            for voice in account.voices:
                self.voices.addItem(voice.name, voice.id)
            self.voices.setCurrentIndex(max(0, self.voices.findData(account.default_voice)))
        self.voices.setPlaceholderText("请先在账号管理中导入 WAV")
        controls.addWidget(self.voices, 1)
        self.model_button = QPushButton()
        self.model_button.clicked.connect(self._models)
        controls.addWidget(self.model_button)
        layout.addLayout(controls)
        self.table = QTableWidget(len(items), 3)
        self.table.setHorizontalHeaderLabels(["题目", "音频", "逐题评分"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.names = []
        self.choose_buttons = []
        for row, item in enumerate(items):
            question = QTableWidgetItem(item.prompt.text)
            question.setToolTip(item.prompt.text)
            self.table.setItem(row, 0, question)
            audio = QWidget()
            bar = QHBoxLayout(audio)
            bar.setContentsMargins(4, 2, 4, 2)
            name = QLabel(textFormat=Qt.TextFormat.PlainText)
            self.names.append(name)
            bar.addWidget(name, 1)
            choose = QPushButton("选择")
            choose.clicked.connect(lambda _, row=row: self._choose(row))
            self.choose_buttons.append(choose)
            bar.addWidget(choose)
            self.table.setCellWidget(row, 1, audio)
            self.table.setRowHeight(row, 44)
        self.table.setCurrentCell(0, 0)
        layout.addWidget(self.table, 1)

        tools = QHBoxLayout()
        listen = QPushButton("试听所选音频")
        listen.clicked.connect(self._listen)
        self.listen = listen
        tools.addWidget(listen)
        tools.addStretch()
        tools.addWidget(QLabel("FFmpeg"))
        self.ffmpeg = QLineEdit(default_ffmpeg(self.data_dir))
        self.ffmpeg.setPlaceholderText("选择 ffmpeg.exe")
        tools.addWidget(self.ffmpeg, 1)
        self.browse_ffmpeg = QPushButton("浏览")
        self.browse_ffmpeg.clicked.connect(self._pick_ffmpeg)
        tools.addWidget(self.browse_ffmpeg)
        layout.addLayout(tools)
        self.status = QLabel(wordWrap=True, textFormat=Qt.TextFormat.PlainText)
        self.status.setObjectName("muted")
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.start = QPushButton("开始评测并提交", objectName="primary")
        self.start.clicked.connect(self._start)
        buttons.addWidget(self.start)
        self.stop = QPushButton("停止")
        self.stop.clicked.connect(self.cancel.set)
        buttons.addWidget(self.stop)
        self.new = QPushButton("新一轮")
        self.new.clicked.connect(self._new)
        buttons.addWidget(self.new)
        buttons.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.mode.currentIndexChanged.connect(self._update)
        self._update()
        if self.store.data.get("stage") == "confirmed":
            self.status.setText(f"上次挑战已确认，本次得分 {self.store.data['score']} / 100。")
        elif self.store.data.get("stage") in RECOVER:
            self.status.setText("上次提交结果待核实，点击「回查成绩」继续。")
        elif self.store.data.get("pending"):
            self.status.setText(f"{len(self.store.data['pending'])} 题待复核。可调整失败题策略或增加预算，已验收题目会保留。")
        elif self._has_rejection():
            self.status.setText("上次音频被拒识，可以继续动态调整未完成的题目。" if self._can_adjust() else
                                "上次音频被拒识。点击「新一轮」，更换单词生成策略、模型或音频后重新评测。")
        elif self.store.data:
            count = len(self.store.data.get('completed', {})) + len(self.store.data.get('prepared', {}))
            self.status.setText(f"已保存 {count}/{len(items)} 题结果，可以继续。")
        elif any(uses_system_voice(item, self.settings) for item in items):
            self.status.setText("单个英文词使用 Windows 美式英语慢速音色（非账号音色），句段使用所选模型；逐题按评测反馈动态调整。")
        else:
            self.status.setText(settings_error or ('Edge TTS 联网生成音频，无需账号参考音色；评测通过后上传并回查成绩。' if self.settings.backend == 'edge' else "选择账号音色与本地模型，一键生成音频、评测并提交。也可以切换为选择本地音频。"))

    def _update(self):
        busy = self.future is not None
        stage = self.store.data.get("stage")
        recovery = stage in RECOVER
        automatic = self.mode.currentData() == "speech"
        pending = bool(self.store.data.get("pending"))
        self.start.setText("回查成绩" if recovery else "重试待复核题" if pending else "继续评测并提交" if self.store.data else
                           "自动生成并提交" if automatic else "开始评测并提交")
        self.start.setEnabled(not busy and (recovery or pending or not self._has_rejection() or self._can_adjust()))
        self.stop.setEnabled(busy)
        self.new.setEnabled(not busy and bool(self.store.data) and stage not in {"submitting", "submitted"})
        self.ffmpeg.setEnabled(not busy and not recovery)
        self.browse_ffmpeg.setEnabled(not busy and not recovery)
        self.listen.setEnabled(not busy)
        self.mode.setEnabled(not busy and not self.store.data)
        settings = self._effective_settings()
        needs_reference = needs_reference_voice(self.items, settings)
        self.voices.setVisible(automatic and needs_reference)
        self.voice_label.setVisible(automatic and needs_reference)
        self.voices.setEnabled(not busy and (not self.store.data or pending))
        self.model_button.setVisible(automatic)
        self.model_button.setEnabled(not busy and (not self.store.data or pending))
        backend = self.store.data.get("source", {}).get("backend", self.settings.backend)
        title = BACKEND_NAMES.get(backend, backend)
        if any(uses_system_voice(item, settings) for item in self.items):
            title = "单词：Windows 英语" if not needs_reference else f"Windows 单词 / {title}"
        self.model_button.setText(f"{title} · 模型设置")
        for row, (item, path, choose) in enumerate(zip(self.items, self.files, self.choose_buttons)):
            selected = self.store.data.get('prepared', {}).get(item.question_id)
            display_path = Path(selected['audio']) if selected else path
            name = f"第 {row + 1} 题音频" if automatic else path.name
            self.names[row].setText(name if display_path.is_file() else "待自动生成" if automatic else "尚未选择")
            self.names[row].setToolTip(str(display_path))
            choose.setVisible(not automatic)
            item_pending = self.store.data.get("pending", {}).get(item.question_id)
            choose.setEnabled(not busy and (not self.store.data or bool(item_pending)))
            record = self.store.data.get("completed", {}).get(item.question_id) or self.store.data.get("prepared", {}).get(item.question_id)
            score = str(record["result"]["score"]) if record else "—"
            details = self.store.data.get("evaluations", {}).get(item.question_id, {})
            rejected = rejected_evaluation(item, details, record["result"] if record else None)
            cell = QTableWidgetItem("待复核" if item_pending else ("拒识" if details.get("rejected", True) else "录音异常") if rejected else score)
            if item_pending:
                cell.setToolTip(item_pending['reason'])
            elif rejected:
                cell.setToolTip(f"引擎拒识，异常码 {details.get('except_info', '未保存')}。自动模式可继续动态调整，或开始新一轮更换语音策略。")
            elif details.get("profile"):
                cell.setToolTip(f"采用 {details.get('label', details['profile'])}；保留了本题各次原始评测 XML。")
            self.table.setItem(row, 2, cell)

    def _has_rejection(self):
        return any(rejected_evaluation(item, self.store.data.get("evaluations", {}).get(item.question_id, {}),
                                       self.store.data.get("completed", {}).get(item.question_id, {}).get("result"))
                   for item in self.items)

    def _effective_settings(self):
        source = self.store.data.get("source") or {}
        if source.get("strategy"):
            return SpeechSettings(**source["strategy"])
        if self.store.data:
            from dataclasses import replace
            return replace(self.settings, word_mode="model")
        return self.settings

    def _can_adjust(self):
        if self.mode.currentData() != "speech" or self._effective_settings().adaptive != "on":
            return False
        return not any(item.question_id in self.store.data.get("completed", {}) and
                       rejected_evaluation(item, self.store.data.get("evaluations", {}).get(item.question_id, {}),
                                           self.store.data["completed"][item.question_id]["result"]) for item in self.items)

    def _models(self):
        dialog = SpeechSettingsDialog(self.settings, self.data_dir, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.settings = dialog.settings
            if self.store.data.get("pending"):
                source = self.store.data.get('source') or {}
                self.store.data.setdefault('strategy_revisions', []).append({'time': time.time(), 'source': source})
                self.store.data['source'] = {**source, 'strategy': asdict(self.settings), 'backend': self.settings.backend}
                self.store.save()
                self.status.setText("新策略仅用于待复核题，已验收题目保持原结果。")
            self._update()
        dialog.deleteLater()

    def _choose(self, row):
        file, _ = QFileDialog.getOpenFileName(self, "选择这一题的音频", "", "音频文件 (*.wav *.mp3 *.flac *.m4a *.ogg)")
        if file:
            self.files[row] = Path(file)
            item = self.items[row]
            if item.question_id in self.store.data.get('pending', {}):
                previous = self.store.data['files'][row]
                self.store.data.setdefault('input_revisions', []).append({'question_id': item.question_id,
                    'previous': previous, 'time': time.time()})
                self.store.data['files'][row] = str(self.files[row].resolve())
                self.store.data['hashes'][row] = file_hash(self.files[row])
                self.store.data.get('evaluations', {}).pop(item.question_id, None)
                self.store.data['pending'][item.question_id] = {'reason': '已更换音频，等待评测。'}
                self.store.save()
            self._update()

    def _listen(self):
        row = self.table.currentRow()
        path = self.files[row] if row >= 0 else None
        if row >= 0:
            selected = self.store.data.get('prepared', {}).get(self.items[row].question_id)
            details = self.store.data.get('evaluations', {}).get(self.items[row].question_id, {})
            if selected:
                path = Path(selected['audio'])
            elif details.get('audio'):
                path = Path(details['audio'])
        if path is not None and path.is_file():
            self.preview.play(QUrl.fromLocalFile(str(path)))
        else:
            self.status.setText("请先选择这道题的音频文件。")

    def _pick_ffmpeg(self):
        file, _ = QFileDialog.getOpenFileName(self, "选择 FFmpeg", "", "FFmpeg (ffmpeg.exe)")
        if file:
            self.ffmpeg.setText(file)

    def _new(self):
        self.store.path = None
        self.store.data = {}
        self.result_data = None
        self._update()
        self.status.setText("可以选择音频，开始新一轮挑战。")

    def _start(self):
        if self.future:
            return
        recovery = self.store.data.get("stage") in RECOVER
        ffmpeg = self.ffmpeg.text().strip()
        if not recovery and (not ffmpeg or not Path(ffmpeg).is_file()):
            self.status.setText("请先选择可用的 ffmpeg.exe。")
            return
        automatic = self.mode.currentData() == "speech" and not recovery
        if not automatic and not recovery and any(not file.is_file() for file in self.files):
            self.status.setText("请为每道题选择一份音频。")
            return
        voice_path = None
        if automatic:
            try:
                settings = self._effective_settings()
                remaining = [item for item in self.items if item.question_id not in self.store.data.get('completed', {})
                             and item.question_id not in self.store.data.get('prepared', {})]
                if remaining:
                    settings.validate_for(remaining)
                if needs_reference_voice(remaining, settings):
                    voice = next((voice for voice in self.account.voices if voice.id == self.voices.currentData()), None) if self.account else None
                    if voice is None or self.vault is None:
                        raise ApiError("请先在「账号 → 管理账号与音色」为这个账号导入参考 WAV。")
                    voice_path = self.vault.voice_path(voice)
                    if file_hash(voice_path) != voice.sha256:
                        raise ApiError("参考音色发生变化，请在账号管理中重新导入。")
            except (ApiError, VaultError) as error:
                self.status.setText(str(error))
                return
        self.preview.stop()
        self.cancel.clear()
        if automatic:
            self.future = self.pool.submit(run_automatic_challenge, self.client, self.store, self.items,
                                           self.settings, voice_path, self.data_dir, self.account_id, ffmpeg,
                                           self.cancel, self.messages.put)
        else:
            self.future = self.pool.submit(run_challenge, self.client, self.store, self.items, list(self.files), ffmpeg,
                                           self.cancel, self.messages.put)
        self._update()
        self.timer.start()

    def _poll(self):
        while not self.messages.empty():
            self.status.setText(self.messages.get_nowait())
        stored_files = self.store.data.get("files", [])
        if len(stored_files) == len(self.items):
            self.files = [Path(file) for file in stored_files]
        self._update()
        if not self.future.done():
            return
        future, self.future = self.future, None
        self.timer.stop()
        try:
            self.result_data = future.result()
        except ApiError as error:
            self.status.setText(str(error))
        except OSError:
            self.status.setText("本地进度无法读写，请检查数据目录权限。")
        except Exception as error:
            self.status.setText(f"挑战未完成（{type(error).__name__}）。")
        else:
            self.status.setText(f"本次成绩已核实 · {self.result_data['score']} / 100 · 报告 {self.result_data['report_id']}")
        self._update()
        if self.close_result is not None:
            result, self.close_result = self.close_result, None
            self.done(result)

    def done(self, result):
        self.preview.stop()
        if self.future:
            self.close_result = result
            self.cancel.set()
            self.status.setText("正在停止并保存进度…")
            return
        self.timer.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        super().done(result)
