from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from html import escape
from math import ceil
from typing import Any

from PySide6.QtCore import QDate, QDateTime, QMarginsF, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QPageLayout, QPageSize, QTextDocument
from PySide6.QtPrintSupport import QPrintDialog, QPrinter, QPrintPreviewWidget
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDateEdit,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .api import ManagementApi
from .timeutils import center_timezone, center_wall_time, now_center, parse_center
from .widgets import (
    SafeComboBox,
    SearchableComboBox,
    configure_calendar,
    configure_form_layout,
    matches_word_prefix,
)
from .workers import Worker

STATUS_LABELS = {
    "planned": "Запланировано",
    "scheduled": "Готово к началу",
    "in_progress": "Идёт",
    "completed": "Завершено",
    "cancelled": "Отменено",
}

ATTENDANCE_LABELS = {
    "expected": "Ожидается",
    "present": "Присутствует",
    "late": "Опоздал",
    "absent": "Не пришёл",
    "left_early": "Ушёл раньше",
    "excused": "Отменено",
}


def _participant_counts(lesson: dict[str, Any]) -> tuple[int, int]:
    participants = lesson.get("participants")
    participant_rows = participants if isinstance(participants, list) else []
    derived_active = sum(
        item.get("attendance_status") != "excused"
        for item in participant_rows
        if isinstance(item, dict)
    )
    derived_excused = sum(
        item.get("attendance_status") == "excused"
        for item in participant_rows
        if isinstance(item, dict)
    )
    active_value = lesson.get("active_participant_count")
    excused_value = lesson.get("excused_participant_count")
    active = derived_active if active_value is None else int(active_value)
    excused = derived_excused if excused_value is None else int(excused_value)
    return active, excused


def _participant_summary(lesson: dict[str, Any]) -> str:
    active, excused = _participant_counts(lesson)
    return f"{active}" + (f" · {excused} отмен." if excused else "")


def _participant_details(lesson: dict[str, Any]) -> str:
    count, excused = _participant_counts(lesson)
    suffix = "участник"
    if count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        suffix = "участника"
    elif count % 10 != 1 or count % 100 == 11:
        suffix = "участников"
    result = f"{count} {suffix}"
    return result + (f" · отменено: {excused}" if excused else "")


def _button(
    text: str,
    callback: Callable[[], None],
    kind: str = "secondary",
    *,
    compact: bool = False,
) -> QPushButton:
    result = QPushButton(text)
    result.setProperty("kind", kind)
    result.setProperty("density", "compact" if compact else "standard")
    result.clicked.connect(callback)
    return result


def _table(
    headers: list[str],
    *,
    stretch: tuple[int, ...] = (),
    compact: tuple[int, ...] = (),
    fixed: dict[int, int] | None = None,
) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(48)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.setWordWrap(False)
    header = table.horizontalHeader()
    header.setHighlightSections(False)
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    header.setStretchLastSection(False)
    header.setMinimumSectionSize(72)
    for column in range(len(headers)):
        mode = QHeaderView.ResizeMode.Interactive
        if column in stretch:
            mode = QHeaderView.ResizeMode.Stretch
        elif column in compact:
            mode = QHeaderView.ResizeMode.ResizeToContents
        if fixed and column in fixed:
            mode = QHeaderView.ResizeMode.Fixed
        header.setSectionResizeMode(column, mode)
        if fixed and column in fixed:
            table.setColumnWidth(column, fixed[column])
    return table


def _teachers_for_subject(references: dict[str, Any], subject_id: object) -> list[dict[str, Any]]:
    # Person.active controls MAX bot access, not whether a teacher may work in
    # the learning module. Archived people are already excluded by the API.
    teachers = list(references.get("teachers", []))
    if subject_id is None:
        return teachers
    subject = next(
        (item for item in references.get("subjects", []) if item.get("id") == subject_id),
        None,
    )
    if subject is None or "teacher_ids" not in subject:
        return teachers
    allowed = set(subject.get("teacher_ids") or [])
    return [item for item in teachers if item.get("id") in allowed]


