"""Native dependency manager; all downloads and subprocesses run off the UI thread."""
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar,
    QPushButton, QVBoxLayout, QWidget,
)

from fif_oral.environment.manager import InstallationManager, catalog
from fif_oral.api.client import ApiError


class SetupDialog(QDialog):
    def __init__(self, data_dir, parent=None):
        super().__init__(parent)
        self.setWindowTitle('环境与下载')
        self.resize(830, 780)
        self.manager = InstallationManager(data_dir)
        self.close_result = None
        self.last_stage = None
        self.buttons = []
        self.licenses = {}
        self.labels = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)
        layout.addWidget(QLabel('环境与下载', objectName='title'))
        note = QLabel('按需准备 FFmpeg、独立 TTS 环境和模型。安装成功后自动配置路径，模型文件单独存放。')
        note.setWordWrap(True)
        layout.addWidget(note)
        grid = QGridLayout()
        grid.setVerticalSpacing(16)
        layout.addLayout(grid)
        for index, resource in enumerate(catalog(data_dir)):
            text = QLabel(resource['name'], textFormat=Qt.TextFormat.PlainText)
            grid.addWidget(text, index, 0)
            info = QWidget()
            column = QVBoxLayout(info)
            column.setContentsMargins(0, 0, 0, 0)
            state = QLabel(wordWrap=True, textFormat=Qt.TextFormat.PlainText, objectName='muted')
            state.setMinimumHeight(state.fontMetrics().lineSpacing() * 2 + 4)
            self.labels[resource['id']] = state
            column.addWidget(state)
            if resource.get('license'):
                size = resource['size'] / 1024**2
                link = QLabel(f'{size:.0f} MB · <a href="{resource["license_url"]}">{resource["license"]}</a>')
                link.setOpenExternalLinks(True)
                column.addWidget(link)
                accepted = QCheckBox('已阅读并同意上述模型许可')
                self.licenses[resource['id']] = accepted
                column.addWidget(accepted)
            grid.addWidget(info, index, 1)
            button = QPushButton('准备 / 修复')
            button.clicked.connect(lambda _, name=resource['id']: self._start(name))
            self.buttons.append(button)
            grid.addWidget(button, index, 2)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.status = QLabel(wordWrap=True, textFormat=Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.logs = QPlainTextEdit(readOnly=True)
        self.logs.setMaximumBlockCount(200)
        layout.addWidget(self.logs, 1)
        row = QHBoxLayout()
        check = QPushButton('重新检测环境')
        check.clicked.connect(lambda: self._start('check'))
        self.buttons.append(check)
        row.addWidget(check)
        edge_test = QPushButton('Edge 联网合成测试')
        edge_test.clicked.connect(lambda: self._start('edge_test'))
        self.buttons.append(edge_test)
        row.addWidget(edge_test)
        row.addStretch()
        self.cancel_button = QPushButton('取消安装')
        self.cancel_button.clicked.connect(self._cancel)
        row.addWidget(self.cancel_button)
        close = QPushButton('关闭')
        close.clicked.connect(self.reject)
        row.addWidget(close)
        layout.addLayout(row)
        self.timer = QTimer(self, interval=150)
        self.timer.timeout.connect(self._poll)
        self.timer.start()
        self._resources()
        self._poll()

    def _resources(self):
        for item in catalog(self.manager.root):
            label = '已找到文件' if item['found'] else '待准备'
            self.labels[item['id']].setText(f"{label} · {item['description']}")
            self.labels[item['id']].setToolTip(item['path'])

    def _start(self, name):
        try:
            accepted = self.licenses[name].isChecked() if name in self.licenses else False
            self.manager.start(name, accept_license=accepted)
        except ApiError as error:
            self.status.setText(str(error))
            return
        self._poll()

    def _cancel(self):
        self.manager.cancel.set()
        self.status.setText('正在取消并保留下载进度…')

    def _poll(self):
        data = self.manager.snapshot()
        for button in self.buttons:
            button.setEnabled(not data['busy'])
        self.cancel_button.setEnabled(data['busy'])
        self.progress.setRange(0, 0 if data['busy'] and not data['total'] else 100)
        if data['total']:
            self.progress.setValue(min(100, int(data['received'] * 100 / data['total'])))
            self.progress.setFormat(f"{data['received'] / 1024**2:.1f} / {data['total'] / 1024**2:.1f} MB")
        else:
            self.progress.setValue(100 if data['stage'] == 'completed' else 0)
            self.progress.setFormat('%p%')
        self.status.setText(data['message'])
        content = '\n'.join(data['logs'])
        if content != self.logs.toPlainText():
            self.logs.setPlainText(content)
            self.logs.verticalScrollBar().setValue(self.logs.verticalScrollBar().maximum())
        if not data['busy'] and self.last_stage != data['stage']:
            self._resources()
            report = data.get('result') or {}
            if data['component'] == 'check' and report.get('runtime_error'):
                self.labels['runtime'].setText('TTS 环境不可用 · 可点击「准备 / 修复」安装。')
                self.status.setText(report['runtime_error'])
            elif data['component'] == 'check' and report.get('runtime'):
                self.labels['runtime'].setText('TTS 导入检查通过。')
        self.last_stage = data['stage']
        if not data['busy'] and self.close_result is not None:
            result, self.close_result = self.close_result, None
            self.done(result)

    def done(self, result):
        if self.manager.busy:
            self.close_result = result
            self._cancel()
            return
        self.timer.stop()
        self.manager.close()
        super().done(result)
