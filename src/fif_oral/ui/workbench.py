from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime

from PySide6.QtCore import QObject, QTimer, Qt, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QToolButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from fif_oral.api.client import ApiError, SessionExpired, question_groups, records
from fif_oral.audio.assessment import challenge_items
from fif_oral.ui.challenge_dialog import ChallengeDialog
from fif_oral.automation import AutomationScope
from fif_oral.ui.automation_dialog import AutomationDialog
from fif_oral.ui.login import LoginDialog
from fif_oral.ui.practice import PracticeDialog, recording_paths
from fif_oral.ui.profiles import ProfilesDialog
from fif_oral.storage import VaultError
from fif_oral.ui.setup_dialog import SetupDialog


ROLE = Qt.ItemDataRole.UserRole


def label(text, style=""):
    widget = QLabel(str(text))
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    widget.setObjectName(style)
    return widget


class MediaPreview(QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self.player = QMediaPlayer(self)
        self.output = QAudioOutput(self)
        self.player.setAudioOutput(self.output)

    def play(self, url):
        if self.player.source() == url and self.player.playbackState() == QMediaPlayer.PlayingState:
            self.stop()
            return
        self.player.setSource(url)
        self.player.play()

    def stop(self):
        self.player.stop()
        self.player.setSource(QUrl())


@dataclass
class Node:
    kind: str
    name: str
    id: str
    task_id: str
    summary: dict
    detail: dict | None = None
    loading: bool = False
    error: str = ""
    last_result: dict | None = None


class Workbench(QMainWindow):
    def __init__(self, vault):
        super().__init__()
        self.setWindowTitle("FIF 口语")
        self.resize(1140, 760)
        self.setMinimumSize(790, 510)
        self.vault = vault
        self.sessions = {}
        self.revision = 0
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="fif-read")
        self.jobs = []
        self.logs = deque(maxlen=100)
        self.preview = MediaPreview(self)
        self.preview.player.errorOccurred.connect(lambda *_: self._status("音频无法播放。", True))

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        top = QFrame(objectName="top")
        top.setMinimumHeight(64)
        bar = QHBoxLayout(top)
        bar.setContentsMargins(26, 10, 26, 10)
        bar.addWidget(label("FIF 口语", "brand"))
        bar.addStretch()
        self.identity = label("", "muted")
        bar.addWidget(self.identity)
        self.account_button = QToolButton(text="账号")
        self.account_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu = QMenu(self.account_button)
        self.menu.aboutToShow.connect(self._account_menu)
        self.account_button.setMenu(self.menu)
        bar.addWidget(self.account_button)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self.refresh)
        self.environment_button = QPushButton('环境与下载')
        self.environment_button.clicked.connect(self._environment)
        bar.addWidget(self.environment_button)
        bar.addWidget(self.refresh_button)
        layout.addWidget(top)

        workspace = QWidget(objectName='workspace')
        workspace_layout = QHBoxLayout(workspace)
        workspace_layout.setContentsMargins(18, 18, 18, 4)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(16)
        splitter.setChildrenCollapsible(False)
        sidebar = QFrame(objectName="sidebar")
        sidebar.setMinimumWidth(246)
        left = QVBoxLayout(sidebar)
        left.setContentsMargins(16, 19, 16, 16)
        left.setSpacing(14)
        header = QHBoxLayout()
        header.addWidget(label("任务", "section"))
        header.addStretch()
        self.category = QComboBox()
        self.category.addItem("进行中", 1)
        self.category.addItem("已结束", 2)
        self.category.currentIndexChanged.connect(self.refresh)
        header.addWidget(self.category)
        left.addLayout(header)
        self.search = QLineEdit(placeholderText="筛选已加载节点")
        self.search.textChanged.connect(self._filter)
        left.addWidget(self.search)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(16)
        self.tree.setUniformRowHeights(True)
        self.tree.currentItemChanged.connect(self._select)
        self.tree.itemExpanded.connect(self._ensure_loaded)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        left.addWidget(self.tree, 1)
        splitter.addWidget(sidebar)

        self.detail = QScrollArea()
        self.detail.viewport().setAutoFillBackground(False)
        self.detail.setWidgetResizable(True)
        self.detail.setFrameShape(QFrame.Shape.NoFrame)
        splitter.addWidget(self.detail)
        splitter.setSizes([290, 810])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        workspace_layout.addWidget(splitter)
        layout.addWidget(workspace, 1)

        footer = QFrame(objectName="footer")
        bottom = QHBoxLayout(footer)
        bottom.setContentsMargins(26, 5, 22, 5)
        self.status = label("", "muted")
        bottom.addWidget(self.status, 1)
        history = QPushButton("日志", flat=True)
        history.clicked.connect(self._show_logs)
        bottom.addWidget(history)
        layout.addWidget(footer)

        self.timer = QTimer(self, interval=60)
        self.timer.timeout.connect(self._poll)
        self.timer.start()
        self.refresh()

    @property
    def account(self):
        return next((account for account in self.vault.accounts if account.id == self.vault.active), None)

    def _environment(self):
        dialog = SetupDialog(self.vault.path.parent, self)
        dialog.exec()
        dialog.deleteLater()

    @property
    def client(self):
        return self.sessions.get(self.vault.active)

    @staticmethod
    def _scope(item):
        node = item.data(0, ROLE)
        if node.kind == 'task':
            return AutomationScope('task', node.task_id, title=node.name)
        if node.kind == 'unit':
            return AutomationScope('unit', node.task_id, node.id, title=node.name)
        parent = item.parent()
        if node.kind == 'level' and parent and parent.data(0, ROLE).kind == 'unit':
            return AutomationScope('level', node.task_id, parent.data(0, ROLE).id, node.id, node.name)
        raise ApiError('无法确定自动任务范围，请刷新目录。')

    def _automation_menu(self, item):
        menu = QMenu(self.tree)
        scope = self._scope(item)
        action = menu.addAction('全自动完成')
        action.triggered.connect(lambda _: self._automate(scope))
        return menu

    def _context_menu(self, position):
        item = self.tree.itemAt(position)
        if item is None:
            return
        try:
            menu = self._automation_menu(item)
        except ApiError as error:
            self._status(str(error), True)
            return
        self.tree.setCurrentItem(item)
        menu.exec(self.tree.viewport().mapToGlobal(position))
        menu.deleteLater()

    def _automate(self, scope):
        if not self.account:
            return
        if not self.client or self.client.expired():
            self.login()
        client, account = self.client, self.account
        if not client or client.expired():
            return
        try:
            dialog = AutomationDialog(client, account, scope, self.vault, self.preview, self,
                                      on_connected=self._connected)
        except (ApiError, ValueError, TypeError, OSError, VaultError) as error:
            self._status(str(error), True)
            return
        self.preview.stop()
        QTimer.singleShot(0, dialog._begin)
        dialog.exec()
        results = {(job['task_id'], job['unit_id'], job['level_id']): job for job in dialog.store.data['jobs']
                   if job['state'] == 'confirmed'}
        def update(parent):
            for index in range(parent.childCount()):
                item = parent.child(index)
                node = item.data(0, ROLE)
                if node.kind == 'level':
                    unit = item.parent().data(0, ROLE)
                    result = results.get((node.task_id, unit.id, node.id))
                    if result:
                        node.last_result = result
                update(item)
        update(self.tree.invisibleRootItem())
        if self.tree.currentItem():
            self._render(self.tree.currentItem())
        self._status(dialog.status.text())
        dialog.deleteLater()

    def _status(self, text, error=False):
        self.status.setText(text)
        self.logs.append(f"{datetime.now():%H:%M:%S}  {'失败  ' if error else ''}{text}")

    def _show_logs(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("查询日志")
        dialog.resize(570, 340)
        layout = QVBoxLayout(dialog)
        body = QPlainTextEdit("\n".join(self.logs), readOnly=True)
        layout.addWidget(body)
        dialog.exec()

    def _account_menu(self):
        self.menu.clear()
        for account in self.vault.accounts:
            action = self.menu.addAction(account.name)
            action.setCheckable(True)
            action.setChecked(account.id == self.vault.active)
            action.triggered.connect(lambda _, value=account.id: self._switch(value))
        if self.account:
            self.menu.addSeparator()
            self.menu.addAction("重新登录" if self.client else "登录", self.login)
            if self.client:
                self.menu.addAction("退出会话", self.logout)
        self.menu.addAction("账号与音色…", self._profiles)

    def _switch(self, account_id):
        if account_id == self.vault.active:
            return
        try:
            self.vault.save(self.vault.accounts, account_id)
        except OSError:
            self._status("无法保存当前账号，请检查配置文件权限。", True)
            return
        self.search.clear()
        self.refresh()

    def _profiles(self):
        old_credentials = {a.id: (a.username, a.password) for a in self.vault.accounts}
        dialog = ProfilesDialog(self.vault, self.vault.active, self.preview, self)
        if not self.vault.accounts:
            dialog._add()
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.sessions = {a.id: self.sessions[a.id] for a in self.vault.accounts
                             if a.id in self.sessions and old_credentials.get(a.id) == (a.username, a.password)}
            self.refresh()
        dialog.deleteLater()

    def login(self):
        account = self.account
        if account is None:
            self._profiles()
            return
        self.preview.stop()
        dialog = LoginDialog(account, self)
        dialog.exec()
        if dialog.client is not None:
            self._connected(account, dialog.client)
        dialog.deleteLater()

    def _connected(self, account, client):
        try:
            current = self.account
            if current is None or current.id != account.id or current.username != account.username:
                return
            accounts = [replace(a, user_id=client.user_id) if a.id == account.id else a
                        for a in self.vault.accounts]
            if current.user_id != client.user_id:
                self.vault.save(accounts, account.id)
        except OSError:
            self._login_failed("登录成功，但账号绑定无法保存，请检查配置目录权限。")
            return
        self.sessions[account.id] = client
        self.refresh()

    def _login_failed(self, message):
        self._update_header()
        self._message("登录失败", message, "重新登录", self.login)
        self._status(message, True)

    def logout(self):
        self.sessions.pop(self.vault.active, None)
        self.refresh()

    def _submit(self, work, success, failure):
        self.jobs.append((self.revision, self.pool.submit(work), success, failure))

    def _poll(self):
        ready = []
        pending = []
        for job in self.jobs:
            (ready if job[1].done() else pending).append(job)
        self.jobs = pending
        for revision, future, success, failure in ready:
            if revision != self.revision:
                continue
            try:
                result = future.result()
            except SessionExpired as error:
                self.sessions.pop(self.vault.active, None)
                self.refresh()
                self._message("会话已过期", str(error), "登录", self.login)
                self._status(str(error), True)
            except ApiError as error:
                failure(str(error))
            except Exception as error:
                failure(f"查询失败（{type(error).__name__}）。")
            else:
                success(result)

    def _update_header(self):
        account = self.account
        self.account_button.setText(account.name if account else "账号")
        self.identity.setText(f"已登录 · {self.client.name}" if self.client else "未登录")
        self.refresh_button.setEnabled(self.client is not None)
        self.category.setEnabled(self.client is not None)

    def refresh(self):
        self.revision += 1
        for _, future, _, _ in self.jobs:
            future.cancel()
        self.preview.stop()
        self.tree.clear()
        self._update_header()
        client = self.client
        if self.account is None:
            self._message("还没有账号", "添加账号后可以查询口语任务。", "添加账号", self._profiles)
            self._status("等待添加账号。")
        elif client is None:
            self._message("登录口语训练系统", "登录后可查看这个账号的任务、题目和已得分。", "登录", self.login)
            self._status("等待登录。")
        elif client.expired():
            self.sessions.pop(self.vault.active, None)
            self._update_header()
            self._message("会话已过期", "重新登录后继续查询。", "登录", self.login)
            self._status("会话已过期。", True)
        else:
            category = self.category.currentData()
            self._message("正在读取任务", "正在查询所选账号的任务列表。")
            self._status("正在查询任务列表。")
            self._submit(lambda: client.tasks(category), self._tasks_loaded, self._tasks_failed)

    def _tasks_loaded(self, tasks):
        for task in tasks:
            task_id = str(task.get("taskId") or task.get("id") or "")
            self._item(self.tree, "task", task.get("taskName") or "任务", task_id, task_id, task)
        self._filter(self.search.text())
        self._status(f"已读取 {len(tasks)} 个任务。")
        if tasks:
            self._message("选择任务", "展开左侧任务树，查看课程、关卡和题目。")
        else:
            self._message("暂无任务", "可以切换任务分类，或稍后刷新。")

    def _tasks_failed(self, message):
        self._message("任务读取失败", message, "重试", self.refresh)
        self._status(message, True)

    def _item(self, parent, kind, name, identity, task_id, summary):
        item = QTreeWidgetItem(parent, [str(name)])
        item.setData(0, ROLE, Node(kind, str(name), identity, task_id, summary))
        if kind != "level":
            item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        return item

    def _filter(self, query):
        term = query.strip().casefold()

        def visit(item, parent_matches):
            matches = parent_matches or term in item.text(0).casefold()
            visible_child = False
            for index in range(item.childCount()):
                visible_child = visit(item.child(index), matches) or visible_child
            item.setHidden(not (matches or visible_child))
            return matches or visible_child

        for index in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(index), False)

    def _select(self, item, previous):
        self.preview.stop()
        if item is not None:
            self._ensure_loaded(item)
            self._render(item)

    def _ensure_loaded(self, item):
        node = item.data(0, ROLE)
        client = self.client
        if not client or node.loading or node.detail is not None or node.error:
            return
        node.loading = True
        if node.kind == "task":
            work = lambda: client.task(node.id)
        elif node.kind == "unit":
            work = lambda: client.unit(node.task_id, node.id)
        else:
            work = lambda: client.level(node.id)
        self._status(f"正在读取 {node.name}。")
        self._submit(work, lambda result: self._node_loaded(item, node, result),
                     lambda message: self._node_failed(item, node, message))

    def _node_loaded(self, item, node, result):
        node.detail = result
        node.loading = False
        if node.kind == "task":
            for unit in records(result.get("ttdList")):
                unit_id = str(unit.get("unitid") or unit.get("unitId") or unit.get("id") or "").split(",")[-1]
                if unit_id:
                    self._item(item, "unit", unit.get("unitName") or "单元", unit_id, node.task_id, unit)
        elif node.kind == "unit":
            for level in records(result.get("levelList")):
                if level.get("levelId"):
                    self._item(item, "level", level.get("levelName") or "关卡", str(level["levelId"]),
                               node.task_id, level)
        item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless)
        self._filter(self.search.text())
        if self.tree.currentItem() == item:
            self._render(item)
        self._status(f"已读取 {node.name}。")

    def _node_failed(self, item, node, message):
        node.loading = False
        node.error = message
        if self.tree.currentItem() == item:
            self._render(item)
        self._status(message, True)

    def _retry(self, item, node):
        node.error = ""
        self._ensure_loaded(item)
        self._render(item)

    def _content(self):
        page = QWidget(objectName="detail")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 26, 28, 26)
        layout.setSpacing(16)
        self.detail.setWidget(page)
        page.show()
        return layout

    def _message(self, title, message, action="", callback=None):
        layout = self._content()
        layout.addWidget(label(title, "title"))
        layout.addWidget(label(message, "muted"))
        if callback:
            button = QPushButton(action, objectName="primary")
            button.clicked.connect(callback)
            layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()

    def _render(self, item):
        node = item.data(0, ROLE)
        layout = self._content()
        names = []
        parent = item.parent()
        while parent is not None:
            names.insert(0, parent.text(0))
            parent = parent.parent()
        if names:
            layout.addWidget(label(" / ".join(names), "muted"))
        layout.addWidget(label({'task': '口语任务', 'unit': '课程', 'level': '关卡'}[node.kind], 'eyebrow'))
        heading = QHBoxLayout()
        heading.addWidget(label(node.name, 'title'), 1)
        if node.kind in {'task', 'unit'}:
            scope = self._scope(item)
            automate = QPushButton('全自动完成', objectName='primary')
            automate.clicked.connect(lambda: self._automate(scope))
            heading.addWidget(automate, alignment=Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(heading)
        data = {**node.summary, **(node.detail or {})}
        score_key = {"task": "score", "unit": "totalScore", "level": "levelScore"}[node.kind]
        metrics = QHBoxLayout()
        metrics.setSpacing(12)

        def metric(caption, text, style):
            box = QWidget(objectName='metric')
            box.setMinimumHeight(94)
            content = QVBoxLayout(box)
            content.setContentsMargins(18, 14, 18, 16)
            content.setSpacing(6)
            content.addWidget(label(caption, 'muted'))
            content.addWidget(label(text, style))
            metrics.addWidget(box, 1)

        if data.get(score_key) not in (None, ""):
            metric('平台成绩', f"{data[score_key]} / 100", 'score')
        if node.last_result:
            metric('最近已核实提交', f"本次挑战  {node.last_result['score']} / 100", 'section')
        if data.get("complete") not in (None, ""):
            metric('任务进度', f"完成度  {str(data['complete']).rstrip('%')}%", 'section')
        if metrics.count():
            layout.addLayout(metrics)
        if data.get("endTime") and node.kind == "task":
            layout.addWidget(label(f"截止时间  {data['endTime']}", "muted"))
        if node.loading:
            layout.addWidget(label("正在查询…", "muted"))
        elif node.error:
            layout.addWidget(label(node.error, "muted"))
            button = QPushButton("重试")
            button.clicked.connect(lambda: self._retry(item, node))
            layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
        elif node.detail is not None:
            if node.kind == "level":
                self._practice_button(layout, node, data)
                self._questions(layout, data)
            else:
                name = "课程" if node.kind == "task" else "关卡"
                count = item.childCount()
                layout.addWidget(label(f"{count} 个{name} · 从左侧选择查看" if count else f"暂无{name}", "muted"))
                for index in range(count):
                    child = item.child(index)
                    entry = child.data(0, ROLE)
                    card = QWidget(objectName='question')
                    row = QHBoxLayout(card)
                    row.setContentsMargins(16, 14, 12, 14)
                    row.setSpacing(12)
                    number = label(f'{index + 1:02}', 'ordinal')
                    number.setFixedWidth(28)
                    row.addWidget(number)
                    row.addWidget(label(entry.name, 'section'), 1)
                    score = entry.summary.get('totalScore' if entry.kind == 'unit' else 'levelScore')
                    if score not in (None, ''):
                        row.addWidget(label(f'{score} 分', 'muted'))
                    open_button = QPushButton('查看', flat=True)
                    open_button.clicked.connect(lambda _, child=child: self.tree.setCurrentItem(child))
                    row.addWidget(open_button)
                    layout.addWidget(card)
        layout.addStretch()

    def _practice_button(self, layout, node, data):
        groups = question_groups(data, for_practice=True)
        if not groups:
            return
        order = {"挑战": 0, "练习": 1}
        mode, _, prompts = min(groups, key=lambda group: order.get(group[0], 2))
        actions = QHBoxLayout()
        button = QPushButton("本地答题")
        button.clicked.connect(lambda: self._practice(node, mode, prompts))
        actions.addWidget(button)
        challenge = QPushButton("音频挑战", objectName="primary")
        challenge.clicked.connect(lambda: self._challenge(node, data))
        actions.addWidget(challenge)
        actions.addStretch()
        layout.addLayout(actions)

    def _challenge(self, node, data):
        account, client = self.account, self.client
        if not account or not client:
            return
        parent = self.tree.currentItem().parent()
        if parent is None:
            return
        unit = parent.data(0, ROLE)
        try:
            items = challenge_items(data)
            dialog = ChallengeDialog(client, account.id, node.task_id, unit.id, node.id, node.name,
                                     items, self.vault.path.parent, self.preview, self, account=account, vault=self.vault)
        except (ApiError, ValueError, OSError) as error:
            self._status(str(error), True)
            QMessageBox.warning(self, "挑战", str(error))
            return
        self.preview.stop()
        dialog.exec()
        result = dialog.result_data
        dialog.deleteLater()
        if result:
            self._status(f"本次挑战成绩已核实，得分 {result['score']}。")
            node.last_result = result
            self._render(self.tree.currentItem())

    def _practice(self, node, mode, prompts):
        account = self.account
        if not account:
            return
        root = self.vault.path.parent / "recordings"
        paths = recording_paths(root, account.id, node.task_id, node.id, mode, prompts)
        self.preview.stop()
        dialog = PracticeDialog(node.name, mode, prompts, paths, self.preview, self)
        dialog.exec()
        dialog.deleteLater()

    def _questions(self, layout, data):
        groups = question_groups(data)
        if not groups:
            layout.addWidget(label("暂无可显示的题目内容。", "muted"))
            return
        for name, kind, prompts in groups:
            layout.addWidget(label(f"{name}  ·  {kind}" if kind else name, "section"))
            for index, prompt in enumerate(prompts, 1):
                question = QWidget(objectName='question')
                row = QHBoxLayout(question)
                row.setContentsMargins(14, 12, 14, 12)
                row.setSpacing(12)
                number = label(f'{index:02}', 'ordinal')
                number.setFixedWidth(28)
                row.addWidget(number, alignment=Qt.AlignmentFlag.AlignTop)
                text = "\n".join(part for part in (prompt.text, prompt.translation) if part)
                row.addWidget(label(text or '题目音频'), 1)
                if prompt.audio:
                    button = QPushButton("试听")
                    button.clicked.connect(lambda _, url=prompt.audio: self.preview.play(QUrl(url)))
                    row.addWidget(button, alignment=Qt.AlignmentFlag.AlignTop)
                layout.addWidget(question)

    def closeEvent(self, event):
        self.timer.stop()
        self.preview.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(event)
