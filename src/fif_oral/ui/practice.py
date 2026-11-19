import hashlib
import json
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtMultimedia import (
    QAudioInput, QMediaCaptureSession, QMediaDevices, QMediaFormat, QMediaRecorder,
)
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)


def recording_paths(root, account_id, task_id, level_id, mode, prompts):
    account = hashlib.sha256(account_id.encode()).hexdigest()[:24]
    level = json.dumps([task_id, level_id, mode], ensure_ascii=False)
    folder = root / account / hashlib.sha256(level.encode()).hexdigest()[:24]
    paths = []
    for index, prompt in enumerate(prompts, 1):
        content = json.dumps([prompt.text, prompt.translation, urlsplit(prompt.audio).path], ensure_ascii=False)
        fingerprint = hashlib.sha256(content.encode()).hexdigest()[:16]
        paths.append(folder / f"{index:03d}-{fingerprint}.wav")
    return paths


def usable_wav(path):
    try:
        with wave.open(str(path), "rb") as audio:
            return bool(audio.readframes(1))
    except (OSError, EOFError, wave.Error):
        return False


@dataclass
class Take:
    folder: tempfile.TemporaryDirectory
    destination: Path
    error: str = ""

    @property
    def file(self):
        return Path(self.folder.name) / "recording.wav"


