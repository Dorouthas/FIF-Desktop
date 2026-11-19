import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QMessageBox, QVBoxLayout,
)

from fif_oral.storage import Vault, VaultError
from fif_oral.ui.workbench import Workbench, label
from fif_oral.ui.theme import STYLE, apply_theme
from fif_oral.paths import default_data_dir


class UnlockDialog(QDialog):
    def __init__(self, path):
        super().__init__()
        self.path = path
        self.vault = None
        self.creating = not path.exists()
        self.setWindowTitle("FIF 口语 · 账号库")
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.addWidget(label("创建账号库" if self.creating else "解锁账号库", "title"))
        layout.addWidget(label("用主密码保护本地账号与音色配置。", "muted"))
        form = QFormLayout()
        self.password = QLineEdit(echoMode=QLineEdit.EchoMode.Password)
        form.addRow("主密码", self.password)
        if self.creating:
            self.confirm = QLineEdit(echoMode=QLineEdit.EchoMode.Password)
            form.addRow("再次输入", self.confirm)
        layout.addLayout(form)
        self.error = label("", "muted")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox()
        proceed = buttons.addButton("创建" if self.creating else "解锁", QDialogButtonBox.ButtonRole.AcceptRole)
        proceed.setObjectName("primary")
        proceed.setDefault(True)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._unlock)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.password.setFocus()

    def _unlock(self):
        password = self.password.text()
        if not password:
            self.error.setText("请输入主密码。")
            return
        if self.creating and password != self.confirm.text():
            self.error.setText("两次输入的主密码不一致。")
            return
        try:
            self.vault = Vault(self.path, password)
        except VaultError as error:
            self.error.setText(str(error))
            return
        except OSError:
            self.error.setText("无法读写账号文件，请检查项目目录权限。")
            return
        self.password.clear()
        if self.creating:
            self.confirm.clear()
        self.accept()


def main():
    parser = argparse.ArgumentParser(description='FIF 口语桌面客户端')
    parser.add_argument('--data-dir', type=Path, default=default_data_dir())
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("FIF 口语")
    apply_theme(app)

    data = args.data_dir.expanduser().resolve()
    try:
        data.mkdir(parents=True, exist_ok=True)
    except OSError:
        QMessageBox.critical(None, "FIF 口语", "无法创建 data 目录，请检查项目目录权限。")
        return 1
    lock = QLockFile(str(data / "accounts.lock"))
    if not lock.tryLock(0):
        QMessageBox.warning(None, "FIF 口语", "账号库正在被使用。请关闭另一个 FIF 口语窗口后重试。")
        return 1
    try:
        unlock = UnlockDialog(data / "accounts.json")
        if unlock.exec() != QDialog.DialogCode.Accepted:
            return 0
        window = Workbench(unlock.vault)
        unlock.deleteLater()
        window.show()
        return app.exec()
    finally:
        lock.unlock()


if __name__ == "__main__":
    sys.exit(main())
