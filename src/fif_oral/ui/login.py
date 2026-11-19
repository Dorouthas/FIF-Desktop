from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout,
)

from fif_oral.api.auth import CaptchaRequired, LoginFlow
from fif_oral.api.client import ApiError, FiFClient


class LoginDialog(QDialog):
    def __init__(self, account, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"登录 FIF 口语 · {account.name}")
        self.setMinimumWidth(455)
        self.account = account
        self.client = None
        self.flow = LoginFlow()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fif-login")
        self.future = None
        self.timer = QTimer(self, interval=60)
        self.timer.timeout.connect(self._poll)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        form = QFormLayout()
        form.addRow("账号", QLabel(account.username, textFormat=Qt.TextFormat.PlainText))
        self.mode = QComboBox()
        self.mode.addItem("账号密码", "password")
        self.mode.addItem("已签发的口语访问令牌", "token")
        form.addRow("登录方式", self.mode)
        self.password = QLineEdit(account.password, echoMode=QLineEdit.EchoMode.Password)
        self.token = QLineEdit(echoMode=QLineEdit.EchoMode.Password)
        self.image = QLabel()
        self.answer = QLineEdit(placeholderText="输入图中的字符")
        for text, widget in (("密码", self.password), ("访问令牌", self.token), ("", self.image), ("验证码", self.answer)):
            form.addRow(text, widget)
        self.form = form
        layout.addLayout(form)
        self.status = QLabel("", wordWrap=True, textFormat=Qt.TextFormat.PlainText)
        self.status.setObjectName("muted")
        layout.addWidget(self.status)

        buttons = QDialogButtonBox()
        self.submit = buttons.addButton("登录", QDialogButtonBox.ButtonRole.AcceptRole)
        self.submit.setObjectName("primary")
        self.submit.setDefault(True)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._begin)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self._mode_changed()
        self.password.setFocus()

    def _mode_changed(self):
        via_token = self.mode.currentData() == "token"
        self.form.setRowVisible(self.password, not via_token)
        self.form.setRowVisible(self.token, via_token)
        self.form.setRowVisible(self.image, not via_token and bool(self.flow.captcha_id))
        self.form.setRowVisible(self.answer, not via_token and bool(self.flow.captcha_id))
        self.status.setText("输入平台签发的口语访问令牌。" if via_token else "")

    def _begin(self):
        if self.future:
            return
        account = self.account
        if self.mode.currentData() == "token":
            token = self.token.text().strip()
            if not token:
                self.status.setText("请输入访问令牌。")
                return
            self.future = self.pool.submit(FiFClient.connect, token, "10003", account.user_id, account.username)
        else:
            password = self.password.text()
            answer = self.answer.text().strip()
            if not password or (self.flow.captcha_id and not answer):
                self.status.setText("请输入密码和平台要求的验证码。" if self.flow.captcha_id else "请输入密码。")
                return
            self.future = self.pool.submit(self.flow.sign_in, account.username, password, account.user_id, answer)
        self.status.setText("正在联系 FIF 口语…")
        self.mode.setEnabled(False)
        self.submit.setEnabled(False)
        self.timer.start()

    def _poll(self):
        if not self.future.done():
            return
        self.timer.stop()
        future, self.future = self.future, None
        self.mode.setEnabled(True)
        self.submit.setEnabled(True)
        try:
            self.client = future.result()
        except CaptchaRequired as challenge:
            image = QPixmap()
            if image.loadFromData(challenge.image):
                self.image.setPixmap(image)
                self.answer.clear()
                self.form.setRowVisible(self.image, True)
                self.form.setRowVisible(self.answer, True)
                self.answer.setFocus()
                self.status.setText(str(challenge))
            else:
                self.status.setText("无法显示平台验证码。")
                self.flow = LoginFlow()
        except ApiError as error:
            self._mode_changed()
            self.status.setText(str(error))
        except Exception as error:
            self._mode_changed()
            self.status.setText(f"登录失败（{type(error).__name__}）。")
        else:
            self.accept()

    def done(self, result):
        self.timer.stop()
        self.password.clear()
        self.token.clear()
        self.pool.shutdown(wait=False, cancel_futures=True)
        super().done(result)
