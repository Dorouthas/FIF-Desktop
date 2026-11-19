"""Native light theme for the FIF Oral desktop client, using Qt and local SVG icons."""
from pathlib import Path

from PySide6.QtGui import QColor, QFont, QPalette
from fif_oral.paths import RESOURCE_ROOT


COLORS = {
    'text': '#171717', 'secondary': '#404756', 'muted': '#5e687e',
    'accent': '#086da4', 'accent_hover': '#086091', 'accent_pressed': '#064c78',
    'highlight': '#e6f3ff', 'border': '#ebecf0', 'canvas': '#f6f7f9',
    'white': '#ffffff', 'disabled': '#99a5b3',
}
ASSETS = RESOURCE_ROOT / 'assets'

STYLE = """
QWidget { color: %(text)s; font-size: 14px; }
QMainWindow, QWidget#workspace { background: %(canvas)s; }
QDialog, QMessageBox { background: %(white)s; }
QLabel { background: transparent; border: none; }
QLabel:disabled { color: %(disabled)s; }
QFrame#top { background: %(white)s; border-bottom: 1px solid %(border)s; }
QFrame#footer { background: %(canvas)s; }
QFrame#sidebar, QWidget#detail {
    background: %(white)s; border: 1px solid %(border)s; border-radius: 16px;
}
QLabel#brand { font-size: 25px; font-weight: 700; letter-spacing: -1px; }
QLabel#title { font-size: 27px; font-weight: 650; }
QLabel#score { font-size: 29px; font-weight: 650; color: %(text)s; }
QLabel#section { font-size: 16px; font-weight: 600; }
QLabel#muted { color: %(muted)s; font-size: 13px; }
QLabel#eyebrow { color: %(accent)s; font-size: 12px; font-weight: 600; }
QLabel#ordinal { color: %(accent)s; font-size: 13px; font-weight: 600; }
QWidget#metric { background: %(canvas)s; border: none; border-radius: 12px; }
QWidget#question { background: %(white)s; border: 1px solid %(border)s; border-radius: 12px; }
QLineEdit, QComboBox, QSpinBox, QPushButton, QToolButton {
    padding: 8px 13px; border-radius: 10px; border: 1px solid %(border)s;
    background: %(white)s; selection-background-color: %(highlight)s; selection-color: %(accent)s;
}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover { border-color: #c6ced8; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: %(accent)s; }
QPushButton, QToolButton { font-weight: 500; }
QPushButton:hover, QToolButton:hover { color: %(accent)s; background: %(highlight)s; border-color: #c9e2f0; }
QPushButton:pressed, QToolButton:pressed { background: #d6eafa; border-color: %(accent)s; }
QPushButton:focus, QToolButton:focus { border-color: %(accent)s; }
QPushButton:disabled, QToolButton:disabled, QComboBox:disabled, QLineEdit:disabled, QSpinBox:disabled {
    color: %(disabled)s; background: %(canvas)s; border-color: %(border)s;
}
QPushButton#primary { color: %(white)s; background: %(accent)s; border-color: %(accent)s; }
QPushButton#primary:hover { color: %(white)s; background: %(accent_hover)s; border-color: %(accent_hover)s; }
QPushButton#primary:pressed { background: %(accent_pressed)s; }
QPushButton#primary:disabled { color: #7c9eb3; background: #e0edf4; border-color: #e0edf4; }
QPushButton:flat { background: transparent; border-color: transparent; color: %(muted)s; }
QPushButton:flat:hover { background: %(highlight)s; color: %(accent)s; }
QToolButton { border-radius: 17px; }
QComboBox { padding-right: 25px; }
QComboBox::drop-down { width: 23px; border: none; }
QComboBox::down-arrow { image: url("%(down)s"); width: 14px; height: 14px; }
QSpinBox { padding-right: 28px; }
QSpinBox::up-button, QSpinBox::down-button { width: 24px; border: none; background: transparent; }
QSpinBox::up-arrow { image: url("%(up)s"); width: 14px; height: 14px; }
QSpinBox::down-arrow { image: url("%(down)s"); width: 14px; height: 14px; }
QToolButton::menu-indicator { image: none; }
QComboBox QAbstractItemView {
    border: 1px solid %(border)s; background: %(white)s; outline: none;
    selection-background-color: %(highlight)s; selection-color: %(accent)s; padding: 4px;
}
QTreeWidget, QListWidget {
    background: %(white)s; border: none; outline: none;
    selection-background-color: %(highlight)s; selection-color: %(accent)s;
}
QTreeWidget::item, QListWidget::item { padding: 7px 5px; min-height: 21px; border-radius: 8px; }
QTreeWidget::item:selected, QListWidget::item:selected { color: %(accent)s; background: %(highlight)s; }
QTreeWidget::item:hover:!selected, QListWidget::item:hover:!selected { background: %(canvas)s; }
QListWidget { border: 1px solid %(border)s; border-radius: 12px; padding: 5px; }
QTableWidget {
    background: %(white)s; alternate-background-color: %(canvas)s; border: 1px solid %(border)s;
    border-radius: 10px; gridline-color: %(border)s; outline: none; font-size: 13px;
    selection-background-color: %(highlight)s; selection-color: %(accent)s;
}
QTableWidget::item { padding: 5px 9px; border: none; }
QTableWidget::item:selected { background: %(highlight)s; color: %(accent)s; }
QHeaderView { background: %(canvas)s; border: none; }
QHeaderView::section {
    background: %(canvas)s; color: %(muted)s; padding: 9px; border: none;
    border-bottom: 1px solid %(border)s; font-size: 12px; font-weight: 600;
}
QTableCornerButton::section { background: %(canvas)s; border: none; }
QPlainTextEdit {
    background: %(canvas)s; border: 1px solid %(border)s; border-radius: 10px;
    padding: 8px; font-size: 12px; selection-background-color: %(highlight)s; selection-color: %(accent)s;
}
QProgressBar {
    background: %(canvas)s; color: %(secondary)s; border: 1px solid %(border)s;
    border-radius: 8px; min-height: 24px; text-align: center; font-size: 12px;
}
QProgressBar::chunk { background: #d5ebf9; border-radius: 7px; }
QCheckBox { spacing: 8px; padding: 4px 0; color: %(secondary)s; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px; border: 1px solid #c6ced8; background: %(white)s; }
QCheckBox::indicator:hover { border-color: %(accent)s; }
QCheckBox::indicator:checked { background: %(accent)s; border-color: %(accent)s; image: url("%(check)s"); }
QCheckBox::indicator:disabled { background: %(canvas)s; border-color: %(border)s; }
QMenu { background: %(white)s; border: 1px solid %(border)s; border-radius: 10px; padding: 6px; }
QMenu::item { padding: 8px 24px; border-radius: 6px; }
QMenu::item:selected { background: %(highlight)s; color: %(accent)s; }
QMenu::item:disabled { color: %(disabled)s; }
QMenu::separator { height: 1px; background: %(border)s; margin: 5px 8px; }
QScrollArea { background: transparent; border: none; }
QSplitter, QSplitter::handle { background: %(canvas)s; }
QSplitter::handle:horizontal { width: 16px; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 3px 0; }
QScrollBar::handle:vertical { background: #ccd1d9; border-radius: 4px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: #99a5b3; }
QScrollBar:horizontal { background: transparent; height: 8px; margin: 0 3px; }
QScrollBar::handle:horizontal { background: #ccd1d9; border-radius: 4px; min-width: 28px; }
QScrollBar::handle:horizontal:hover { background: #99a5b3; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: none; background: transparent; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QToolTip { background: %(white)s; color: %(secondary)s; border: 1px solid %(border)s; padding: 7px; }
""" % {**COLORS, 'down': (ASSETS / 'chevron-down.svg').as_posix(),
       'up': (ASSETS / 'chevron-up.svg').as_posix(), 'check': (ASSETS / 'check.svg').as_posix()}


def apply_theme(app):
    app.setStyle('Fusion')
    font = QFont()
    font.setFamilies(['Segoe UI', 'Microsoft YaHei UI', 'Noto Sans CJK SC'])
    app.setFont(font)
    palette = QPalette()
    for role, color in ((QPalette.ColorRole.Window, 'white'), (QPalette.ColorRole.Base, 'white'),
                        (QPalette.ColorRole.WindowText, 'text'), (QPalette.ColorRole.Text, 'text'),
                        (QPalette.ColorRole.Button, 'white'), (QPalette.ColorRole.ButtonText, 'text'),
                        (QPalette.ColorRole.Highlight, 'accent'), (QPalette.ColorRole.HighlightedText, 'white'),
                        (QPalette.ColorRole.PlaceholderText, 'disabled')):
        palette.setColor(role, QColor(COLORS[color]))
    app.setPalette(palette)
    app.setStyleSheet(STYLE)
