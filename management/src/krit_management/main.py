from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QDialog

from .api import ApiError, ManagementApi
from .config import API_URL
from .dialogs import LoginDialog
from .window import MainWindow

STYLESHEET = """
QMainWindow, QDialog { background: #f5f7fb; }
QWidget { color: #172033; font-size: 13px; }
QWidget#appRoot, QWidget#workspace { background: #f5f7fb; }
QFrame#sidebar { background: #0b225a; border: 0; }
QLabel#brandTitle { color: white; font-size: 22px; font-weight: 800; }
QLabel#sidebarLogo { background: white; border-radius: 9px; }
QListWidget#mainNavigation {
    background: transparent; border: 0; outline: 0; color: #dfe8ff;
}
QListWidget#mainNavigation::item {
    border-radius: 8px; padding: 0 12px; margin: 1px 0;
}
QListWidget#mainNavigation::item:hover { background: #173777; color: white; }
QListWidget#mainNavigation::item:selected {
    background: #1b56c9; color: white; font-weight: 650;
}
QFrame#workspaceHeader { background: transparent; border: 0; }
QFrame#controlPanel {
    background: #f8faff; border: 1px solid #e1e7f0; border-radius: 9px;
}
QTabWidget#clientTabs::pane {
    background: white; border: 1px solid #dfe5ee; border-radius: 10px;
    top: -1px;
}
QTabWidget#clientTabs, QTabWidget#clientTabs QTabBar { background: #f5f7fb; }
QTabWidget::pane {
    background: white; border: 1px solid #dfe5ee; border-radius: 9px;
    top: -1px;
}
QTabWidget QStackedWidget, QTabWidget QStackedWidget > QWidget { background: white; }
QTabBar::tab {
    background: #f5f7fb; color: #687386; border: 0;
    padding: 9px 16px; margin-right: 4px;
}
QTabBar::tab:hover { color: #164db3; }
QTabBar::tab:selected {
    color: #164db3; border-bottom: 2px solid #1b63db; font-weight: 650;
}
QTableWidget {
    background: white; alternate-background-color: #f8faff;
    border: 1px solid #e2e7ef; border-radius: 8px; outline: 0;
    selection-background-color: #e6efff; selection-color: #172033;
}
QListWidget {
    background: white; alternate-background-color: #f8faff;
    border: 1px solid #dfe5ee; border-radius: 7px; outline: 0;
    selection-background-color: #e6efff; selection-color: #172033;
}
QListWidget::item { min-height: 30px; padding: 3px 8px; }
QListWidget::item:hover { background: #f2f6fd; }
QTableWidget::item { padding: 6px 8px; border-bottom: 1px solid #edf0f5; }
QPushButton {
    background: #1b63db; color: white; border: 0; border-radius: 7px;
    min-height: 18px; padding: 7px 13px; font-weight: 600;
}
QPushButton:hover { background: #164fb5; }
QPushButton:pressed { background: #123f91; }
QPushButton[density="compact"] {
    min-height: 16px; padding: 5px 9px; border-radius: 6px;
}
QPushButton[kind="secondary"] {
    background: #eef3fb; color: #244164; border: 1px solid #d7e0ec;
}
QPushButton[kind="secondary"]:hover { background: #e1eafa; }
QPushButton[kind="warning"] { background: #fff2d9; color: #8b5400; }
QPushButton[kind="warning"]:hover { background: #ffe7b8; }
QPushButton[kind="danger"] { background: #fde8eb; color: #a72c3c; }
QPushButton[kind="danger"]:hover { background: #f9d5da; }
QPushButton[kind="navigation"] {
    background: transparent; color: #dfe8ff; border: 1px solid #385184;
    text-align: left; padding: 8px 12px;
}
QPushButton[kind="navigation"]:hover { background: #173777; color: white; }
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QAbstractSpinBox {
    background: white; border: 1px solid #ccd5e2; border-radius: 7px;
    min-height: 20px; padding: 7px 9px; selection-background-color: #1b63db;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
QComboBox:focus, QAbstractSpinBox:focus { border: 1px solid #1b63db; }
QLineEdit:read-only { background: #f4f6f9; color: #697386; }
QLineEdit:disabled, QComboBox:disabled, QAbstractSpinBox:disabled {
    background: #f2f4f7; color: #8b95a5; border-color: #e0e5ec;
}
QTextEdit, QPlainTextEdit { color: #172033; padding: 8px; }
QAbstractSpinBox { padding-right: 28px; }
QComboBox { padding-right: 30px; }
QComboBox::drop-down, QDateEdit::drop-down, QDateTimeEdit::drop-down {
    subcontrol-origin: padding; subcontrol-position: top right;
    width: 28px; border: 0; border-left: 1px solid #e1e6ee;
}
QComboBox::down-arrow, QDateEdit::down-arrow, QDateTimeEdit::down-arrow {
    image: url("%s"); width: 14px; height: 14px;
}
QComboBox::down-arrow:disabled, QDateEdit::down-arrow:disabled,
QDateTimeEdit::down-arrow:disabled {
    image: url("%s");
}
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-origin: border; subcontrol-position: top right;
    width: 28px; border: 0; border-left: 1px solid #e1e6ee;
    border-bottom: 1px solid #e1e6ee; border-top-right-radius: 7px;
    background: transparent;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border; subcontrol-position: bottom right;
    width: 28px; border: 0; border-left: 1px solid #e1e6ee;
    border-bottom-right-radius: 7px; background: transparent;
}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {
    background: #eef3fb;
}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    image: url("%s"); width: 12px; height: 12px;
}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    image: url("%s"); width: 12px; height: 12px;
}
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled {
    image: url("%s");
}
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {
    image: url("%s");
}
QComboBox QAbstractItemView {
    background: white; border: 1px solid #ccd5e2; border-radius: 7px;
    padding: 4px; outline: 0; selection-background-color: #e6efff;
    selection-color: #172033;
}
QComboBoxPrivateContainer {
    background: white; border: 1px solid #ccd5e2; border-radius: 7px;
}
QComboBoxPrivateContainer QAbstractItemView,
QAbstractItemView#searchCompleterPopup {
    background: white; color: #172033; border: 1px solid #ccd5e2;
    border-radius: 7px; padding: 4px; outline: 0;
    selection-background-color: #e6efff; selection-color: #172033;
}
QAbstractItemView#searchCompleterPopup::item {
    min-height: 28px; padding: 3px 8px; border-radius: 5px;
}
QAbstractItemView#searchCompleterPopup::item:hover,
QAbstractItemView#searchCompleterPopup::item:selected {
    background: #e6efff; color: #172033;
}
QCalendarWidget { background: white; border: 1px solid #ccd5e2; }
QCalendarWidget QWidget#qt_calendar_navigationbar {
    background: #f2f5f9; border-bottom: 1px solid #dfe5ee;
}
QCalendarWidget QToolButton {
    background: transparent; color: #172033; border: 0; border-radius: 5px;
    min-height: 24px; padding: 3px 8px; font-weight: 650;
}
QCalendarWidget QToolButton:hover { background: #e6efff; color: #164db3; }
QCalendarWidget QToolButton#qt_calendar_prevmonth { qproperty-icon: url("%s"); }
QCalendarWidget QToolButton#qt_calendar_nextmonth { qproperty-icon: url("%s"); }
QCalendarWidget QSpinBox {
    background: white; color: #172033; border: 1px solid #ccd5e2;
    border-radius: 5px; padding: 3px 6px;
}
QCalendarWidget QMenu {
    background: white; color: #172033; border: 1px solid #ccd5e2;
}
QCalendarWidget QAbstractItemView {
    background: white; color: #172033; outline: 0;
    selection-background-color: #1b63db; selection-color: white;
}
QCalendarWidget QAbstractItemView:disabled { color: #a4adba; }
QScrollBar:vertical {
    background: #f0f3f7; width: 12px; margin: 0; border: 0;
}
QScrollBar::handle:vertical {
    background: #b5bfcc; min-height: 30px; border-radius: 5px; margin: 2px;
}
QScrollBar:horizontal {
    background: #f0f3f7; height: 12px; margin: 0; border: 0;
}
QScrollBar::handle:horizontal {
    background: #b5bfcc; min-width: 30px; border-radius: 5px; margin: 2px;
}
QScrollBar::handle:hover { background: #8f9cad; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QHeaderView::section {
    background: #f2f5f9; color: #445066; border: 0;
    border-bottom: 1px solid #dfe5ee; padding: 8px; font-weight: 650;
}
QLabel#pageTitle { font-size: 24px; font-weight: 750; color: #15213a; }
QLabel#sectionTitle { font-size: 17px; font-weight: 700; color: #1c2942; }
QLabel#controlGroupLabel { color: #34435a; font-weight: 700; }
QLabel#supportingText { color: #687386; padding: 1px 2px; }
QLabel#formError {
    color: #9f2635; background: #fdecef; border: 1px solid #efc5cb;
    border-radius: 7px; padding: 8px 10px;
}
QLabel#emptyState {
    color: #7b8798; background: #fafbfc; border: 1px dashed #d9e0ea;
    border-radius: 8px; font-size: 14px;
}
QLabel#dialogTitle { font-size: 20px; font-weight: 750; color: #15213a; }
QLabel#placeholder { color: #8590a3; font-size: 16px; }
QLabel#connectionStatus {
    border-radius: 11px; padding: 4px 10px; font-size: 12px; font-weight: 650;
}
QLabel#connectionStatus[state="online"] { background: #dcf7e9; color: #087448; }
QLabel#connectionStatus[state="loading"] { background: #fff2d9; color: #875400; }
QLabel#connectionStatus[state="offline"] { background: #fde7ea; color: #a52b3b; }
QDialogButtonBox { background: transparent; }
QMessageBox { background: #f5f7fb; }
"""