class PracticeDialog(QDialog):
    def __init__(self, title, mode, prompts, paths, preview, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"本地答题 · {title}")
        self.resize(680, 410)
        self.prompts = prompts
        self.paths = paths
        self.preview = preview
        self.index = next((i for i, path in enumerate(paths) if not usable_wav(path)), 0)
        self.take = None
        self.close_result = None
        self.devices = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(23, 20, 23, 20)
        self.heading = QLabel(textFormat=Qt.TextFormat.PlainText)
        self.heading.setObjectName("section")
        layout.addWidget(self.heading)
        self.mode = mode

        scroll = QScrollArea(widgetResizable=True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 2, 0, 2)
        self.prompt = QLabel(wordWrap=True, textFormat=Qt.TextFormat.PlainText)
        self.prompt.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body_layout.addWidget(self.prompt)
        body_layout.addStretch()
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

        form = QFormLayout()
        self.inputs = QComboBox()
        form.addRow("麦克风", self.inputs)
        layout.addLayout(form)
        controls = QHBoxLayout()
        self.listen_question = QPushButton("播放题目")
        self.listen_question.clicked.connect(self._play_question)
        controls.addWidget(self.listen_question)
        self.record = QPushButton("录音", objectName="primary")
        self.record.clicked.connect(self._record_or_stop)
        controls.addWidget(self.record)
        self.listen_answer = QPushButton("试听录音")
        self.listen_answer.clicked.connect(self._play_answer)
        controls.addWidget(self.listen_answer)
        controls.addStretch()
        layout.addLayout(controls)

        self.status = QLabel(wordWrap=True, textFormat=Qt.TextFormat.PlainText)
        self.status.setObjectName("muted")
        layout.addWidget(self.status)
        navigation = QHBoxLayout()
        self.previous = QPushButton("上一题")
        self.previous.clicked.connect(lambda: self._step(-1))
        self.next = QPushButton("下一题")
        self.next.clicked.connect(lambda: self._step(1))
        navigation.addWidget(self.previous)
        navigation.addWidget(self.next)
        navigation.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.reject)
        navigation.addWidget(close)
        layout.addLayout(navigation)

        self.media_devices = QMediaDevices(self)
        self.media_devices.audioInputsChanged.connect(self._reload_devices)
        self.audio_input = QAudioInput(self)
        self.recorder = QMediaRecorder(self)
        self.capture = QMediaCaptureSession(self)
        self.capture.setAudioInput(self.audio_input)
        self.capture.setRecorder(self.recorder)
        media = QMediaFormat(QMediaFormat.FileFormat.Wave)
        media.setAudioCodec(QMediaFormat.AudioCodec.Wave)
        self.recorder.setMediaFormat(media)
        self.recorder.setAudioChannelCount(1)
        self.recorder.setAudioSampleRate(48000)
        self.recorder.recorderStateChanged.connect(self._state_changed)
        self.recorder.errorOccurred.connect(self._recording_error)
        self.recorder.durationChanged.connect(self._duration)
        self._reload_devices()
        self._display()

    def _reload_devices(self):
        if self.take is not None:
            return
        current = self.inputs.currentIndex()
        preferred = self.devices[current] if 0 <= current < len(self.devices) else QMediaDevices.defaultAudioInput()
        self.devices = QMediaDevices.audioInputs()
        self.inputs.clear()
        self.inputs.addItems([device.description() for device in self.devices])
        selected = next((i for i, device in enumerate(self.devices) if device.id() == preferred.id()), 0)
        if self.devices:
            self.inputs.setCurrentIndex(selected)
        else:
            self.inputs.addItem("未找到录音设备")
        self._controls()

    def _display(self):
        prompt = self.prompts[self.index]
        ready = sum(usable_wav(path) for path in self.paths)
        total = len(self.prompts)
        self.heading.setText(f"{self.mode} · 第 {self.index + 1} / {total} 题 · 已录 {ready} / {total}")
        self.prompt.setText("\n\n".join(part for part in (prompt.text, prompt.translation) if part) or "播放音频以听取题目。")
        self._controls()

    def _controls(self):
        busy = self.take is not None
        recorded = usable_wav(self.paths[self.index])
        self.inputs.setEnabled(bool(self.devices) and not busy)
        self.record.setEnabled(busy or bool(self.devices))
        self.record.setText("停止录音" if busy else "重录" if recorded else "录音")
        self.listen_question.setEnabled(not busy and bool(self.prompts[self.index].audio))
        self.listen_answer.setEnabled(not busy and recorded)
        self.previous.setEnabled(not busy and self.index > 0)
        self.next.setEnabled(not busy and self.index + 1 < len(self.prompts))

    def _step(self, amount):
        self.preview.stop()
        self.index += amount
        self.status.clear()
        self._display()

    def _play_question(self):
        self.preview.play(QUrl(self.prompts[self.index].audio))

    def _play_answer(self):
        self.preview.play(QUrl.fromLocalFile(str(self.paths[self.index])))

    def _record_or_stop(self):
        if self.take is not None:
            self._stop()
            return
        index = self.inputs.currentIndex()
        if not 0 <= index < len(self.devices):
            return
        self.preview.stop()
        target = self.paths[self.index]
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            folder = tempfile.TemporaryDirectory(prefix=".recording-", dir=target.parent, ignore_cleanup_errors=True)
        except OSError:
            self.status.setText("无法写入录音目录，请检查目录权限。")
            return
        self.take = Take(folder, target)
        self.audio_input.setDevice(self.devices[index])
        self.recorder.setOutputLocation(QUrl.fromLocalFile(str(self.take.file)))
        self.status.setText("正在打开麦克风…")
        self._controls()
        self.recorder.record()

    def _duration(self, duration):
        if self.take is not None and self.record.isEnabled():
            seconds = duration // 1000
            self.status.setText(f"正在录音 · {seconds // 60:02d}:{seconds % 60:02d}")

    def _stop(self):
        self.record.setEnabled(False)
        self.status.setText("正在保存录音…")
        self.recorder.stop()
        if self.recorder.recorderState() == QMediaRecorder.RecorderState.StoppedState:
            QTimer.singleShot(0, self._finish)

    def _state_changed(self, state):
        if state == QMediaRecorder.RecorderState.StoppedState and self.take is not None:
            QTimer.singleShot(0, self._finish)

    def _recording_error(self, error, message):
        if self.take is not None and not self.take.error:
            self.take.error = message or "麦克风录音失败。"
            self._stop()

    def _finish(self):
        take, self.take = self.take, None
        if take is None:
            return
        message = "录音已保存到本地。"
        try:
            if take.error:
                message = take.error
            elif usable_wav(take.file):
                take.file.replace(take.destination)
            else:
                message = "没有录到音频，请重新录音。"
        except OSError:
            message = "保存录音失败，请检查录音目录权限。"
        finally:
            take.folder.cleanup()
        self.status.setText(message)
        self._reload_devices()
        self._display()
        if self.close_result is not None:
            result, self.close_result = self.close_result, None
            self.done(result)

    def done(self, result):
        self.preview.stop()
        if self.take is not None:
            self.close_result = result
            self._stop()
            return
        super().done(result)
