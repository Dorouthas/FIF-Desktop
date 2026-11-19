from dataclasses import replace

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget, QScrollArea, QFrame,
)

from fif_oral.api.client import ApiError
from fif_oral.audio.speech import MODEL_NAMES, BACKEND_NAMES
from fif_oral.audio.speech import SpeechSettings
from fif_oral.ui.setup_dialog import SetupDialog


class SpeechSettingsDialog(QDialog):
    def __init__(self, settings, data_dir, parent=None):
        super().__init__(parent)
        self.setWindowTitle("语音模型")
        self.resize(740, 680)
        self.settings = settings
        self.data_dir = data_dir
        self.directories = {**{name: getattr(settings, name) for name in MODEL_NAMES}, 'edge': ''}
        self.previous = settings.backend
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)
        note = QLabel("Edge TTS 联网合成，无需下载模型，不使用账号参考音色。YourTTS 和 XTTS v2 保留本地模型及账号音色。备用模型需单独准备。")
        note.setWordWrap(True)
        layout.addWidget(note)
        setup = QPushButton('环境检测与自动下载…')
        setup.clicked.connect(self._environment)
        layout.addWidget(setup)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        form = QFormLayout(body)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        self.backend = QComboBox()
        for name, title in BACKEND_NAMES.items():
            self.backend.addItem(title, name)
        self.backend.setCurrentIndex(self.backend.findData(settings.backend))
        form.addRow("语音引擎", self.backend)
        self.python = QLineEdit(settings.python)
        form.addRow("Python", self._picker(self.python, self._python))
        self.directory = QLineEdit(self.directories[settings.backend])
        form.addRow("模型目录", self._picker(self.directory, self._directory))
        self.device = QComboBox()
        for title, device in (("自动", "auto"), ("CUDA", "cuda"), ("CPU", "cpu")):
            self.device.addItem(title, device)
        self.device.setCurrentIndex(self.device.findData(settings.device))
        form.addRow("运行设备", self.device)
        self.word_mode = QComboBox()
        self.word_mode.addItem("Windows 美式英语慢速（旧版策略，非账号音色）", "system")
        self.word_mode.addItem("使用所选引擎（本地模型使用账号音色）", "model")
        self.word_mode.setCurrentIndex(self.word_mode.findData(settings.word_mode))
        form.addRow("单词生成", self.word_mode)
        self.edge_voice = QComboBox()
        self.edge_voice.setEditable(True)
        self.edge_voice.addItems(['en-US-AriaNeural', 'en-US-JennyNeural', 'en-US-AndrewNeural', 'en-US-GuyNeural'])
        self.edge_voice.setCurrentText(settings.edge_voice)
        form.addRow('Edge 英文音色', self.edge_voice)
        self.edge_rate = QSpinBox(minimum=-50, maximum=100, value=int(settings.edge_rate[:-1]))
        self.edge_rate.setSuffix('%')
        form.addRow('Edge 语速', self.edge_rate)
        self.fallback = QComboBox()
        self.fallback.addItem("关闭：保持当前音色与生成引擎", "off")
        for backend, title in MODEL_NAMES.items():
            self.fallback.addItem(f"{title}（使用账号参考音色）", backend)
        self.fallback.setCurrentIndex(self.fallback.findData(settings.fallback_model))
        form.addRow("允许的备用模型", self.fallback)
        self.context_mode = QCheckBox('允许上下文合成后截取目标词（含一次对齐评测）')
        self.context_mode.setChecked(settings.context_mode == 'on')
        self.context_mode.setToolTip('只对英文单词使用；对齐和目标词评测共占 2 个预算。通常需将预算提高到 5。')
        form.addRow('上下文后备', self.context_mode)
        self.adaptive = QCheckBox("生成候选、检查音频并按真实评测选择")
        self.adaptive.setChecked(settings.adaptive == "on")
        form.addRow("动态调整", self.adaptive)
        self.target = QSpinBox(minimum=0, maximum=100, value=int(settings.target_score))
        self.target.setToolTip("优化目标：预算用完后可采用未拒识的有效低分，始终保留真实分数。")
        form.addRow("优化目标分数", self.target)
        self.trials = QSpinBox(minimum=1, maximum=12, value=int(settings.max_trials))
        self.trials.setToolTip("预算跨继续、重启保存；更改目标分数不会清空已用次数。")
        form.addRow("每题评测预算", self.trials)
        self.backend.currentIndexChanged.connect(self._switch)
        self.fallback.currentIndexChanged.connect(self._toggle_backend)
        self._toggle_backend()
        self.error = QLabel(wordWrap=True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox()
        buttons.addButton("保存", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _picker(self, field, callback):
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(field, 1)
        button = QPushButton("浏览")
        button.clicked.connect(callback)
        row.addWidget(button)
        return widget

    def _environment(self):
        previous = SpeechSettings.load(self.data_dir)
        dialog = SetupDialog(self.data_dir, self)
        dialog.exec()
        updated = SpeechSettings.load(self.data_dir)
        if updated.python != previous.python:
            self.python.setText(updated.python)
        for name in MODEL_NAMES:
            if getattr(updated, name) != getattr(previous, name):
                self.directories[name] = getattr(updated, name)
                if name == self.previous:
                    self.directory.setText(getattr(updated, name))
        dialog.deleteLater()

    def _python(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 TTS 环境的 Python", self.python.text(), "Python (python.exe);;所有文件 (*)")
        if path:
            self.python.setText(path)

    def _directory(self):
        path = QFileDialog.getExistingDirectory(self, "选择模型目录", self.directory.text())
        if path:
            self.directory.setText(path)

    def _switch(self):
        self.directories[self.previous] = self.directory.text().strip()
        self.previous = self.backend.currentData()
        self.directory.setText(self.directories[self.previous])
        self._toggle_backend()

    def _toggle_backend(self):
        local = self.backend.currentData() != 'edge'
        self.directory.parentWidget().setEnabled(local)
        self.python.parentWidget().setEnabled(local or self.fallback.currentData() != 'off')
        self.device.setEnabled(local or self.fallback.currentData() != 'off')
        self.edge_voice.setEnabled(not local)
        self.edge_rate.setEnabled(not local)

    def _save(self):
        self.directories[self.previous] = self.directory.text().strip()
        settings = replace(self.settings, python=self.python.text().strip(), backend=self.backend.currentData(),
                           device=self.device.currentData(), word_mode=self.word_mode.currentData(),
                           adaptive="on" if self.adaptive.isChecked() else "off",
                           target_score=str(self.target.value()), max_trials=str(self.trials.value()),
                           fallback_model=self.fallback.currentData(),
                           context_mode='on' if self.context_mode.isChecked() else 'off',
                           edge_voice=self.edge_voice.currentText().strip(), edge_rate=f'{self.edge_rate.value():+d}%',
                           **{name: self.directories[name] for name in MODEL_NAMES})
        try:
            settings.validate(neural=settings.word_mode == "model")
            if settings.fallback_model != "off":
                replace(settings, backend=settings.fallback_model).validate()
            settings.save(self.data_dir)
        except (ApiError, OSError) as error:
            self.error.setText(str(error))
            return
        self.settings = settings
        self.accept()