class SchedulePreviewDialog(QDialog):
    def __init__(
        self,
        printer: QPrinter,
        document: QTextDocument,
        print_requested: Callable[[], None],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.printer = printer
        self.document = document
        self.setWindowTitle("Предпросмотр расписания")
        self.setMinimumSize(900, 620)
        self.resize(1180, 780)
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(_button("По ширине", self._fit_to_width))
        controls.addWidget(_button("−", lambda: self._change_zoom(0.8)))
        self.zoom = SafeComboBox()
        self.zoom.setEditable(True)
        self.zoom.setMinimumWidth(120)
        self.zoom.addItems(["50 %", "75 %", "100 %", "125 %", "150 %", "200 %"])
        self.zoom.setCurrentText("100 %")
        self.zoom.setAccessibleName("Масштаб предпросмотра")
        self.zoom.activated.connect(lambda _index: self._apply_zoom_text())
        if self.zoom.lineEdit() is not None:
            self.zoom.lineEdit().editingFinished.connect(self._apply_zoom_text)
        controls.addWidget(self.zoom)
        controls.addWidget(_button("+", lambda: self._change_zoom(1.25)))
        controls.addStretch(1)
        controls.addWidget(_button("Печать…", print_requested))
        controls.addWidget(_button("Закрыть", self.reject))
        layout.addLayout(controls)
        self.preview = QPrintPreviewWidget(printer, self)
        self.preview.paintRequested.connect(document.print_)
        layout.addWidget(self.preview, 1)
        QTimer.singleShot(0, self._initialize_preview)

    def _initialize_preview(self) -> None:
        self.preview.updatePreview()
        self._set_zoom_percent(100)

    @staticmethod
    def _normalize_zoom_percent(percent: int) -> int:
        return max(25, min(percent, 400))

    def _set_zoom_percent(self, percent: int) -> None:
        normalized = self._normalize_zoom_percent(percent)
        self.preview.setZoomFactor(normalized / 100)
        self.zoom.blockSignals(True)
        self.zoom.setCurrentText(f"{normalized} %")
        if self.zoom.lineEdit() is not None:
            self.zoom.lineEdit().setCursorPosition(0)
        self.zoom.blockSignals(False)

    def _apply_zoom_text(self) -> None:
        value = self.zoom.currentText().replace("%", "").strip()
        try:
            percent = int(value)
        except ValueError:
            percent = round(self.preview.zoomFactor() * 100)
        self._set_zoom_percent(percent)

    def _change_zoom(self, multiplier: float) -> None:
        self._set_zoom_percent(round(self.preview.zoomFactor() * multiplier * 100))

    def _fit_to_width(self) -> None:
        self.preview.fitToWidth()
        self.zoom.blockSignals(True)
        self.zoom.setCurrentText("По ширине")
        self.zoom.blockSignals(False)


class ReasonDialog(QDialog):
    def __init__(self, title: str, prompt: str, parent=None, *, minimum_length: int = 3) -> None:
        super().__init__(parent)
        self.minimum_length = minimum_length
        self.setWindowTitle(title)
        self.setMinimumSize(520, 240)
        self.resize(560, 260)
        layout = QVBoxLayout(self)
        label = QLabel(prompt)
        label.setWordWrap(True)
        layout.addWidget(label)
        self.editor = QTextEdit()
        self.editor.setPlaceholderText("Опишите причину…")
        self.editor.setMinimumHeight(110)
        self.editor.setAccessibleName(prompt)
        layout.addWidget(self.editor, 1)
        self.error = QLabel()
        self.error.setObjectName("formError")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("ОК")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        self.buttons.accepted.connect(self._accept_reason)
        self.buttons.rejected.connect(self.reject)
        self.editor.textChanged.connect(self._update_state)
        layout.addWidget(self.buttons)
        self._update_state()
        QTimer.singleShot(0, self.editor.setFocus)

    def reason(self) -> str:
        return self.editor.toPlainText().strip()

    def _update_state(self) -> None:
        valid = len(self.reason()) >= self.minimum_length
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(valid)
        if valid:
            self.error.clear()
            self.error.hide()

    def _accept_reason(self) -> None:
        if len(self.reason()) < self.minimum_length:
            self.error.setText(
                f"Опишите причину подробнее — минимум {self.minimum_length} символа."
            )
            self.error.show()
            return
        self.accept()


class ReferenceDialog(QDialog):
    def __init__(
        self,
        kind: str,
        item: dict[str, Any] | None = None,
        parent=None,
        *,
        references: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self.kind = kind
        self.item = item or {}
        self.references = references or {}
        titles = {"subjects": "Предмет", "rooms": "Кабинет", "groups": "Группа"}
        self.setWindowTitle(titles[kind])
        self.setMinimumWidth(420)
        if kind == "subjects":
            self.setMinimumSize(620, 520)
        layout = QVBoxLayout(self)
        self.error_label = QLabel()
        self.error_label.setObjectName("formError")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        form = QFormLayout()
        configure_form_layout(form)
        self.name = QLineEdit(str(self.item.get("name", "")))
        self.capacity = QSpinBox()
        self.capacity.setRange(1, 1000)
        self.capacity.setValue(int(self.item.get("capacity", 12)))
        self.color_value = str(self.item.get("color", "#2563eb"))
        self.color_button = QPushButton()
        self.color_button.setProperty("kind", "secondary")
        self.color_button.clicked.connect(self._choose_color)
        self._update_color_button()
        self.active = QCheckBox("Используется")
        self.active.setChecked(bool(self.item.get("active", True)))
        self.group_subject = SearchableComboBox(placeholder="Предмет")
        self.group_subject.addItem("Не выбран", None)
        for subject in self.references.get("subjects", []):
            self.group_subject.addItem(subject.get("name", ""), subject.get("id"))
        self.group_teacher = SearchableComboBox(placeholder="Фамилия или имя")
        self.group_teacher.addItem("Не выбран", None)
        self.group_duration = QSpinBox()
        self.group_duration.setRange(5, 1440)
        self.group_duration.setSuffix(" мин")
        self.group_duration.setValue(int(self.item.get("default_duration_minutes", 60)))
        self.subject_teacher_search = QLineEdit()
        self.subject_teacher_search.setPlaceholderText("Поиск по фамилии или имени…")
        self.subject_teacher_search.setClearButtonEnabled(True)
        self.subject_teacher_search.textChanged.connect(self._filter_subject_teachers)
        self.subject_teachers = QTableWidget(0, 2)
        self.subject_teachers.setHorizontalHeaderLabels(["Выбрать", "Преподаватель"])
        self.subject_teachers.verticalHeader().setVisible(False)
        self.subject_teachers.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.subject_teachers.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed
        )
        self.subject_teachers.setColumnWidth(0, 90)
        self.subject_teachers.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        selected_teacher_ids = set(self.item.get("teacher_ids") or [])
        teachers = self.references.get("teachers", [])
        self.subject_teachers.setRowCount(len(teachers))
        for row, teacher in enumerate(teachers):
            check = QCheckBox()
            check.setChecked(teacher.get("id") in selected_teacher_ids)
            check.setProperty("teacher_id", teacher.get("id"))
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            holder_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            holder_layout.addWidget(check)
            self.subject_teachers.setCellWidget(row, 0, holder)
            self.subject_teachers.setItem(
                row, 1, QTableWidgetItem(str(teacher.get("full_name", "")))
            )
        form.addRow("Название", self.name)
        if kind == "rooms":
            form.addRow("Вместимость", self.capacity)
        if kind == "subjects":
            form.addRow("Цвет в календаре", self.color_button)
        if kind == "groups":
            form.addRow("Предмет", self.group_subject)
            form.addRow("Преподаватель", self.group_teacher)
            form.addRow("Продолжительность", self.group_duration)
            self.group_subject.setCurrentIndex(
                max(0, self.group_subject.findData(self.item.get("subject_id")))
            )
            self._reload_group_teachers(preferred=self.item.get("default_teacher_id"))
            self.group_subject.currentIndexChanged.connect(self._reload_group_teachers)
        form.addRow("", self.active)
        layout.addLayout(form)
        if kind == "subjects":
            teacher_label = QLabel("Преподаватели, которые могут вести предмет")
            teacher_label.setObjectName("controlGroupLabel")
            layout.addWidget(teacher_label)
            layout.addWidget(self.subject_teacher_search)
            layout.addWidget(self.subject_teachers, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept_if_valid(self) -> None:
        if not self.name.text().strip():
            self.error_label.setText("Укажите название.")
            self.error_label.show()
            return
        if self.kind == "groups":
            invalid = []
            for label, field in (
                ("предмет", self.group_subject),
                ("преподавателя", self.group_teacher),
            ):
                if field.currentText() != "Не выбран" and field.currentData() is None:
                    invalid.append(label)
            if invalid:
                self.error_label.setText("Выберите значение из списка: " + ", ".join(invalid) + ".")
                self.error_label.show()
                return
        self.error_label.hide()
        self.accept()

    def _choose_color(self) -> None:
        selected = QColorDialog.getColor(
            QColor(self.color_value), self, "Цвет предмета в календаре"
        )
        if selected.isValid():
            self.color_value = selected.name()
            self._update_color_button()

    def _update_color_button(self) -> None:
        color = QColor(self.color_value)
        text_color = "#ffffff" if color.lightness() < 145 else "#172033"
        self.color_button.setText("Выбрать цвет")
        self.color_button.setStyleSheet(
            f"background: {color.name()}; color: {text_color}; border: 1px solid #aeb9c8;"
        )
        self.color_button.setToolTip("Нажмите, чтобы выбрать цвет из палитры")

    def _filter_subject_teachers(self, text: str) -> None:
        for row in range(self.subject_teachers.rowCount()):
            item = self.subject_teachers.item(row, 1)
            self.subject_teachers.setRowHidden(
                row, item is None or not matches_word_prefix(text, item.text())
            )

    def _reload_group_teachers(
        self, _index: int | None = None, *, preferred: object = None
    ) -> None:
        current = preferred if preferred is not None else self.group_teacher.currentData()
        teachers = _teachers_for_subject(self.references, self.group_subject.currentData())
        self.group_teacher.blockSignals(True)
        self.group_teacher.clear()
        self.group_teacher.addItem("Не выбран", None)
        for teacher in teachers:
            self.group_teacher.addItem(teacher.get("full_name", ""), teacher.get("id"))
        selected = self.group_teacher.findData(current)
        self.group_teacher.setCurrentIndex(selected if selected >= 0 else 0)
        self.group_teacher.blockSignals(False)

    def _subject_teacher_ids(self) -> list[int]:
        result = []
        for row in range(self.subject_teachers.rowCount()):
            holder = self.subject_teachers.cellWidget(row, 0)
            check = holder.findChild(QCheckBox) if holder else None
            if check and check.isChecked():
                result.append(int(check.property("teacher_id")))
        return result

    def payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": " ".join(self.name.text().split()),
            "active": self.active.isChecked(),
        }
        if self.kind == "rooms":
            result["capacity"] = self.capacity.value()
        elif self.kind == "subjects":
            result["color"] = self.color_value
            result["teacher_ids"] = self._subject_teacher_ids()
        elif self.kind == "groups":
            result["subject_id"] = self.group_subject.currentData()
            result["default_teacher_id"] = self.group_teacher.currentData()
            result["default_duration_minutes"] = self.group_duration.value()
        return result


class LessonDialog(QDialog):
    def __init__(
        self, references: dict[str, Any], lesson: dict[str, Any] | None = None, parent=None
    ) -> None:
        super().__init__(parent)
        self.references = references
        self.lesson = lesson or {}
        self._group_member_ids: set[int] = set()
        self.setWindowTitle("Занятие")
        self.setMinimumSize(800, 740)
        self.resize(880, 840)
        layout = QVBoxLayout(self)
        self.error_label = QLabel()
        self.error_label.setObjectName("formError")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        form = QFormLayout()
        configure_form_layout(form)
        self.subject = self._combo(references.get("subjects", []), "name")
        self.teacher = self._combo([], "full_name", empty="Выберите преподавателя")
        self.room = self._combo(references.get("rooms", []), "name")
        self.group = self._combo(references.get("groups", []), "name", empty="Без группы")
        now = now_center().replace(second=0, microsecond=0)
        rounded = now + timedelta(minutes=(30 - now.minute % 30) % 30)
        self.start = QDateTimeEdit(QDateTime(rounded))
        self.start.setCalendarPopup(True)
        configure_calendar(self.start)
        self.start.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.duration = QSpinBox()
        self.duration.setRange(5, 1440)
        self.duration.setSingleStep(5)
        self.duration.setSuffix(" мин")
        self.duration.setValue(60)
        self.end_display = QLineEdit()
        self.end_display.setReadOnly(True)
        self.end_display.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.end_display.setAccessibleName("Рассчитанное время окончания занятия")
        self.start.dateTimeChanged.connect(self._update_end_display)
        self.duration.valueChanged.connect(self._update_end_display)
        self.students = QTableWidget(0, 3)
        self.students.setHorizontalHeaderLabels(["Выбрать", "Ученик", "Источник"])
        self.students.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.students.verticalHeader().setVisible(False)
        self.students.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.students.setColumnWidth(0, 90)
        self.students.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.students.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.students.setColumnWidth(2, 125)
        self.students.setMinimumHeight(290)
        selected = {p.get("person_id") for p in self.lesson.get("participants", [])}
        self._manual_participant_ids = {int(person_id) for person_id in selected if person_id}
        all_students = references.get("students", [])
        self.students.setRowCount(len(all_students))
        for row, person in enumerate(all_students):
            check = QCheckBox()
            check.setChecked(person.get("id") in selected)
            check.setProperty("person_id", person.get("id"))
            check.toggled.connect(
                lambda checked, widget=check: self._student_checkbox_toggled(widget, checked)
            )
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            holder_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            holder_layout.addWidget(check)
            self.students.setCellWidget(row, 0, holder)
            self.students.setItem(row, 1, QTableWidgetItem(person.get("full_name", "")))
            self.students.setItem(row, 2, QTableWidgetItem("Вручную" if check.isChecked() else "—"))
        self.student_search = QLineEdit()
        self.student_search.setPlaceholderText("Введите фамилию или имя…")
        self.student_search.setClearButtonEnabled(True)
        self.student_search.setAccessibleName("Поиск участников занятия")
        self.student_search.textChanged.connect(self._filter_students)
        self.selected_only = QCheckBox("Только выбранные")
        self.selected_only.toggled.connect(self._filter_students)
        self.participant_count = QLabel()
        self.participant_count.setObjectName("supportingText")
        self.notes = QTextEdit(str(self.lesson.get("notes") or ""))
        self.notes.setMaximumHeight(72)
        self.repeat = QCheckBox("Повторять еженедельно")
        self.occurrences = QSpinBox()
        self.occurrences.setRange(2, 104)
        self.occurrences.setValue(4)
        form.addRow("Предмет", self.subject)
        form.addRow("Преподаватель", self.teacher)
        form.addRow("Кабинет", self.room)
        form.addRow("Группа", self.group)
        form.addRow("Начало", self.start)
        form.addRow("Продолжительность", self.duration)
        form.addRow("Окончание", self.end_display)
        if not self.lesson:
            form.addRow("", self.repeat)
            form.addRow("Количество занятий", self.occurrences)
            form.setRowVisible(self.occurrences, False)
            self.repeat.toggled.connect(
                lambda checked: form.setRowVisible(self.occurrences, checked)
            )
        layout.addLayout(form)
        participant_header = QHBoxLayout()
        participant_label = QLabel("Участники")
        participant_label.setObjectName("controlGroupLabel")
        participant_header.addWidget(participant_label)
        participant_header.addWidget(self.student_search, 1)
        participant_header.addWidget(self.selected_only)
        participant_header.addWidget(self.participant_count)
        layout.addLayout(participant_header)
        layout.addWidget(self.students, 1)
        layout.addWidget(QLabel("Заметка"))
        layout.addWidget(self.notes)
        self._select(self.subject, self.lesson.get("subject_id"))
        self._reload_lesson_teachers(preferred=self.lesson.get("teacher_id"))
        self.subject.currentIndexChanged.connect(self._reload_lesson_teachers)
        self._select(self.room, self.lesson.get("room_id"))
        self._select(self.group, self.lesson.get("group_id"))
        if self.lesson.get("start_at"):
            self.set_period(self.lesson["start_at"], self.lesson["end_at"])
        else:
            self._update_end_display()
        initial_group_ids = self._active_group_member_ids()
        self._manual_participant_ids.difference_update(initial_group_ids)
        self._refresh_group_participants()
        self.group.currentIndexChanged.connect(self._apply_group_defaults)
        self.start.dateTimeChanged.connect(self._refresh_group_participants)
        self._student_selection_changed()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply_group_defaults(self) -> None:
        group_id = self.group.currentData()
        group = next(
            (item for item in self.references.get("groups", []) if item.get("id") == group_id),
            None,
        )
        if group is None:
            self._refresh_group_participants()
            return
        self._select(self.subject, group.get("subject_id"))
        self._reload_lesson_teachers(preferred=group.get("default_teacher_id"))
        duration = int(group.get("default_duration_minutes") or 60)
        self.duration.setValue(duration)
        self._refresh_group_participants()

    def _active_group_member_ids(self) -> set[int]:
        group_id = self.group.currentData()
        group = next(
            (item for item in self.references.get("groups", []) if item.get("id") == group_id),
            None,
        )
        if group is None:
            return set()
        lesson_start = center_wall_time(self.start.dateTime().toPython())
        result: set[int] = set()
        for membership in group.get("memberships", []):
            starts_at = parse_center(membership["start_at"])
            ends_at = (
                parse_center(membership["end_at"])
                if membership.get("end_at")
                else None
            )
            if starts_at <= lesson_start and (ends_at is None or lesson_start < ends_at):
                result.add(int(membership["person_id"]))
        return result

    def _refresh_group_participants(self, _value: object = None) -> None:
        self._group_member_ids = self._active_group_member_ids()
        for row in range(self.students.rowCount()):
            holder = self.students.cellWidget(row, 0)
            check = holder.findChild(QCheckBox) if holder else None
            if check is None:
                continue
            person_id = int(check.property("person_id"))
            from_group = person_id in self._group_member_ids
            check.blockSignals(True)
            check.setChecked(from_group or person_id in self._manual_participant_ids)
            check.setEnabled(not from_group)
            check.setToolTip(
                "Участник группы добавляется автоматически"
                if from_group
                else "Дополнительный участник занятия"
            )
            check.blockSignals(False)
            source = self.students.item(row, 2)
            if source is not None:
                source.setText(
                    "Из группы"
                    if from_group
                    else "Дополнительно"
                    if check.isChecked()
                    else "—"
                )
        self._student_selection_changed()

    def _student_checkbox_toggled(self, check: QCheckBox, checked: bool) -> None:
        person_id = int(check.property("person_id"))
        if checked:
            self._manual_participant_ids.add(person_id)
        else:
            self._manual_participant_ids.discard(person_id)
        self._refresh_group_participants()

    def _reload_lesson_teachers(
        self, _index: int | None = None, *, preferred: object = None
    ) -> None:
        current = preferred if preferred is not None else self.teacher.currentData()
        teachers = _teachers_for_subject(self.references, self.subject.currentData())
        self.teacher.blockSignals(True)
        self.teacher.clear()
        self.teacher.addItem("Выберите преподавателя", None)
        for teacher in teachers:
            self.teacher.addItem(teacher.get("full_name", ""), teacher.get("id"))
        selected = self.teacher.findData(current)
        self.teacher.setCurrentIndex(selected if selected >= 0 else 0)
        self.teacher.blockSignals(False)

    def set_period(self, start_at: str, end_at: str) -> None:
        start = QDateTime.fromString(start_at, Qt.DateFormat.ISODate)
        end = QDateTime.fromString(end_at, Qt.DateFormat.ISODate)
        if start.isValid():
            self.start.setDateTime(start)
        if start.isValid() and end.isValid():
            self.duration.setValue(max(5, start.secsTo(end) // 60))
        self._update_end_display()

    def _end_datetime(self) -> QDateTime:
        return self.start.dateTime().addSecs(self.duration.value() * 60)

    def _update_end_display(self, _value: object = None) -> None:
        end = self._end_datetime()
        if end.date() == self.start.date():
            text = end.toString("HH:mm")
        else:
            text = end.toString("dd.MM.yyyy HH:mm")
        self.end_display.setText(text)

    def _filter_students(self, _value: object = None) -> None:
        query = self.student_search.text()
        selected_only = self.selected_only.isChecked()
        for row in range(self.students.rowCount()):
            item = self.students.item(row, 1)
            holder = self.students.cellWidget(row, 0)
            check = holder.findChild(QCheckBox) if holder else None
            matches = item is not None and matches_word_prefix(query, item.text())
            hide_selected = bool(selected_only and not check.isChecked())
            self.students.setRowHidden(row, not matches or hide_selected)

    def _student_selection_changed(self, _checked: bool = False) -> None:
        selected = 0
        for row in range(self.students.rowCount()):
            holder = self.students.cellWidget(row, 0)
            check = holder.findChild(QCheckBox) if holder else None
            selected += int(bool(check and check.isChecked()))
        extras = max(0, selected - len(self._group_member_ids))
        if self._group_member_ids:
            self.participant_count.setText(
                f"Из группы: {len(self._group_member_ids)} · доп.: {extras}"
            )
        else:
            self.participant_count.setText(f"Выбрано: {selected}")
        if self.selected_only.isChecked():
            self._filter_students()

    def _accept_if_valid(self) -> None:
        missing = []
        for label, field in (
            ("предмет", self.subject),
            ("преподавателя", self.teacher),
            ("кабинет", self.room),
        ):
            if field.currentData() is None:
                missing.append(label)
        if missing:
            self.show_error("Выберите " + ", ".join(missing) + ".")
            return
        self.error_label.hide()
        self.accept()

    def show_error(self, message: str) -> None:
        self.error_label.setText(f"Не удалось сохранить занятие. {message}")
        self.error_label.show()

    @staticmethod
    def _combo(
        items: list[dict[str, Any]],
        label: str,
        empty: str | None = None,
        *,
        include_inactive: bool = False,
    ) -> QComboBox:
        combo = SearchableComboBox(
            placeholder="Фамилия или имя" if label == "full_name" else "Начните вводить…"
        )
        if empty:
            combo.addItem(empty, None)
        for item in items:
            if include_inactive or item.get("active", True):
                combo.addItem(str(item.get(label, "")), item.get("id"))
        return combo

    @staticmethod
    def _select(combo: QComboBox, value: object) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def payload(self) -> dict[str, Any]:
        participant_ids = []
        for row in range(self.students.rowCount()):
            check = self.students.cellWidget(row, 0).findChild(QCheckBox)
            if check and check.isChecked():
                participant_ids.append(int(check.property("person_id")))
        start = center_wall_time(self.start.dateTime().toPython())
        end = center_wall_time(self._end_datetime().toPython())
        return {
            "subject_id": self.subject.currentData(),
            "teacher_id": self.teacher.currentData(),
            "room_id": self.room.currentData(),
            "group_id": self.group.currentData(),
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "participant_ids": participant_ids,
            "notes": self.notes.toPlainText().strip() or None,
        }

    def series_payload(self) -> dict[str, Any]:
        payload = self.payload()
        return {
            "subject_id": payload["subject_id"],
            "teacher_id": payload["teacher_id"],
            "room_id": payload["room_id"],
            "group_id": payload["group_id"],
            "starts_at": payload["start_at"],
            "duration_minutes": self.duration.value(),
            "interval_weeks": 1,
            "occurrences": self.occurrences.value(),
            "participant_ids": payload["participant_ids"],
            "notes": payload["notes"],
        }


class LessonCardDialog(QDialog):
    def __init__(
        self,
        lesson: dict[str, Any],
        parent=None,
        *,
        open_person: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.lesson = lesson
        self.operation: str | None = None
        self.setWindowTitle("Карточка занятия")
        self.setMinimumSize(720, 500)
        layout = QVBoxLayout(self)
        title = QLabel(str(lesson.get("subject_name_snapshot", "Занятие")))
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        start = parse_center(lesson["start_at"])
        end = parse_center(lesson["end_at"])
        details = QLabel(
            f"План: {start:%d.%m.%Y, %H:%M}–{end:%H:%M} · "
            f"{lesson.get('teacher_name_snapshot', '')} · "
            f"{lesson.get('room_name_snapshot', '')} · "
            f"{STATUS_LABELS.get(lesson.get('status'), lesson.get('status', ''))}"
        )
        layout.addWidget(details)
        segments = lesson.get("teacher_segments", [])
        if segments:
            actual_lines = [
                "Плановый преподаватель: " + str(lesson.get("teacher_name_snapshot", ""))
            ]
            for segment in segments:
                segment_start = parse_center(segment["started_at"])
                segment_end_value = segment.get("ended_at")
                segment_end = (
                    parse_center(segment_end_value).strftime("%H:%M")
                    if segment_end_value
                    else "сейчас"
                )
                actual_lines.append(
                    f"{segment_start:%H:%M}–{segment_end} "
                    f"{segment.get('teacher_name_snapshot', '')}"
                )
            actual_teachers = QLabel("Фактически:\n" + "\n".join(actual_lines))
            actual_teachers.setWordWrap(True)
            layout.addWidget(actual_teachers)
        self.actual_start: QDateTimeEdit | None = None
        self.actual_end: QDateTimeEdit | None = None
        self._actual_original: tuple[str | None, str | None] = (
            lesson.get("actual_start_at"),
            lesson.get("actual_end_at"),
        )
        if lesson.get("status") == "completed" and all(self._actual_original):
            actual_start = parse_center(self._actual_original[0])
            actual_end = parse_center(self._actual_original[1])
            duration = max(0, int((actual_end - actual_start).total_seconds() // 60))
            actual_form = QFormLayout()
            configure_form_layout(actual_form)
            self.actual_start = QDateTimeEdit(QDateTime(actual_start))
            self.actual_end = QDateTimeEdit(QDateTime(actual_end))
            for editor in (self.actual_start, self.actual_end):
                editor.setDisplayFormat("dd.MM.yyyy HH:mm")
                editor.setCalendarPopup(True)
                configure_calendar(editor)
            actual_form.addRow("Фактическое начало", self.actual_start)
            actual_form.addRow("Фактическое окончание", self.actual_end)
            actual_form.addRow("Продолжительность", QLabel(f"{duration} мин"))
            layout.addLayout(actual_form)
        self.table = _table(
            ["Участник", "Посещение", "Приход", "Уход", "Опоздание", "Отмена"],
            stretch=(0, 5),
            compact=(1, 2, 3, 4),
        )
        participants = lesson.get("participants", [])
        self.table.setRowCount(len(participants))
        editable = lesson.get("status") != "cancelled"
        for row, participant in enumerate(participants):
            name = QTableWidgetItem(participant.get("person_name_snapshot", ""))
            name.setData(Qt.ItemDataRole.UserRole, participant.get("person_id"))
            self.table.setItem(row, 0, name)
            combo = SafeComboBox()
            allowed = ATTENDANCE_LABELS.items()
            if lesson.get("status") == "in_progress":
                allowed = [
                    (value, label)
                    for value, label in ATTENDANCE_LABELS.items()
                    if value not in {"excused", "left_early"}
                    or value == participant.get("attendance_status")
                ]
            for value, label in allowed:
                combo.addItem(label, value)
            combo.setCurrentIndex(combo.findData(participant.get("attendance_status")))
            combo.setEnabled(
                editable and participant.get("attendance_status") not in {"excused", "left_early"}
            )
            self.table.setCellWidget(row, 1, combo)
            for column, key in ((2, "arrived_at"), (3, "left_at")):
                value = participant.get(key)
                text_value = f"{parse_center(value):%H:%M}" if value else "—"
                self.table.setItem(row, column, QTableWidgetItem(text_value))
            self.table.setItem(
                row,
                4,
                QTableWidgetItem(str(participant.get("late_minutes") or "—")),
            )
            cancellation = "—"
            if participant.get("attendance_status") == "excused":
                actor = {
                    "student": "ученик",
                    "guardian": "родитель",
                    "administrator": "администратор",
                }.get(participant.get("cancelled_by"), "не указан")
                reason = participant.get("cancellation_reason") or "без причины"
                cancelled_at = participant.get("cancelled_at")
                when = (
                    parse_center(cancelled_at).strftime("%d.%m.%Y %H:%M")
                    if cancelled_at
                    else "время не указано"
                )
                cancellation = f"{actor}, {when}: {reason}"
            self.table.setItem(row, 5, QTableWidgetItem(cancellation))
        if open_person is not None:
            self.table.cellDoubleClicked.connect(
                lambda row, column: (
                    open_person(int(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)))
                    if column == 0 and self.table.item(row, 0) is not None
                    else None
                )
            )
        layout.addWidget(self.table)
        if lesson.get("status") == "in_progress":
            operations = QHBoxLayout()
            operations.addWidget(
                _button(
                    "Ученик покинул занятие",
                    lambda: self._select_operation("leave_early"),
                    "warning",
                )
            )
            operations.addWidget(
                _button(
                    "Отменить участие",
                    lambda: self._select_operation("cancel_participant"),
                )
            )
            operations.addStretch(1)
            operations.addWidget(
                _button(
                    "Преподаватель не может продолжить",
                    lambda: self._select_operation("teacher_exit"),
                    "warning",
                )
            )
            operations.addWidget(
                _button(
                    "Завершить досрочно",
                    lambda: self._select_operation("finish_early"),
                    "danger",
                )
            )
            layout.addLayout(operations)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Закрыть")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _select_operation(self, operation: str) -> None:
        if operation in {"leave_early", "cancel_participant"} and self.table.currentRow() < 0:
            QMessageBox.information(self, "Участник", "Сначала выберите ученика в таблице.")
            return
        if operation in {"leave_early", "cancel_participant"}:
            status = self.table.cellWidget(self.table.currentRow(), 1).currentData()
            if operation == "leave_early" and status not in {"present", "late"}:
                QMessageBox.information(
                    self,
                    "Досрочный уход",
                    "Отметить уход можно только для ученика со статусом «Пришёл» или «Опоздал».",
                )
                return
            if operation == "cancel_participant" and status != "expected":
                QMessageBox.information(
                    self,
                    "Отмена участия",
                    "Отменить можно только ещё не начавшееся участие ученика.",
                )
                return
        self.operation = operation
        self.accept()

    def selected_person_id(self) -> int | None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return int(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None

    def attendance(self) -> list[tuple[int, str]]:
        result = []
        for row in range(self.table.rowCount()):
            person_id = int(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole))
            combo = self.table.cellWidget(row, 1)
            result.append((person_id, str(combo.currentData())))
        return result

    def actual_time_change(self) -> tuple[str, str] | None:
        if self.actual_start is None or self.actual_end is None:
            return None
        start = self.actual_start.dateTime().toString(Qt.DateFormat.ISODate)
        end = self.actual_end.dateTime().toString(Qt.DateFormat.ISODate)
        original = tuple(
            QDateTime.fromString(str(value), Qt.DateFormat.ISODate).toString(Qt.DateFormat.ISODate)
            for value in self._actual_original
        )
        return None if (start, end) == original else (start, end)


class GroupMembersDialog(QDialog):
    def __init__(
        self,
        group: dict[str, Any],
        students: list[dict[str, Any]],
        memberships: list[dict[str, Any]],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.group = group
        self.students = students
        self.memberships = [dict(item) for item in memberships]
        self.setWindowTitle(f"Состав группы · {group.get('name', '')}")
        self.setMinimumSize(720, 360)
        self.resize(760, 440)
        layout = QVBoxLayout(self)
        actions = QHBoxLayout()
        member_label = QLabel("Новый участник")
        member_label.setObjectName("controlGroupLabel")
        actions.addWidget(member_label)
        self.student = SearchableComboBox(placeholder="Фамилия или имя")
        self.student.setMinimumWidth(280)
        member_label.setBuddy(self.student)
        for person in students:
            self.student.addItem(person.get("full_name", ""), person.get("id"))
        actions.addWidget(self.student, 1)
        actions.addWidget(_button("Добавить", self._add, "primary"))
        actions.addWidget(_button("Завершить участие", self._end, "warning"))
        layout.addLayout(actions)
        self.table = _table(
            ["Ученик", "Начало", "Окончание", "Состояние"],
            stretch=(0,),
            compact=(1, 2, 3),
        )
        layout.addWidget(self.table)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._render()

    def _add(self) -> None:
        person_id = self.student.currentData()
        if person_id is None or any(
            item.get("person_id") == person_id and not item.get("end_at")
            for item in self.memberships
        ):
            return
        self.memberships.append(
            {
                "person_id": person_id,
                "person_name": self.student.currentText(),
                "_new": True,
            }
        )
        self._render()

    def _end(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        item = self.memberships[row]
        if item.get("id") and not item.get("end_at"):
            item["_end"] = True
        elif item.get("_new"):
            self.memberships.pop(row)
        self._render()

    def _render(self) -> None:
        self.table.setRowCount(len(self.memberships))
        for row, item in enumerate(self.memberships):
            start = item.get("start_at")
            end = item.get("end_at")
            values = [
                item.get("person_name", ""),
                parse_center(start).strftime("%d.%m.%Y")
                if start
                else "После сохранения",
                parse_center(end).strftime("%d.%m.%Y") if end else "—",
                "Будет завершено" if item.get("_end") else "Активно" if not end else "Завершено",
            ]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))

    def changes(self) -> tuple[list[int], list[int]]:
        additions = [int(item["person_id"]) for item in self.memberships if item.get("_new")]
        endings = [int(item["id"]) for item in self.memberships if item.get("_end")]
        return additions, endings


class FreeSlotDialog(QDialog):
    search_requested = Signal(dict)

    def __init__(self, references: dict[str, Any], parent=None) -> None:
        super().__init__(parent)
        self.references = references
        self.slots: list[dict[str, Any]] = []
        self.setWindowTitle("Найти свободное время")
        self.setMinimumSize(680, 560)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        configure_form_layout(form)
        self.day = QDateEdit(QDate.currentDate())
        self.day.setCalendarPopup(True)
        configure_calendar(self.day)
        self.duration = QSpinBox()
        self.duration.setRange(5, 480)
        self.duration.setValue(60)
        self.duration.setSuffix(" мин")
        self.teacher = LessonDialog._combo(
            references.get("teachers", []),
            "full_name",
            include_inactive=True,
        )
        self.room = LessonDialog._combo(references.get("rooms", []), "name", empty="Любой кабинет")
        form.addRow("Дата", self.day)
        form.addRow("Продолжительность", self.duration)
        form.addRow("Преподаватель", self.teacher)
        form.addRow("Предпочитаемый кабинет", self.room)
        layout.addLayout(form)
        student_header = QHBoxLayout()
        student_label = QLabel("Ученики")
        student_label.setObjectName("controlGroupLabel")
        student_header.addWidget(student_label)
        self.student_search = QLineEdit()
        self.student_search.setPlaceholderText("Введите фамилию или имя…")
        self.student_search.setClearButtonEnabled(True)
        self.student_search.textChanged.connect(self._filter_students)
        student_header.addWidget(self.student_search, 1)
        self.selected_only = QCheckBox("Только выбранные")
        self.selected_only.toggled.connect(self._filter_students)
        student_header.addWidget(self.selected_only)
        self.student_count = QLabel("Выбрано: 0")
        self.student_count.setObjectName("supportingText")
        student_header.addWidget(self.student_count)
        layout.addLayout(student_header)
        self.students = QTableWidget(0, 2)
        self.students.setHorizontalHeaderLabels(["Выбрать", "ФИО"])
        self.students.verticalHeader().setVisible(False)
        self.students.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.students.setColumnWidth(0, 90)
        self.students.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        people = references.get("students", [])
        self.students.setRowCount(len(people))
        for row, person in enumerate(people):
            check = QCheckBox()
            check.setProperty("person_id", person.get("id"))
            check.toggled.connect(self._student_selection_changed)
            self.students.setCellWidget(row, 0, check)
            self.students.setItem(row, 1, QTableWidgetItem(person.get("full_name", "")))
        self.students.setMinimumHeight(150)
        self.students.setMaximumHeight(220)
        layout.addWidget(self.students)
        search_row = QHBoxLayout()
        search_row.addStretch(1)
        self.search_button = _button("Найти варианты", self._request, "primary")
        search_row.addWidget(self.search_button)
        layout.addLayout(search_row)
        self.search_status = QLabel("Задайте параметры и нажмите «Найти варианты».")
        self.search_status.setObjectName("supportingText")
        self.search_status.setWordWrap(True)
        layout.addWidget(self.search_status)
        self.results = _table(["Дата", "Время", "Кабинет"], stretch=(2,), compact=(0, 1))
        self.results.doubleClicked.connect(lambda _index: self.accept())
        self.results.itemSelectionChanged.connect(self._update_create_button)
        layout.addWidget(self.results, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.create_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.create_button.setText("Создать занятие")
        self.create_button.setEnabled(False)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _filter_students(self, _value: object = None) -> None:
        query = self.student_search.text()
        selected_only = self.selected_only.isChecked()
        for row in range(self.students.rowCount()):
            name = self.students.item(row, 1)
            check = self.students.cellWidget(row, 0)
            matches = name is not None and matches_word_prefix(query, name.text())
            hide_selected = bool(
                selected_only and isinstance(check, QCheckBox) and not check.isChecked()
            )
            self.students.setRowHidden(row, not matches or hide_selected)

    def _student_selection_changed(self, _checked: bool = False) -> None:
        selected = sum(
            int(
                isinstance(self.students.cellWidget(row, 0), QCheckBox)
                and self.students.cellWidget(row, 0).isChecked()
            )
            for row in range(self.students.rowCount())
        )
        self.student_count.setText(f"Выбрано: {selected}")
        if self.selected_only.isChecked():
            self._filter_students()

    def criteria(self) -> dict[str, Any]:
        student_ids = []
        for row in range(self.students.rowCount()):
            check = self.students.cellWidget(row, 0)
            if isinstance(check, QCheckBox) and check.isChecked():
                student_ids.append(int(check.property("person_id")))
        return {
            "day": self.day.date().toString(Qt.DateFormat.ISODate),
            "duration_minutes": self.duration.value(),
            "teacher_id": self.teacher.currentData(),
            "room_id": self.room.currentData(),
            "student_ids": student_ids,
        }

    def _request(self) -> None:
        self.search_button.setEnabled(False)
        self.search_button.setText("Ищем…")
        self.search_status.setText("Идёт поиск свободного времени…")
        self.results.clearContents()
        self.results.setRowCount(0)
        self.create_button.setEnabled(False)
        self.search_requested.emit(self.criteria())

    def set_slots(self, slots: list[dict[str, Any]]) -> None:
        self.slots = slots
        self.search_button.setEnabled(True)
        self.search_button.setText("Найти варианты")
        self.search_status.setText(
            f"Найдено вариантов: {len(slots)}. Выберите подходящий."
            if slots
            else "Подходящих вариантов не найдено."
        )
        self.results.setRowCount(len(slots))
        for row, slot in enumerate(slots):
            start = parse_center(slot["start_at"])
            end = parse_center(slot["end_at"])
            for column, value in enumerate(
                (f"{start:%d.%m.%Y}", f"{start:%H:%M}–{end:%H:%M}", slot.get("room_name", ""))
            ):
                cell = QTableWidgetItem(str(value))
                cell.setData(Qt.ItemDataRole.UserRole, slot)
                self.results.setItem(row, column, cell)
        self._update_create_button()

    def search_failed(self, message: str) -> None:
        self.search_button.setEnabled(True)
        self.search_button.setText("Найти варианты")
        self.search_status.setText("Не удалось выполнить поиск. Повторите попытку.")
        QMessageBox.critical(self, "Свободное время", message)

    def _update_create_button(self) -> None:
        self.create_button.setEnabled(self.results.currentRow() >= 0)

    def selected_slot(self) -> dict[str, Any] | None:
        row = self.results.currentRow()
        return self.results.item(row, 0).data(Qt.ItemDataRole.UserRole) if row >= 0 else None


class LearningPage(QWidget):
    notifications_changed = Signal(list)
    person_requested = Signal(int)

    def __init__(self, api: ManagementApi, parent=None) -> None:
        super().__init__(parent)
        self.api = api
        self.pool = QThreadPool(self)
        self._workers: set[Worker] = set()
        self._closing = False
        self.references: dict[str, Any] = {}
        self.today_data: dict[str, Any] = {}
        self.calendar_lessons: list[dict[str, Any]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("clientTabs")
        self.tabs.addTab(self._today_tab(), "Сегодня")
        self.tabs.addTab(self._calendar_tab(), "Календарь")
        self.tabs.addTab(self._references_tab(), "Справочники")
        self.tabs.addTab(self._journals_tab(), "Журналы")
        layout.addWidget(self.tabs)
        self.refresh()
        self.live_timer = QTimer(self)
        self.live_timer.setInterval(10_000)
        self.live_timer.timeout.connect(self._auto_refresh)
        self.live_timer.start()

    def _auto_refresh(self) -> None:
        if (
            not self._closing
            and self.isVisible()
            and QApplication.activeModalWidget() is None
            and not self._workers
        ):
            self.refresh()

    def _today_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(_button("Добавить занятие", self.add_lesson, "primary"))
        actions.addWidget(_button("Найти свободное время", self.find_free_time))
        actions.addStretch(1)
        layout.addLayout(actions)
        presence_panel = QFrame()
        presence_panel.setObjectName("controlPanel")
        presence_actions = QHBoxLayout(presence_panel)
        presence_actions.setContentsMargins(12, 8, 12, 8)
        presence_actions.setSpacing(8)
        presence_label = QLabel("Посещение клуба")
        presence_label.setObjectName("controlGroupLabel")
        presence_actions.addWidget(presence_label)
        self.presence_person = SearchableComboBox(placeholder="Фамилия или имя")
        self.presence_person.setMinimumWidth(280)
        presence_label.setBuddy(self.presence_person)
        presence_actions.addWidget(self.presence_person, 1)
        presence_actions.addWidget(_button("Пришёл", lambda: self.presence("arrival"), "primary"))
        self.departure_button = _button("Ушёл", lambda: self.presence("departure"), "warning")
        presence_actions.addWidget(self.departure_button)
        layout.addWidget(presence_panel)
        alert_row = QHBoxLayout()
        self.alert_label = QLabel()
        self.alert_label.setObjectName("connectionStatus")
        self.alert_label.setProperty("state", "loading")
        self.alert_label.setVisible(False)
        alert_row.addWidget(self.alert_label, 1)
        self.dismiss_alerts_button = _button("Прочитано", self.dismiss_alerts)
        self.dismiss_alerts_button.setVisible(False)
        alert_row.addWidget(self.dismiss_alerts_button)
        layout.addLayout(alert_row)
        self.today_lessons = _table(
            [
                "Время",
                "Предмет",
                "Преподаватель",
                "Кабинет",
                "Участники",
                "Статус",
                "Действия",
            ],
            stretch=(1, 2, 3),
            fixed={0: 110, 4: 90, 5: 125, 6: 290},
        )
        layout.addWidget(self.today_lessons, 2)
        present_header = QHBoxLayout()
        present_header.addWidget(QLabel("Сейчас в клубе"))
        present_hint = QLabel("Дважды щёлкните человека, чтобы отметить уход")
        present_hint.setObjectName("supportingText")
        present_header.addStretch(1)
        present_header.addWidget(present_hint)
        layout.addLayout(present_header)
        self.present_table = _table(["ФИО", "Время прихода"], stretch=(0,), compact=(1,))
        self.present_table.setMaximumHeight(180)
        self.present_table.setToolTip(
            "Дважды щёлкните строку, чтобы выбрать человека в поле посещения клуба"
        )
        self.present_table.cellDoubleClicked.connect(
            lambda row, _column: self._select_present_person(row)
        )
        layout.addWidget(self.present_table)
        return page

    def _calendar_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        filter_panel = QFrame()
        filter_panel.setObjectName("controlPanel")
        filters = QGridLayout(filter_panel)
        filters.setContentsMargins(12, 10, 12, 10)
        filters.setHorizontalSpacing(8)
        filters.setVerticalSpacing(8)
        period_label = QLabel("Период")
        period_label.setObjectName("controlGroupLabel")
        filters.addWidget(period_label, 0, 0)
        self.calendar_period = SafeComboBox()
        self.calendar_period.setMinimumWidth(190)
        self.calendar_period.addItem("День", 1)
        self.calendar_period.addItem("Неделя", 7)
        self.calendar_period.addItem("Произвольный период", 0)
        self.calendar_period.currentIndexChanged.connect(self._calendar_period_changed)
        self.calendar_period.setAccessibleName("Режим отображения расписания")
        filters.addWidget(self.calendar_period, 0, 1)
        from_label = QLabel("с")
        filters.addWidget(from_label, 0, 2)
        self.calendar_date = QDateEdit(QDate.currentDate())
        self.calendar_date.setDisplayFormat("dd.MM.yyyy")
        self.calendar_date.setMinimumWidth(145)
        self.calendar_date.setCalendarPopup(True)
        configure_calendar(self.calendar_date)
        self.calendar_date.dateChanged.connect(self._calendar_period_changed)
        self.calendar_date.setAccessibleName("Начало периода")
        from_label.setBuddy(self.calendar_date)
        filters.addWidget(self.calendar_date, 0, 3)
        to_label = QLabel("по")
        filters.addWidget(to_label, 0, 4)
        self.calendar_end = QDateEdit(QDate.currentDate())
        self.calendar_end.setDisplayFormat("dd.MM.yyyy")
        self.calendar_end.setCalendarPopup(True)
        configure_calendar(self.calendar_end)
        self.calendar_end.setEnabled(False)
        self.calendar_end.dateChanged.connect(self.load_calendar)
        self.calendar_end.setMinimumWidth(145)
        self.calendar_end.setAccessibleName("Окончание периода")
        to_label.setBuddy(self.calendar_end)
        filters.addWidget(self.calendar_end, 0, 5)
        filters.setColumnStretch(6, 1)

        object_label = QLabel("Показать")
        object_label.setObjectName("controlGroupLabel")
        filters.addWidget(object_label, 1, 0)
        self.calendar_filter_type = SafeComboBox()
        self.calendar_filter_type.setMinimumWidth(190)
        for label, value in (
            ("Весь клуб", None),
            ("Преподаватель", "teacher_id"),
            ("Ученик", "student_id"),
            ("Группа", "group_id"),
            ("Кабинет", "room_id"),
        ):
            self.calendar_filter_type.addItem(label, value)
        self.calendar_filter_type.currentIndexChanged.connect(self._calendar_filter_changed)
        self.calendar_filter_type.setAccessibleName("Тип фильтра расписания")
        filters.addWidget(self.calendar_filter_type, 1, 1)
        self.calendar_filter_value = SearchableComboBox(placeholder="Начните вводить…")
        self.calendar_filter_value.setMinimumWidth(320)
        self.calendar_filter_value.setAccessibleName("Значение фильтра расписания")
        self.calendar_filter_value.setVisible(False)
        self.calendar_filter_value.currentIndexChanged.connect(self.load_calendar)
        filters.addWidget(self.calendar_filter_value, 1, 2, 1, 4)
        layout.addWidget(filter_panel)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(_button("Добавить занятие", self.add_lesson, "primary"))
        actions.addWidget(_button("Найти свободное время", self.find_free_time))
        self.delete_calendar_button = _button(
            "Удалить занятие", self._delete_calendar_selected, "danger"
        )
        self.delete_calendar_button.setEnabled(False)
        self.delete_calendar_button.setToolTip("Сначала выберите занятие в таблице")
        actions.addWidget(self.delete_calendar_button)
        actions.addStretch(1)
        actions.addWidget(_button("Предпросмотр", self.preview_calendar))
        actions.addWidget(_button("Сохранить PDF", self.save_calendar_pdf))
        actions.addWidget(_button("Печать", self.print_calendar))
        layout.addLayout(actions)
        self.calendar_table = _table(
            ["Время", "Предмет", "Учитель", "Кабинет", "Участников", "Статус"],
            stretch=(1, 2, 3),
            fixed={0: 125, 4: 105, 5: 130},
        )
        self.calendar_table.doubleClicked.connect(self._edit_calendar_selected)
        self.calendar_table.itemSelectionChanged.connect(self._calendar_selection_changed)
        self.calendar_summary = QLabel("Загрузка расписания…")
        self.calendar_summary.setObjectName("supportingText")
        layout.addWidget(self.calendar_summary)
        layout.addWidget(self.calendar_table)
        return page

    def _references_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.reference_tabs = QTabWidget()
        self.reference_tables: dict[str, QTableWidget] = {}
        self.reference_empty_labels: dict[str, QLabel] = {}
        for kind, title, headers in (
            (
                "subjects",
                "Предметы",
                ["Название", "Цвет", "Преподаватели", "Состояние"],
            ),
            ("rooms", "Кабинеты", ["Название", "Вместимость", "Состояние"]),
            (
                "groups",
                "Группы",
                ["Название", "Предмет", "Преподаватель", "Длительность", "Состояние"],
            ),
        ):
            tab = QWidget()
            tab_layout = QVBoxLayout(tab)
            buttons = QHBoxLayout()
            section_label = QLabel(title)
            section_label.setObjectName("sectionTitle")
            buttons.addWidget(section_label)
            buttons.addStretch(1)
            add_button = _button(
                "Добавить", lambda checked=False, key=kind: self.edit_reference(key), "primary"
            )
            buttons.addWidget(add_button)
            group_members_button: QPushButton | None = None
            if kind == "groups":
                group_members_button = _button("Состав группы", self.manage_selected_group)
                group_members_button.setEnabled(False)
                buttons.addWidget(group_members_button)
            tab_layout.addLayout(buttons)
            if kind == "subjects":
                table = _table(headers, stretch=(0, 2), fixed={1: 72, 3: 105})
            elif kind == "groups":
                table = _table(headers, stretch=(0, 1, 2), compact=(3, 4))
            else:
                table = _table(headers, stretch=(0,), compact=tuple(range(1, len(headers))))
            self.reference_tables[kind] = table
            table.itemSelectionChanged.connect(
                lambda widget=table, members=group_members_button: (
                    members.setEnabled(widget.currentRow() >= 0) if members else None
                )
            )
            table.itemActivated.connect(lambda _item, key=kind: self.edit_selected_reference(key))
            table.setToolTip("Дважды щёлкните строку или нажмите Enter для изменения")
            tab_layout.addWidget(table)
            empty_label = QLabel("Записей пока нет")
            empty_label.setObjectName("emptyState")
            empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_label.setMinimumHeight(220)
            self.reference_empty_labels[kind] = empty_label
            tab_layout.addWidget(empty_label)
            self.reference_tabs.addTab(tab, title)
        layout.addWidget(self.reference_tabs)
        return page

    def _journals_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        tabs = QTabWidget()
        student_page = QWidget()
        student_layout = QVBoxLayout(student_page)
        student_actions = QHBoxLayout()
        student_label = QLabel("Ученик")
        student_label.setObjectName("controlGroupLabel")
        student_actions.addWidget(student_label)
        self.journal_student = SearchableComboBox(placeholder="Фамилия или имя")
        self.journal_student.setMinimumWidth(340)
        student_label.setBuddy(self.journal_student)
        student_actions.addWidget(self.journal_student)
        student_actions.addWidget(_button("Показать", self.load_student_history))
        student_actions.addStretch(1)
        student_layout.addLayout(student_actions)
        self.student_summary = QLabel("Выберите ученика, чтобы открыть историю занятий")
        self.student_summary.setObjectName("supportingText")
        student_layout.addWidget(self.student_summary)
        self.student_journal = _table(
            ["Дата", "Предмет", "Учитель", "Время", "Посещение", "Опоздание"],
            stretch=(1, 2),
            compact=(0, 3, 4, 5),
        )
        student_layout.addWidget(self.student_journal)
        teacher_page = QWidget()
        teacher_layout = QVBoxLayout(teacher_page)
        teacher_actions = QHBoxLayout()
        teacher_label = QLabel("Преподаватель")
        teacher_label.setObjectName("controlGroupLabel")
        teacher_actions.addWidget(teacher_label)
        self.journal_teacher = SearchableComboBox(placeholder="Фамилия или имя")
        self.journal_teacher.setMinimumWidth(340)
        teacher_label.setBuddy(self.journal_teacher)
        teacher_actions.addWidget(self.journal_teacher)
        teacher_actions.addWidget(_button("Показать", self.load_teacher_history))
        teacher_actions.addStretch(1)
        teacher_layout.addLayout(teacher_actions)
        self.teacher_summary = QLabel("Выберите преподавателя, чтобы открыть историю занятий")
        self.teacher_summary.setObjectName("supportingText")
        teacher_layout.addWidget(self.teacher_summary)
        self.teacher_journal = _table(
            ["Дата", "Предмет", "Кабинет", "Фактическое время", "Минут", "Роль / статус"],
            stretch=(1, 2),
            compact=(0, 3, 4, 5),
        )
        self.student_journal.doubleClicked.connect(self._open_student_journal_lesson)
        self.teacher_journal.doubleClicked.connect(self._open_teacher_journal_lesson)
        teacher_layout.addWidget(self.teacher_journal)
        tabs.addTab(student_page, "Ученики")
        tabs.addTab(teacher_page, "Преподаватели")
        layout.addWidget(tabs)
        return page

    def _run(
        self,
        fn: Callable[..., Any],
        *args: Any,
        done: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        if self._closing:
            return
        worker = Worker(lambda: fn(*args))
        self._workers.add(worker)

        def finished(result: object) -> None:
            self._workers.discard(worker)
            if not self._closing:
                (done or (lambda _result: self.refresh()))(result)

        def failed(message: str) -> None:
            self._workers.discard(worker)
            if self._closing:
                return
            if on_error is not None:
                on_error(message)
            else:
                QMessageBox.critical(self, "Ошибка", message)

        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(failed)
        self.pool.start(worker)

    def shutdown(self) -> None:
        self._closing = True
        self.live_timer.stop()
        self.pool.clear()
        self.pool.waitForDone(16_000)
        self._workers.clear()

    def refresh(self) -> None:
        self._run(self.api.learning_reference_data, done=self._references_loaded)
        self.refresh_today()
        self.load_calendar()

    def refresh_today(self) -> None:
        if self.isVisible():
            self._run(self.api.learning_today, done=self._today_loaded)

    def _references_loaded(self, data: object) -> None:
        self.references = data if isinstance(data, dict) else {}
        current = self.presence_person.currentData()
        self.presence_person.clear()
        self.journal_student.clear()
        self.journal_teacher.clear()
        for person in self.references.get("students", []):
            self.presence_person.addItem(person.get("full_name", ""), person.get("id"))
            self.journal_student.addItem(person.get("full_name", ""), person.get("id"))
        for person in self.references.get("teachers", []):
            self.journal_teacher.addItem(person.get("full_name", ""), person.get("id"))
        index = self.presence_person.findData(current)
        if index >= 0:
            self.presence_person.setCurrentIndex(index)
        self._calendar_filter_changed()
        for kind, table in self.reference_tables.items():
            items = self.references.get(kind, [])
            table.setVisible(bool(items))
            self.reference_empty_labels[kind].setVisible(not items)
            table.setRowCount(len(items))
            for row, item in enumerate(items):
                values = [item.get("name", "")]
                if kind == "subjects":
                    has_teacher_assignments = "teacher_ids" in item
                    teacher_ids = set(item.get("teacher_ids") or [])
                    teacher_names = (
                        [
                            str(teacher.get("full_name", ""))
                            for teacher in self.references.get("teachers", [])
                            if teacher.get("id") in teacher_ids
                        ]
                        if has_teacher_assignments
                        else []
                    )
                    values.extend(
                        [
                            "",
                            (
                                ", ".join(teacher_names)
                                if teacher_names
                                else (
                                    "Не назначены"
                                    if has_teacher_assignments
                                    else "Требуется обновление сервера"
                                )
                            ),
                        ]
                    )
                elif kind == "rooms":
                    values.append(item.get("capacity", ""))
                elif kind == "groups":
                    subject = next(
                        (
                            value.get("name", "")
                            for value in self.references.get("subjects", [])
                            if value.get("id") == item.get("subject_id")
                        ),
                        "—",
                    )
                    teacher = next(
                        (
                            value.get("full_name", "")
                            for value in self.references.get("teachers", [])
                            if value.get("id") == item.get("default_teacher_id")
                        ),
                        "—",
                    )
                    values.extend(
                        [subject, teacher, f"{item.get('default_duration_minutes', 60)} мин"]
                    )
                values.append("Активен" if item.get("active") else "Отключён")
                for column, value in enumerate(values):
                    cell = QTableWidgetItem(str(value))
                    cell.setData(Qt.ItemDataRole.UserRole, item)
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    if kind == "subjects" and column == 1:
                        color = QColor(str(item.get("color", "#2563eb")))
                        if not color.isValid():
                            color = QColor("#2563eb")
                        swatch_holder = QWidget()
                        swatch_layout = QHBoxLayout(swatch_holder)
                        swatch_layout.setContentsMargins(10, 8, 10, 8)
                        swatch = QFrame()
                        swatch.setFixedSize(58, 22)
                        swatch.setStyleSheet(
                            f"background-color: {color.name()}; "
                            "border: 1px solid #aeb9c8; border-radius: 5px;"
                        )
                        swatch_layout.addWidget(swatch)
                        swatch_layout.addStretch(1)
                        swatch_holder.setToolTip(f"Цвет предмета в календаре: {color.name()}")
                        table.setCellWidget(row, column, swatch_holder)
                    if kind == "subjects" and column == 2:
                        cell.setTextAlignment(
                            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                        )
                        cell.setToolTip(str(value))
                    table.setItem(row, column, cell)

    def _today_loaded(self, data: object) -> None:
        self.today_data = data if isinstance(data, dict) else {}
        self.notifications_changed.emit(list(self.today_data.get("alerts", [])))
        lessons = [
            lesson
            for lesson in self.today_data.get("lessons", [])
            if lesson.get("status") != "cancelled"
        ]
        self.today_lessons.setRowCount(len(lessons))
        for row, lesson in enumerate(lessons):
            start = parse_center(lesson["start_at"])
            end = parse_center(lesson["end_at"])
            values = [
                f"{start:%H:%M}–{end:%H:%M}",
                lesson.get("subject_name_snapshot", ""),
                lesson.get("teacher_name_snapshot", ""),
                lesson.get("room_name_snapshot", ""),
                _participant_summary(lesson),
                STATUS_LABELS.get(lesson.get("status"), lesson.get("status", "")),
            ]
            for column, value in enumerate(values):
                self.today_lessons.setItem(row, column, QTableWidgetItem(str(value)))
            actions = QWidget()
            bar = QHBoxLayout(actions)
            bar.setContentsMargins(2, 2, 2, 2)
            if lesson.get("status") in {"planned", "scheduled"}:
                start_button = _button(
                    "Начать",
                    lambda checked=False, item=lesson: self.lesson_action(item, "start"),
                    "primary",
                    compact=True,
                )
                edit_button = _button(
                    "Изменить",
                    lambda checked=False, item=lesson: self.edit_lesson(item),
                    compact=True,
                )
                bar.addWidget(start_button, 1)
                bar.addWidget(edit_button, 1)
            elif lesson.get("status") == "in_progress":
                bar.addWidget(
                    _button(
                        "Завершить",
                        lambda checked=False, item=lesson: self.lesson_action(item, "finish"),
                        "primary",
                        compact=True,
                    ),
                    1,
                )
            bar.addWidget(
                _button(
                    "Карточка",
                    lambda checked=False, item=lesson: self.open_lesson(item),
                    compact=True,
                ),
                1,
            )
            self.today_lessons.setCellWidget(row, 6, actions)
        present = self.today_data.get("present", [])
        self.present_table.setRowCount(len(present))
        for row, person in enumerate(present):
            arrived = parse_center(person["arrived_at"])
            name_item = QTableWidgetItem(person.get("person_name", ""))
            name_item.setData(Qt.ItemDataRole.UserRole, person.get("person_id"))
            self.present_table.setItem(row, 0, name_item)
            self.present_table.setItem(row, 1, QTableWidgetItem(f"{arrived:%H:%M}"))
        alerts = self.today_data.get("alerts", [])
        self.alert_label.setVisible(bool(alerts))
        self.dismiss_alerts_button.setVisible(bool(alerts))
        if alerts:
            self.alert_label.setText(
                " · ".join(str(item.get("message", "")) for item in alerts[:3])
            )

    def dismiss_alerts(self) -> None:
        alerts = list(self.today_data.get("alerts", []))

        def mark_all() -> None:
            for item in alerts:
                self.api.read_admin_notification(int(item["id"]))

        self._run(mark_all, done=lambda _result: self.refresh_today())

    def load_calendar(self, *_args: object) -> None:
        selected = self.calendar_date.date().toPython()
        local = center_timezone()
        start = datetime.combine(selected, datetime.min.time(), tzinfo=local)
        days = int(self.calendar_period.currentData() or 1)
        if int(self.calendar_period.currentData() or 0) == 0:
            selected_end = max(selected, self.calendar_end.date().toPython())
            end = datetime.combine(
                selected_end + timedelta(days=1), datetime.min.time(), tzinfo=local
            )
        else:
            end = start + timedelta(days=days)
        filter_name = self.calendar_filter_type.currentData()
        filter_value = self.calendar_filter_value.currentData() if filter_name else None
        self._run(
            lambda: self.api.learning_lessons(
                start.isoformat(),
                end.isoformat(),
                **({str(filter_name): int(filter_value)} if filter_value is not None else {}),
            ),
            done=self._calendar_loaded,
        )

    def _calendar_period_changed(self, *_args: object) -> None:
        custom = int(self.calendar_period.currentData() or 0) == 0
        self.calendar_end.setEnabled(custom)
        if not custom:
            days = int(self.calendar_period.currentData() or 1)
            self.calendar_end.setDate(self.calendar_date.date().addDays(days - 1))
        self.load_calendar()

    def _calendar_filter_changed(self, *_args: object) -> None:
        filter_name = self.calendar_filter_type.currentData()
        self.calendar_filter_value.blockSignals(True)
        self.calendar_filter_value.clear()
        source = {
            "teacher_id": ("teachers", "full_name"),
            "student_id": ("students", "full_name"),
            "group_id": ("groups", "name"),
            "room_id": ("rooms", "name"),
        }.get(filter_name)
        if source:
            for item in self.references.get(source[0], []):
                self.calendar_filter_value.addItem(str(item.get(source[1], "")), item.get("id"))
        self.calendar_filter_value.setVisible(source is not None)
        self.calendar_filter_value.blockSignals(False)
        self.load_calendar()

    def _calendar_loaded(self, data: object) -> None:
        self.calendar_lessons = (
            [item for item in data if item.get("status") != "cancelled"]
            if isinstance(data, list)
            else []
        )
        count = len(self.calendar_lessons)
        self.calendar_summary.setText(
            f"Найдено занятий: {count}" if count else "В выбранном периоде занятий нет"
        )
        self.calendar_table.setRowCount(len(self.calendar_lessons))
        show_date = self.calendar_end.date() > self.calendar_date.date()
        for row, lesson in enumerate(self.calendar_lessons):
            start = parse_center(lesson["start_at"])
            values = [
                f"{start:%d.%m %H:%M}" if show_date else f"{start:%H:%M}",
                lesson.get("subject_name_snapshot", ""),
                lesson.get("teacher_name_snapshot", ""),
                lesson.get("room_name_snapshot", ""),
                _participant_details(lesson),
                STATUS_LABELS.get(lesson.get("status"), lesson.get("status", "")),
            ]
            for column, value in enumerate(values):
                self.calendar_table.setItem(row, column, QTableWidgetItem(str(value)))
        self._calendar_selection_changed()

    def _calendar_selection_changed(self) -> None:
        row = self.calendar_table.currentRow()
        lesson = self.calendar_lessons[row] if 0 <= row < len(self.calendar_lessons) else None
        planned = bool(lesson and lesson.get("status") in {"planned", "scheduled"})
        self.delete_calendar_button.setEnabled(planned)
        self.delete_calendar_button.setToolTip(
            "Удалить выбранное занятие из расписания"
            if planned
            else "Выберите запланированное занятие в таблице"
        )

    def _delete_calendar_selected(self) -> None:
        row = self.calendar_table.currentRow()
        if not 0 <= row < len(self.calendar_lessons):
            return
        self._delete_planned_lesson(self.calendar_lessons[row])

    def _delete_planned_lesson(self, lesson: dict[str, Any]) -> None:
        if lesson.get("status") not in {"planned", "scheduled"}:
            return
        if not self._confirm_lesson_deletion(lesson):
            return
        self._run(
            self.api.lesson_action,
            int(lesson["id"]),
            "cancel",
            {"reason": "Удалено администратором"},
            done=self._action_done,
        )

    def _confirm_lesson_deletion(self, lesson: dict[str, Any]) -> bool:
        confirmation = QMessageBox(self)
        confirmation.setIcon(QMessageBox.Icon.Warning)
        confirmation.setWindowTitle("Удаление занятия")
        confirmation.setText(
            f"Удалить занятие «{lesson.get('subject_name_snapshot', '')}» "
            "из расписания?"
        )
        confirmation.setInformativeText(
            "Занятие будет отменено. Если оно входит в серию, остальные занятия не изменятся."
        )
        delete_button = confirmation.addButton(
            "Удалить занятие", QMessageBox.ButtonRole.DestructiveRole
        )
        confirmation.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        confirmation.exec()
        return confirmation.clickedButton() == delete_button

    def _edit_calendar_selected(self, _index: object = None) -> None:
        row = self.calendar_table.currentRow()
        if 0 <= row < len(self.calendar_lessons):
            self.edit_lesson(self.calendar_lessons[row])

    def add_lesson(self) -> None:
        dialog = LessonDialog(self.references, parent=self)
        self._submit_new_lesson(dialog)

    def _submit_new_lesson(self, dialog: LessonDialog) -> None:
        if not dialog.exec():
            return
        if dialog.repeat.isChecked():
            function = self.api.create_lesson_series
            payload = dialog.series_payload()
        else:
            function = self.api.create_lesson
            payload = dialog.payload()
        self._run(
            function,
            payload,
            on_error=lambda message: self._restore_lesson_dialog(
                dialog, message, lambda: self._submit_new_lesson(dialog)
            ),
        )

    @staticmethod
    def _restore_lesson_dialog(
        dialog: LessonDialog, message: str, retry: Callable[[], None]
    ) -> None:
        dialog.show_error(message)
        QTimer.singleShot(0, retry)

    def find_free_time(self) -> None:
        dialog = FreeSlotDialog(self.references, self)

        def search(criteria: dict[str, Any]) -> None:
            self._run(
                lambda: self.api.free_slots(**criteria),
                done=lambda result: dialog.set_slots(result if isinstance(result, list) else []),
                on_error=dialog.search_failed,
            )

        dialog.search_requested.connect(search)
        if not dialog.exec():
            return
        slot = dialog.selected_slot()
        if slot is None:
            QMessageBox.information(self, "Свободное время", "Сначала выберите вариант.")
            return
        criteria = dialog.criteria()
        lesson = LessonDialog(self.references, parent=self)
        LessonDialog._select(lesson.teacher, criteria.get("teacher_id"))
        LessonDialog._select(lesson.room, slot.get("room_id"))
        lesson.set_period(slot["start_at"], slot["end_at"])
        selected_students = set(criteria.get("student_ids", []))
        for row in range(lesson.students.rowCount()):
            check = lesson.students.cellWidget(row, 0).findChild(QCheckBox)
            if check:
                check.setChecked(int(check.property("person_id")) in selected_students)
        self._submit_new_lesson(lesson)

    def edit_lesson(self, lesson: dict[str, Any]) -> None:
        if lesson.get("status") not in {"planned", "scheduled"}:
            QMessageBox.information(
                self,
                "Занятие",
                "План можно менять только до начала занятия. "
                "Для идущего занятия используйте действия в его карточке.",
            )
            return
        dialog = LessonDialog(self.references, lesson, self)
        self._submit_existing_lesson(dialog, lesson)

    def _submit_existing_lesson(self, dialog: LessonDialog, lesson: dict[str, Any]) -> None:
        if not dialog.exec():
            return

        def retry() -> None:
            self._submit_existing_lesson(dialog, lesson)

        if not lesson.get("series_id"):
            self._run(
                self.api.update_lesson,
                int(lesson["id"]),
                dialog.payload(),
                on_error=lambda message: self._restore_lesson_dialog(dialog, message, retry),
            )
            return
        choice = QMessageBox(self)
        choice.setWindowTitle("Изменение серии")
        choice.setText("Какие занятия изменить?")
        only_button = choice.addButton("Только это", QMessageBox.ButtonRole.AcceptRole)
        future_button = choice.addButton("Это и будущие", QMessageBox.ButtonRole.ActionRole)
        all_button = choice.addButton("Всю серию", QMessageBox.ButtonRole.ActionRole)
        choice.addButton("Вернуться к занятию", QMessageBox.ButtonRole.RejectRole)
        choice.exec()
        clicked = choice.clickedButton()
        if clicked == only_button:
            self._run(
                self.api.update_lesson,
                int(lesson["id"]),
                dialog.payload(),
                on_error=lambda message: self._restore_lesson_dialog(dialog, message, retry),
            )
        elif clicked in {future_button, all_button}:
            payload = dialog.series_payload()
            payload["scope"] = "future" if clicked == future_button else "all"
            payload["anchor_lesson_id"] = int(lesson["id"])
            self._run(
                self.api.update_lesson_series,
                int(lesson["series_id"]),
                payload,
                on_error=lambda message: self._restore_lesson_dialog(dialog, message, retry),
            )
        else:
            QTimer.singleShot(0, retry)

    def open_lesson(self, lesson: dict[str, Any]) -> None:
        dialog = LessonCardDialog(
            lesson,
            self,
            open_person=lambda person_id: self.person_requested.emit(person_id),
        )
        if dialog.exec():
            if dialog.operation:
                self._handle_lesson_operation(lesson, dialog)
                return
            changes = dialog.attendance()
            actual_time_change = dialog.actual_time_change()
            original = {
                int(item["person_id"]): str(item.get("attendance_status", "expected"))
                for item in lesson.get("participants", [])
            }
            cancellations: dict[int, dict[str, Any]] = {}
            for person_id, attendance_status in changes:
                if (
                    lesson.get("status") != "completed"
                    and attendance_status == "excused"
                    and original.get(person_id) != "excused"
                ):
                    cancellation = self._cancellation_details(person_id)
                    if cancellation is None:
                        return
                    cancellations[person_id] = cancellation

            def save_all() -> None:
                for person_id, attendance_status in changes:
                    before = original.get(person_id)
                    if attendance_status == before:
                        continue
                    if lesson.get("status") == "completed":
                        reason = correction_reasons[person_id]
                        participant = next(
                            item
                            for item in lesson.get("participants", [])
                            if int(item["person_id"]) == person_id
                        )
                        self.api.correct_attendance(
                            int(lesson["id"]),
                            person_id,
                            {
                                "attendance_status": attendance_status,
                                "arrived_at": participant.get("arrived_at"),
                                "left_at": participant.get("left_at"),
                                "reason": reason,
                            },
                        )
                        continue
                    if attendance_status == "excused":
                        self.api.cancel_lesson_participant(
                            int(lesson["id"]),
                            person_id,
                            cancellations[person_id],
                        )
                    elif before == "excused":
                        self.api.restore_lesson_participant(int(lesson["id"]), person_id)
                        if attendance_status != "expected":
                            self.api.set_attendance(int(lesson["id"]), person_id, attendance_status)
                    else:
                        self.api.set_attendance(int(lesson["id"]), person_id, attendance_status)

            correction_reasons: dict[int, str] = {}
            if lesson.get("status") == "completed":
                for person_id, attendance_status in changes:
                    if attendance_status == original.get(person_id):
                        continue
                    reason, accepted = QInputDialog.getText(
                        self,
                        "Корректировка завершённого занятия",
                        "Причина изменения посещаемости:",
                    )
                    if not accepted or len(reason.strip()) < 3:
                        return
                    correction_reasons[person_id] = reason.strip()
            actual_time_reason: str | None = None
            if actual_time_change is not None:
                actual_time_reason, accepted = QInputDialog.getText(
                    self,
                    "Корректировка фактического времени",
                    "Причина изменения:",
                )
                if not accepted or len(actual_time_reason.strip()) < 3:
                    return
                actual_time_reason = actual_time_reason.strip()

            original_save_all = save_all

            def save_all_with_time() -> None:
                original_save_all()
                if actual_time_change is not None and actual_time_reason is not None:
                    self.api.correct_actual_time(
                        int(lesson["id"]),
                        {
                            "actual_start_at": actual_time_change[0],
                            "actual_end_at": actual_time_change[1],
                            "reason": actual_time_reason,
                        },
                    )

            self._run(save_all_with_time)

    def _handle_lesson_operation(self, lesson: dict[str, Any], dialog: LessonCardDialog) -> None:
        lesson_id = int(lesson["id"])
        if dialog.operation == "leave_early":
            person_id = dialog.selected_person_id()
            reason_dialog = ReasonDialog(
                "Ученик покинул занятие",
                "Укажите причину досрочного ухода:",
                self,
            )
            if person_id is not None and reason_dialog.exec():
                self._run(
                    self.api.leave_lesson_early,
                    lesson_id,
                    person_id,
                    reason_dialog.reason(),
                    done=self._action_done,
                )
            return
        if dialog.operation == "cancel_participant":
            person_id = dialog.selected_person_id()
            if person_id is not None:
                self._cancel_participant(lesson, person_id)
            return
        if dialog.operation == "finish_early":
            self._finish_lesson_early(lesson_id)
            return
        if dialog.operation == "teacher_exit":
            self._teacher_exit(lesson)

    def _cancel_participant(self, lesson: dict[str, Any], person_id: int) -> None:
        payload = self._cancellation_details(person_id)
        if payload is None:
            return
        self._run(
            self.api.cancel_lesson_participant,
            int(lesson["id"]),
            person_id,
            payload,
            done=self._action_done,
        )

    def _cancellation_details(self, person_id: int) -> dict[str, Any] | None:
        student = next(
            (
                item
                for item in self.references.get("students", [])
                if int(item.get("id", -1)) == person_id
            ),
            {},
        )
        choices: list[tuple[str, str, int | None]] = [
            ("Ученик", "student", person_id),
            ("Администратор", "administrator", None),
        ]
        choices[1:1] = [
            (f"Родитель: {guardian.get('full_name', '')}", "guardian", int(guardian["id"]))
            for guardian in student.get("guardians", [])
        ]
        label, accepted = QInputDialog.getItem(
            self,
            "Отмена участия",
            "Кто сообщил об отмене?",
            [item[0] for item in choices],
            editable=False,
        )
        if not accepted:
            return
        reason, accepted = QInputDialog.getText(self, "Отмена участия", "Причина:")
        if not accepted or not reason.strip():
            return None
        _title, actor, actor_person_id = next(item for item in choices if item[0] == label)
        return {
            "cancelled_by": actor,
            "cancelled_by_person_id": actor_person_id,
            "reason": reason.strip(),
        }

    def _finish_lesson_early(self, lesson_id: int) -> None:
        reason, accepted = QInputDialog.getText(self, "Досрочное завершение", "Внутренняя причина:")
        if not accepted or not reason.strip():
            return
        public_comment, accepted = QInputDialog.getText(
            self,
            "Досрочное завершение",
            "Комментарий для родителей (необязательно):",
        )
        if not accepted:
            return
        self._run(
            self.api.finish_lesson_early,
            lesson_id,
            reason.strip(),
            public_comment.strip(),
            done=self._action_done,
        )

    def _teacher_exit(self, lesson: dict[str, Any]) -> None:
        choice = QMessageBox(self)
        choice.setWindowTitle("Преподаватель не может продолжить")
        choice.setText("Выберите, как продолжить занятие.")
        replace_button = choice.addButton("Назначить замену", QMessageBox.ButtonRole.AcceptRole)
        finish_button = choice.addButton(
            "Завершить досрочно", QMessageBox.ButtonRole.DestructiveRole
        )
        choice.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        choice.exec()
        if choice.clickedButton() == finish_button:
            self._finish_lesson_early(int(lesson["id"]))
            return
        if choice.clickedButton() != replace_button:
            return
        subject = next(
            (
                item
                for item in self.references.get("subjects", [])
                if int(item.get("id", -1)) == int(lesson.get("subject_id", -1))
            ),
            {},
        )
        qualified = set(subject.get("teacher_ids", []))
        participant_ids = {int(item["person_id"]) for item in lesson.get("participants", [])}
        teachers = [
            item
            for item in self.references.get("teachers", [])
            if int(item.get("id", -1)) in qualified
            and int(item.get("id", -1)) not in participant_ids
        ]
        name, accepted = QInputDialog.getItem(
            self,
            "Замена преподавателя",
            "Новый преподаватель:",
            [str(item.get("full_name", "")) for item in teachers],
            editable=False,
        )
        if not accepted or not name:
            return
        reason, accepted = QInputDialog.getText(self, "Замена преподавателя", "Причина замены:")
        if not accepted or not reason.strip():
            return
        teacher = next(item for item in teachers if item.get("full_name") == name)
        expected_end = parse_center(lesson["end_at"])
        if now_center() >= expected_end:
            suggested = now_center() + timedelta(minutes=30)
            value, accepted = QInputDialog.getText(
                self,
                "Замена преподавателя",
                "До какого времени продлится занятие (ДД.ММ.ГГГГ ЧЧ:ММ):",
                text=suggested.strftime("%d.%m.%Y %H:%M"),
            )
            if not accepted:
                return
            try:
                expected_end = datetime.strptime(value.strip(), "%d.%m.%Y %H:%M").replace(
                    tzinfo=center_timezone()
                )
            except ValueError:
                QMessageBox.warning(
                    self,
                    "Некорректное время",
                    "Укажите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ.",
                )
                return
        self._run(
            self.api.transition_lesson_teacher,
            int(lesson["id"]),
            {
                "action": "substitute",
                "replacement_teacher_id": int(teacher["id"]),
                "reason": reason.strip(),
                "expected_end_at": expected_end.isoformat(),
            },
            done=self._action_done,
        )

    def lesson_action(self, lesson: dict[str, Any], action: str) -> None:
        self._run(self.api.lesson_action, int(lesson["id"]), action, done=self._action_done)

    def _action_done(self, result: object) -> None:
        if isinstance(result, dict) and result.get("warnings"):
            QMessageBox.warning(
                self, "Предупреждение", "\n".join(x.get("message", "") for x in result["warnings"])
            )
        self.refresh()

    def presence(self, action: str) -> None:
        person_id = self.presence_person.currentData()
        if person_id is not None:
            self._run(self.api.presence_action, int(person_id), action, done=self._action_done)

    def _select_present_person(self, row: int) -> None:
        item = self.present_table.item(row, 0)
        if item is None:
            return
        person_id = item.data(Qt.ItemDataRole.UserRole)
        index = self.presence_person.findData(person_id)
        if index < 0:
            return
        self.presence_person.setCurrentIndex(index)
        self.presence_person.setFocus()
        self.departure_button.setFocus()

    def edit_reference(self, kind: str, item: dict[str, Any] | None = None) -> None:
        dialog = ReferenceDialog(kind, item, self, references=self.references)
        if dialog.exec():
            singular = {"subjects": "subjects", "rooms": "rooms", "groups": "groups"}[kind]
            if item:
                self._run(
                    self.api.update_learning_item, singular, int(item["id"]), dialog.payload()
                )
            else:
                self._run(self.api.create_learning_item, singular, dialog.payload())

    def edit_selected_reference(self, kind: str) -> None:
        table = self.reference_tables[kind]
        row = table.currentRow()
        if row >= 0 and table.item(row, 0):
            self.edit_reference(kind, table.item(row, 0).data(Qt.ItemDataRole.UserRole))

    def manage_selected_group(self) -> None:
        table = self.reference_tables["groups"]
        row = table.currentRow()
        if row < 0 or table.item(row, 0) is None:
            return
        group = table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        self._run(
            self.api.group_memberships,
            int(group["id"]),
            done=lambda result: self._show_group_members(group, result),
        )

    def _show_group_members(self, group: dict[str, Any], result: object) -> None:
        memberships = result if isinstance(result, list) else []
        dialog = GroupMembersDialog(
            group,
            self.references.get("students", []),
            memberships,
            self,
        )
        if not dialog.exec():
            return
        additions, endings = dialog.changes()

        def save() -> None:
            for person_id in additions:
                self.api.add_group_membership(int(group["id"]), person_id)
            for membership_id in endings:
                self.api.end_group_membership(int(group["id"]), membership_id)

        self._run(save, done=lambda _result: self.refresh())

    def print_calendar(self) -> None:
        document = self._schedule_document()
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        self._prepare_schedule_printer(printer)
        dialog = QPrintDialog(printer, self)
        if dialog.exec():
            document.print_(printer)

    def preview_calendar(self) -> None:
        document = self._schedule_document()
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        self._prepare_schedule_printer(printer)
        preview = SchedulePreviewDialog(printer, document, self.print_calendar, self)
        preview.exec()

    def save_calendar_pdf(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Сохранить расписание",
            "Расписание КРиТ.pdf",
            "PDF (*.pdf)",
        )
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        self._prepare_schedule_printer(printer)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(path)
        self._schedule_document().print_(printer)

    def _schedule_document(self) -> QTextDocument:
        selected = self.calendar_date.date().toPython()
        selected_end = max(selected, self.calendar_end.date().toPython())
        lessons_by_day: dict[date, list[dict[str, Any]]] = {}
        for lesson in self.calendar_lessons:
            if lesson.get("status") == "cancelled":
                continue
            start = parse_center(lesson["start_at"])
            if not selected <= start.date() <= selected_end:
                continue
            lessons_by_day.setdefault(start.date(), []).append(lesson)
        for lessons in lessons_by_day.values():
            lessons.sort(key=lambda item: str(item.get("start_at", "")))

        weekday_names = (
            "Понедельник",
            "Вторник",
            "Среда",
            "Четверг",
            "Пятница",
            "Суббота",
            "Воскресенье",
        )
        columns: list[tuple[date, int, int, list[dict[str, Any]], int]] = []
        for day, lessons in sorted(lessons_by_day.items()):
            chunks = self._split_schedule_day(lessons)
            for part, chunk in enumerate(chunks, start=1):
                columns.append(
                    (day, part, len(chunks), chunk, sum(map(self._schedule_lesson_cost, chunk)))
                )
        pages = self._schedule_pages(columns)

        document = QTextDocument(self)
        period_label = selected.strftime("%d.%m.%Y")
        if selected_end != selected:
            period_label += f"–{selected_end:%d.%m.%Y}"
        filter_label = self.calendar_filter_type.currentText()
        if self.calendar_filter_value.isVisible():
            filter_label += f": {self.calendar_filter_value.currentText()}"
        if not pages:
            document.setHtml(
                "<style>body { font-family: 'Segoe UI'; color: #172033; }</style>"
                f"<h1>КРиТ · расписание {period_label}</h1>"
                f"<p>{escape(filter_label)}</p>"
                "<p>В выбранном периоде занятий нет.</p>"
            )
            return document

        page_sections: list[str] = []
        for page_number, page in enumerate(pages, start=1):
            width = max(1, 100 // len(page))
            headers: list[str] = []
            cells: list[str] = []
            for day, part, total_parts, lessons, _cost in page:
                continuation = (
                    f"<br><span class='continuation'>часть {part} из {total_parts}</span>"
                    if total_parts > 1
                    else ""
                )
                headers.append(
                    f"<th width='{width}%'>"
                    f"{weekday_names[day.weekday()]}<br>"
                    f"<span class='date'>{day:%d.%m.%Y}</span>{continuation}</th>"
                )
                lesson_blocks = "".join(self._schedule_lesson_html(item) for item in lessons)
                cells.append(f"<td width='{width}%' valign='top'>{lesson_blocks}</td>")
            page_break = " style='page-break-after: always'" if page_number < len(pages) else ""
            page_sections.append(
                f"<h1>КРиТ · расписание {period_label}</h1>"
                f"<p class='subtitle'>{escape(filter_label)} · страница "
                f"{page_number} из {len(pages)}</p>"
                f"<table class='calendar' width='100%' cellspacing='0' cellpadding='3'"
                f"{page_break}><thead><tr>{''.join(headers)}</tr></thead>"
                f"<tbody><tr>{''.join(cells)}</tr></tbody></table>"
            )
        document.setHtml(
            "<style>"
            "body { font-family: 'Segoe UI'; color: #172033; font-size: 7.5pt; }"
            "h1 { font-size: 14pt; margin: 0 0 2px 0; }"
            ".subtitle { color: #526174; margin: 0 0 6px 0; }"
            "table.calendar { border-collapse: collapse; }"
            "table.calendar th { background: #e8eef8; padding: 4px 3px; "
            "border: 1px solid #b8c4d4; font-size: 8.5pt; }"
            "table.calendar td { padding: 3px; border: 1px solid #b8c4d4; }"
            ".date { font-weight: 700; }"
            ".continuation { color: #526174; font-size: 7pt; font-weight: 400; }"
            ".lesson { background: #eef4ff; border: 1px solid #c5d4ec; "
            "margin: 0 0 3px 0; padding: 3px; line-height: 105%; }"
            ".lesson-title { font-weight: 700; }"
            ".time { color: #124da8; }"
            ".teacher { color: #526174; }"
            ".students { color: #172033; }"
            "</style>"
            f"{''.join(page_sections)}"
        )
        return document

    def _split_schedule_day(
        self, lessons: list[dict[str, Any]], *, column_capacity: int = 25
    ) -> list[list[dict[str, Any]]]:
        chunks: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_cost = 0
        for lesson in lessons:
            cost = self._schedule_lesson_cost(lesson)
            if current and current_cost + cost > column_capacity:
                chunks.append(current)
                current = []
                current_cost = 0
            current.append(lesson)
            current_cost += cost
        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def _schedule_pages(
        columns: list[tuple[date, int, int, list[dict[str, Any]], int]],
    ) -> list[list[tuple[date, int, int, list[dict[str, Any]], int]]]:
        if not columns:
            return []
        if len(columns) <= 7 and all(column[4] <= 18 for column in columns):
            return [columns]
        return [columns[index : index + 4] for index in range(0, len(columns), 4)]

    def _schedule_lesson_cost(self, lesson: dict[str, Any]) -> int:
        participants = self._schedule_participant_names(lesson)
        subject = str(lesson.get("subject_name_snapshot", ""))
        teacher = self._short_person_name(lesson.get("teacher_name_snapshot", ""))
        students = ", ".join(participants)
        return 2 + ceil(len(subject) / 24) + ceil(len(teacher) / 28) + ceil(len(students) / 30)

    @staticmethod
    def _short_person_name(value: object) -> str:
        parts = str(value or "").split()
        if len(parts) < 2:
            return " ".join(parts)
        initials = " ".join(f"{part[0]}." for part in parts[1:] if part)
        return f"{parts[0]} {initials}".strip()

    def _schedule_lesson_html(self, lesson: dict[str, Any]) -> str:
        start = parse_center(lesson["start_at"])
        end = parse_center(lesson["end_at"])
        participants = self._schedule_participant_names(lesson)
        students = ", ".join(map(escape, participants)) or "не указаны"
        subject = escape(str(lesson.get("subject_name_snapshot", "")))
        teacher = escape(self._short_person_name(lesson.get("teacher_name_snapshot", "")))
        return (
            "<div class='lesson'>"
            f"<div class='lesson-title'><span class='time'>{start:%H:%M}–{end:%H:%M}</span> · "
            f"{subject}</div>"
            f"<div class='teacher'>{teacher}</div>"
            f"<div class='students'><b>Ученики ({len(participants)}):</b> {students}</div>"
            "</div>"
        )

    def _schedule_participant_names(self, lesson: dict[str, Any]) -> list[str]:
        participants = [
            name
            for item in lesson.get("participants", [])
            if item.get("attendance_status") != "excused"
            if (
                name := self._short_person_name(
                    item.get("person_name_snapshot") or item.get("person_name")
                )
            )
        ]
        return participants

    def _prepare_schedule_printer(self, printer: QPrinter) -> None:
        printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        printer.setPageOrientation(QPageLayout.Orientation.Landscape)
        printer.setPageMargins(QMarginsF(8, 8, 8, 8), QPageLayout.Unit.Millimeter)

    def load_student_history(self) -> None:
        person_id = self.journal_student.currentData()
        if person_id is not None:
            self._run(
                self.api.student_history,
                int(person_id),
                done=self._student_history_loaded,
            )

    def _student_history_loaded(self, result: object) -> None:
        data = result if isinstance(result, dict) else {}
        lessons = data.get("lessons", [])
        self.student_summary.setText(
            f"Занятий: {len(lessons)} · дважды щёлкните строку для подробностей"
            if lessons
            else "В истории этого ученика пока нет занятий"
        )
        labels = {
            "expected": "Ожидается",
            "present": "Присутствовал",
            "late": "Опоздал",
            "absent": "Не пришёл",
            "left_early": "Ушёл раньше",
            "excused": "Отменено",
        }
        self.student_journal.setRowCount(len(lessons))
        for row, lesson in enumerate(lessons):
            start = parse_center(lesson["start_at"])
            end = parse_center(lesson["end_at"])
            values = [
                f"{start:%d.%m.%Y}",
                lesson.get("subject_name_snapshot", ""),
                lesson.get("teacher_name_snapshot", ""),
                f"{start:%H:%M}–{end:%H:%M}",
                labels.get(lesson.get("attendance_status"), ""),
                lesson.get("late_minutes") or "—",
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setData(Qt.ItemDataRole.UserRole, lesson)
                self.student_journal.setItem(row, column, cell)

    def _open_student_journal_lesson(self, _index: object = None) -> None:
        row = self.student_journal.currentRow()
        if row >= 0 and self.student_journal.item(row, 0) is not None:
            lesson = self.student_journal.item(row, 0).data(Qt.ItemDataRole.UserRole)
            if isinstance(lesson, dict):
                self.open_lesson(lesson)

    def load_teacher_history(self) -> None:
        person_id = self.journal_teacher.currentData()
        if person_id is not None:
            self._run(
                self.api.teacher_history,
                int(person_id),
                done=self._teacher_history_loaded,
            )

    def _teacher_history_loaded(self, result: object) -> None:
        data = result if isinstance(result, dict) else {}
        lessons = data.get("lessons", [])
        summary = data.get("summary", {})
        self.teacher_summary.setText(
            f"Всего: {summary.get('total', 0)} · "
            f"Завершено: {summary.get('completed', 0)} · "
            f"Отменено: {summary.get('cancelled', 0)} · "
            "дважды щёлкните строку для подробностей"
        )
        self.teacher_journal.setRowCount(len(lessons))
        for row, lesson in enumerate(lessons):
            planned_start = parse_center(lesson["start_at"])
            actual_start_value = lesson.get("actual_start_at")
            actual_end_value = lesson.get("actual_end_at")
            start = (
                parse_center(actual_start_value)
                if actual_start_value
                else planned_start
            )
            end = (
                parse_center(actual_end_value)
                if actual_end_value
                else parse_center(lesson["end_at"])
            )
            segment_type = lesson.get("teacher_segment_type")
            role_status = STATUS_LABELS.get(lesson.get("status"), lesson.get("status", ""))
            if segment_type == "substitute":
                role_status = "Замещающий преподаватель"
            elif segment_type == "primary" and actual_end_value:
                role_status = f"Основной · замена/завершение в {end:%H:%M}"
            values = [
                f"{planned_start:%d.%m.%Y}",
                lesson.get("subject_name_snapshot", ""),
                lesson.get("room_name_snapshot", ""),
                f"{start:%H:%M}–{end:%H:%M}",
                lesson.get("teacher_segment_minutes", "—"),
                role_status,
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setData(Qt.ItemDataRole.UserRole, lesson)
                self.teacher_journal.setItem(row, column, cell)

    def _open_teacher_journal_lesson(self, _index: object = None) -> None:
        row = self.teacher_journal.currentRow()
        if row >= 0 and self.teacher_journal.item(row, 0) is not None:
            lesson = self.teacher_journal.item(row, 0).data(Qt.ItemDataRole.UserRole)
            if isinstance(lesson, dict):
                self.open_lesson(lesson)