CHECKBOX_STYLESHEET = """
QCheckBox { spacing: 9px; }
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border: 1px solid #87948b;
    border-radius: 4px;
    background: white;
}
QCheckBox::indicator:hover { border: 1px solid #1b63db; }
QCheckBox::indicator:checked {
    border: 1px solid #1b63db;
    background: #1b63db;
    image: url("%s");
}
QCheckBox::indicator:disabled {
    border-color: #c5ccd7;
    background: #f2f4f7;
}
QCheckBox::indicator:checked:disabled {
    border-color: #7fa7e8;
    background: #7fa7e8;
    image: url("%s");
}
"""

ASSETS_DIR = Path(__file__).with_name("assets")
APP_ICON = ASSETS_DIR / "app_icon.ico"


def build_stylesheet() -> str:
    def asset(name: str) -> str:
        return (ASSETS_DIR / name).as_posix()

    controls = STYLESHEET % (
        asset("combo_chevron.svg"),
        asset("chevron_down_disabled.svg"),
        asset("chevron_up.svg"),
        asset("combo_chevron.svg"),
        asset("chevron_up_disabled.svg"),
        asset("chevron_down_disabled.svg"),
        asset("chevron_left.svg"),
        asset("chevron_right.svg"),
    )
    check_asset = asset("checkbox_check.svg")
    return controls + CHECKBOX_STYLESHEET % (check_asset, check_asset)


def configure_windows_identity() -> None:
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("KRiT.Management")


def run() -> None:
    configure_windows_identity()
    QLocale.setDefault(QLocale(QLocale.Language.Russian, QLocale.Country.Russia))
    app = QApplication(sys.argv)
    qt_translator = QTranslator(app)
    translations_path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if qt_translator.load("qtbase_ru", translations_path):
        app.installTranslator(qt_translator)
    app.setOrganizationName("KRiT")
    app.setApplicationName("KRiT Management")
    app.setApplicationDisplayName("КРиТ · управление")
    app.setWindowIcon(QIcon(str(APP_ICON)))
    app.setStyleSheet(build_stylesheet())

    api = ManagementApi(API_URL)
    while True:
        dialog = LoginDialog()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            api.close()
            raise SystemExit(0)
        try:
            api.login(dialog.username.text().strip(), dialog.password.text())
        except ApiError as exc:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.warning(None, "Вход не выполнен", str(exc))
            continue
        break

    window = MainWindow(api)
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    run()
