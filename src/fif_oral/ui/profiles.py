import wave
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from fif_oral.storage import Account, VaultError


class ProfilesDialog(QDialog):
    def __init__(self, vault, active, preview, parent=None):
        super().__init__(parent)
        self.setWindowTitle("账号与音色")
        self.resize(840, 560)
        self.vault = vault
        self.draft = deepcopy(vault.accounts)
        self.account = None
        self.preview = preview
        self.imported = []
        self.original = [voice for account in vault.accounts for voice in account.voices]

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 22)
        outer.setSpacing(16)
        columns = QHBoxLayout()
        outer.addLayout(columns, 1)
        left = QVBoxLayout()
        columns.addLayout(left)
        self.accounts = QListWidget()
        self.accounts.setFixedWidth(165)
        left.addWidget(self.accounts, 1)
        left.addLayout(self._actions(("添加", self._add), ("移除", self._remove)))

        self.editor = QWidget()
        columns.addWidget(self.editor, 1)
        right = QVBoxLayout(self.editor)
        right.setContentsMargins(12, 0, 0, 0)
        form = QFormLayout()
        self.name = QLineEdit()
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("可在登录时手动输入")
        form.addRow("别名", self.name)
        form.addRow("登录账号", self.username)
        form.addRow("密码（可选）", self.password)
        right.addLayout(form)
        right.addWidget(QLabel("参考 WAV"))
        self.voices = QListWidget()
        right.addWidget(self.voices, 1)
        right.addLayout(self._actions(("导入", self._import), ("试听", self._listen),
                                      ("改名", self._rename), ("替换", self._replace),
                                      ("设为默认", self._default),
                                      ("移除", self._remove_voice)))

        buttons = QDialogButtonBox()
        buttons.addButton("保存", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._commit)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.accounts.currentRowChanged.connect(self._select)
        self.name.editingFinished.connect(self._save_fields)
        self._populate(active)

    def _actions(self, *actions):
        layout = QHBoxLayout()
        for label, callback in actions:
            button = QPushButton(label)
            button.clicked.connect(callback)
            layout.addWidget(button)
        return layout

    def _populate(self, active=""):
        self.accounts.blockSignals(True)
        self.accounts.clear()
        for account in self.draft:
            self.accounts.addItem(account.name or "新账号")
        row = next((i for i, account in enumerate(self.draft) if account.id == active), 0)
        self.accounts.setCurrentRow(row if self.draft else -1)
        self.accounts.blockSignals(False)
        self.account = None
        self._select(self.accounts.currentRow())

    def _save_fields(self):
        if self.account is None:
            return
        self.account.name = self.name.text().strip()
        username = self.username.text().strip()
        if username.casefold() != self.account.username.casefold():
            self.account.user_id = ""
        self.account.username = username
        self.account.password = self.password.text()
        item = self.accounts.item(self.draft.index(self.account))
        if item:
            item.setText(self.account.name or "新账号")

    def _select(self, row):
        self._save_fields()
        self.preview.stop()
        self.account = self.draft[row] if row >= 0 else None
        self.editor.setEnabled(self.account is not None)
        self.name.setText(self.account.name if self.account else "")
        self.username.setText(self.account.username if self.account else "")
        self.password.setText(self.account.password if self.account else "")
        self._show_voices()

    def _add(self):
        self._save_fields()
        account = Account(name="新账号")
        self.draft.append(account)
        self._populate(account.id)
        self.name.selectAll()
        self.name.setFocus()

    def _remove(self):
        if self.account is None:
            return
        answer = QMessageBox.question(self, "移除账号", "移除这个本地账号档案及其音色副本？")
        if answer == QMessageBox.StandardButton.Yes:
            self.draft.remove(self.account)
            self._populate()

    def _show_voices(self, row=0):
        self.voices.clear()
        if self.account:
            for voice in self.account.voices:
                self.voices.addItem(voice.name + ("  ·  默认" if voice.id == self.account.default_voice else ""))
        if self.voices.count():
            self.voices.setCurrentRow(min(row, self.voices.count() - 1))

    def _voice(self):
        row = self.voices.currentRow()
        return self.account.voices[row] if self.account and row >= 0 else None

    def _pick_voice(self):
        source, _ = QFileDialog.getOpenFileName(self, "导入参考音色", "", "WAV 文件 (*.wav)")
        if not source:
            return None
        try:
            voice = self.vault.import_voice(Path(source))
        except (OSError, ValueError, EOFError, wave.Error, VaultError) as error:
            QMessageBox.warning(self, "导入失败", f"无法读取参考 WAV。\n{error}")
            return None
        self.imported.append(voice)
        return voice

    def _import(self):
        voice = self._pick_voice()
        if voice:
            self.account.voices.append(voice)
            if not self.account.default_voice:
                self.account.default_voice = voice.id
            self._show_voices(len(self.account.voices) - 1)

    def _replace(self):
        previous = self._voice()
        if previous is None:
            return
        voice = self._pick_voice()
        if voice is None:
            return
        self.preview.stop()
        voice.name = previous.name
        row = self.voices.currentRow()
        self.account.voices[row] = voice
        if self.account.default_voice == previous.id:
            self.account.default_voice = voice.id
        self._show_voices(row)

    def _listen(self):
        voice = self._voice()
        if voice:
            path = self.vault.voice_path(voice)
            if path.is_file():
                self.preview.play(QUrl.fromLocalFile(str(path)))
            else:
                QMessageBox.warning(self, "音色文件缺失", "请重新导入这个音色的参考 WAV。")

    def _rename(self):
        voice = self._voice()
        if voice:
            name, ok = QInputDialog.getText(self, "音色名称", "名称", text=voice.name)
            if ok and name.strip():
                voice.name = name.strip()
                self._show_voices(self.voices.currentRow())

    def _default(self):
        voice = self._voice()
        if voice:
            self.account.default_voice = voice.id
            self._show_voices(self.voices.currentRow())

    def _remove_voice(self):
        voice = self._voice()
        if voice:
            self.preview.stop()
            row = self.voices.currentRow()
            self.account.voices.remove(voice)
            if voice.id == self.account.default_voice:
                self.account.default_voice = self.account.voices[0].id if self.account.voices else ""
            self._show_voices(row)

    def _commit(self):
        self._save_fields()
        if any(not account.name or not account.username for account in self.draft):
            QMessageBox.warning(self, "检查账号", "请为每个账号填写别名和登录账号。")
            return
        if len({account.username.casefold() for account in self.draft}) != len(self.draft):
            QMessageBox.warning(self, "检查账号", "这个登录账号已经添加。")
            return
        active = self.account.id if self.account else ""
        try:
            self.vault.save(self.draft, active)
        except OSError:
            QMessageBox.warning(self, "保存失败", "账号文件无法写入，请检查目录权限。")
            return
        self.preview.stop()
        retained = {voice.id for account in self.draft for voice in account.voices}
        self._discard(self.original + self.imported, retained)
        self.imported.clear()
        self.accept()

    def _discard(self, voices, retained):
        leftovers = 0
        for voice in voices:
            if voice.id not in retained:
                try:
                    self.vault.voice_path(voice).unlink(missing_ok=True)
                except (OSError, VaultError):
                    leftovers += 1
        if leftovers:
            QMessageBox.warning(self, "音色文件", f"有 {leftovers} 个音色副本仍在 data/voices 目录中。")

    def done(self, result):
        self.preview.stop()
        self._discard(self.imported, set())
        self.imported.clear()
        super().done(result)
