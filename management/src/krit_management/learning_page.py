from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from html import escape
from typing import Any

from PySide6.QtCore import QDate, QDateTime, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QPageLayout, QTextDocument
from PySide6.QtPrintSupport import QPrintDialog, QPrinter, QPrintPreviewDialog
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
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
from .widgets import SearchableComboBox, configure_calendar
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


def _button(text: str, callback: Callable[[], None], kind: str = "secondary") -> QPushButton:
    result = QPushButton(text)
    result.setProperty("kind", kind)
    result.clicked.connect(callback)
    return result


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(48)
    table.horizontalHeader().setStretchLastSection(True)
    return table


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
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(str(self.item.get("name", "")))
        self.capacity = QSpinBox()
        self.capacity.setRange(1, 1000)
        self.capacity.setValue(int(self.item.get("capacity", 12)))
        self.color = QLineEdit(str(self.item.get("color", "#2563eb")))
        self.active = QCheckBox("Используется")
        self.active.setChecked(bool(self.item.get("active", True)))
        self.group_subject = SearchableComboBox(placeholder="Предмет")
        self.group_subject.addItem("Не выбран", None)
        for subject in self.references.get("subjects", []):
            self.group_subject.addItem(subject.get("name", ""), subject.get("id"))
        self.group_teacher = SearchableComboBox(placeholder="Фамилия или имя")
        self.group_teacher.addItem("Не выбран", None)
        for teacher in self.references.get("teachers", []):
            self.group_teacher.addItem(teacher.get("full_name", ""), teacher.get("id"))
        self.group_duration = QSpinBox()
        self.group_duration.setRange(5, 1440)
        self.group_duration.setSuffix(" мин")
        self.group_duration.setValue(int(self.item.get("default_duration_minutes", 60)))
        form.addRow("Название", self.name)
        if kind == "rooms":
            form.addRow("Вместимость", self.capacity)
        if kind == "subjects":
            form.addRow("Цвет", self.color)
        if kind == "groups":
            form.addRow("Предмет", self.group_subject)
            form.addRow("Преподаватель", self.group_teacher)
            form.addRow("Продолжительность", self.group_duration)
            self.group_subject.setCurrentIndex(
                max(0, self.group_subject.findData(self.item.get("subject_id")))
            )
            self.group_teacher.setCurrentIndex(
                max(0, self.group_teacher.findData(self.item.get("default_teacher_id")))
            )
        form.addRow("", self.active)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": " ".join(self.name.text().split()),
            "active": self.active.isChecked(),
        }
        if self.kind == "rooms":
            result["capacity"] = self.capacity.value()
        elif self.kind == "subjects":
            result["color"] = self.color.text().strip()
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
        self.setWindowTitle("Занятие")
        self.setMinimumSize(680, 680)
        self.resize(720, 700)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.subject = self._combo(references.get("subjects", []), "name")
        self.teacher = self._combo(references.get("teachers", []), "full_name")
        self.room = self._combo(references.get("rooms", []), "name")
        self.group = self._combo(references.get("groups", []), "name", empty="Без группы")
        now = datetime.now().astimezone().replace(second=0, microsecond=0)
        rounded = now + timedelta(minutes=(30 - now.minute % 30) % 30)
        self.start = QDateTimeEdit(QDateTime(rounded))
        self.end = QDateTimeEdit(QDateTime(rounded + timedelta(hours=1)))
        for editor in (self.start, self.end):
            editor.setCalendarPopup(True)
            configure_calendar(editor)
            editor.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.students = QTableWidget(0, 2)
        self.students.setHorizontalHeaderLabels(["Выбрать", "Ученик"])
        self.students.verticalHeader().setVisible(False)
        self.students.horizontalHeader().setStretchLastSection(True)
        selected = {p.get("person_id") for p in self.lesson.get("participants", [])}
        all_students = references.get("students", [])
        self.students.setRowCount(len(all_students))
        for row, person in enumerate(all_students):
            check = QCheckBox()
            check.setChecked(person.get("id") in selected)
            check.setProperty("person_id", person.get("id"))
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            holder_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            holder_layout.addWidget(check)
            self.students.setCellWidget(row, 0, holder)
            self.students.setItem(row, 1, QTableWidgetItem(person.get("full_name", "")))
        self.notes = QTextEdit(str(self.lesson.get("notes") or ""))
        self.notes.setMaximumHeight(80)
        self.repeat = QCheckBox("Повторять еженедельно")
        self.occurrences = QSpinBox()
        self.occurrences.setRange(2, 104)
        self.occurrences.setValue(4)
        self.occurrences.setEnabled(False)
        self.repeat.toggled.connect(self.occurrences.setEnabled)
        form.addRow("Предмет", self.subject)
        form.addRow("Учитель", self.teacher)
        form.addRow("Кабинет", self.room)
        form.addRow("Группа", self.group)
        form.addRow("Начало", self.start)
        form.addRow("Окончание", self.end)
        if not self.lesson:
            form.addRow("", self.repeat)
            form.addRow("Количество занятий", self.occurrences)
        layout.addLayout(form)
        layout.addWidget(QLabel("Участники"))
        layout.addWidget(self.students, 1)
        layout.addWidget(QLabel("Заметка"))
        layout.addWidget(self.notes)
        self._select(self.subject, self.lesson.get("subject_id"))
        self._select(self.teacher, self.lesson.get("teacher_id"))
        self._select(self.room, self.lesson.get("room_id"))
        self._select(self.group, self.lesson.get("group_id"))
        if not self.lesson:
            self.group.currentIndexChanged.connect(self._apply_group_defaults)
        if self.lesson.get("start_at"):
            self.start.setDateTime(
                QDateTime.fromString(self.lesson["start_at"], Qt.DateFormat.ISODate)
            )
            self.end.setDateTime(QDateTime.fromString(self.lesson["end_at"], Qt.DateFormat.ISODate))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply_group_defaults(self) -> None:
        group_id = self.group.currentData()
        group = next(
            (item for item in self.references.get("groups", []) if item.get("id") == group_id),
            None,
        )
        if group is None:
            return
        self._select(self.subject, group.get("subject_id"))
        self._select(self.teacher, group.get("default_teacher_id"))
        duration = int(group.get("default_duration_minutes") or 60)
        self.end.setDateTime(self.start.dateTime().addSecs(duration * 60))

    @staticmethod
    def _combo(items: list[dict[str, Any]], label: str, empty: str | None = None) -> QComboBox:
        combo = SearchableComboBox(
            placeholder="Фамилия или имя" if label == "full_name" else "Начните вводить…"
        )
        if empty:
            combo.addItem(empty, None)
        for item in items:
            if item.get("active", True):
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
        return {
            "subject_id": self.subject.currentData(),
            "teacher_id": self.teacher.currentData(),
            "room_id": self.room.currentData(),
            "group_id": self.group.currentData(),
            "start_at": self.start.dateTime().toString(Qt.DateFormat.ISODate),
            "end_at": self.end.dateTime().toString(Qt.DateFormat.ISODate),
            "participant_ids": participant_ids,
            "notes": self.notes.toPlainText().strip() or None,
        }

    def series_payload(self) -> dict[str, Any]:
        payload = self.payload()
        start = self.start.dateTime().toPython()
        end = self.end.dateTime().toPython()
        return {
            "subject_id": payload["subject_id"],
            "teacher_id": payload["teacher_id"],
            "room_id": payload["room_id"],
            "group_id": payload["group_id"],
            "starts_at": payload["start_at"],
            "duration_minutes": max(5, int((end - start).total_seconds() // 60)),
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
        self.setWindowTitle("Карточка занятия")
        self.setMinimumSize(720, 500)
        layout = QVBoxLayout(self)
        title = QLabel(str(lesson.get("subject_name_snapshot", "Занятие")))
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        start = datetime.fromisoformat(lesson["start_at"]).astimezone()
        end = datetime.fromisoformat(lesson["end_at"]).astimezone()
        details = QLabel(
            f"План: {start:%d.%m.%Y, %H:%M}–{end:%H:%M} · "
            f"{lesson.get('teacher_name_snapshot', '')} · "
            f"{lesson.get('room_name_snapshot', '')} · "
            f"{STATUS_LABELS.get(lesson.get('status'), lesson.get('status', ''))}"
        )
        layout.addWidget(details)
        self.actual_start: QDateTimeEdit | None = None
        self.actual_end: QDateTimeEdit | None = None
        self._actual_original: tuple[str | None, str | None] = (
            lesson.get("actual_start_at"),
            lesson.get("actual_end_at"),
        )
        if lesson.get("status") == "completed" and all(self._actual_original):
            actual_start = datetime.fromisoformat(str(self._actual_original[0])).astimezone()
            actual_end = datetime.fromisoformat(str(self._actual_original[1])).astimezone()
            duration = max(0, int((actual_end - actual_start).total_seconds() // 60))
            actual_form = QFormLayout()
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
            ["Участник", "Посещение", "Приход", "Уход", "Опоздание", "Отмена"]
        )
        participants = lesson.get("participants", [])
        self.table.setRowCount(len(participants))
        editable = lesson.get("status") != "cancelled"
        for row, participant in enumerate(participants):
            name = QTableWidgetItem(participant.get("person_name_snapshot", ""))
            name.setData(Qt.ItemDataRole.UserRole, participant.get("person_id"))
            self.table.setItem(row, 0, name)
            combo = QComboBox()
            for value, label in ATTENDANCE_LABELS.items():
                combo.addItem(label, value)
            combo.setCurrentIndex(combo.findData(participant.get("attendance_status")))
            combo.setEnabled(editable)
            self.table.setCellWidget(row, 1, combo)
            for column, key in ((2, "arrived_at"), (3, "left_at")):
                value = participant.get(key)
                text_value = f"{datetime.fromisoformat(value).astimezone():%H:%M}" if value else "—"
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
                    datetime.fromisoformat(cancelled_at).astimezone().strftime("%d.%m.%Y %H:%M")
                    if cancelled_at
                    else "время не указано"
                )
                cancellation = f"{actor}, {when}: {reason}"
            self.table.setItem(row, 5, QTableWidgetItem(cancellation))
        if open_person is not None:
            self.table.cellDoubleClicked.connect(
                lambda row, column: open_person(
                    int(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole))
                )
                if column == 0 and self.table.item(row, 0) is not None
                else None
            )
        layout.addWidget(self.table)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Закрыть")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

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
            QDateTime.fromString(str(value), Qt.DateFormat.ISODate).toString(
                Qt.DateFormat.ISODate
            )
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
        self.setMinimumSize(620, 430)
        layout = QVBoxLayout(self)
        actions = QHBoxLayout()
        self.student = SearchableComboBox(placeholder="Фамилия или имя")
        self.student.setMinimumWidth(280)
        for person in students:
            self.student.addItem(person.get("full_name", ""), person.get("id"))
        actions.addWidget(self.student, 1)
        actions.addWidget(_button("Добавить", self._add, "primary"))
        actions.addWidget(_button("Завершить участие", self._end, "warning"))
        layout.addLayout(actions)
        self.table = _table(["Ученик", "Начало", "Окончание", "Состояние"])
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
                datetime.fromisoformat(start).astimezone().strftime("%d.%m.%Y")
                if start
                else "После сохранения",
                datetime.fromisoformat(end).astimezone().strftime("%d.%m.%Y") if end else "—",
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
        self.day = QDateEdit(QDate.currentDate())
        self.day.setCalendarPopup(True)
        configure_calendar(self.day)
        self.duration = QSpinBox()
        self.duration.setRange(5, 480)
        self.duration.setValue(60)
        self.duration.setSuffix(" мин")
        self.teacher = LessonDialog._combo(references.get("teachers", []), "full_name")
        self.room = LessonDialog._combo(
            references.get("rooms", []), "name", empty="Любой кабинет"
        )
        form.addRow("Дата", self.day)
        form.addRow("Продолжительность", self.duration)
        form.addRow("Преподаватель", self.teacher)
        form.addRow("Предпочитаемый кабинет", self.room)
        layout.addLayout(form)
        layout.addWidget(QLabel("Ученики"))
        self.students = QTableWidget(0, 2)
        self.students.setHorizontalHeaderLabels(["Выбрать", "ФИО"])
        self.students.verticalHeader().setVisible(False)
        self.students.horizontalHeader().setStretchLastSection(True)
        people = references.get("students", [])
        self.students.setRowCount(len(people))
        for row, person in enumerate(people):
            check = QCheckBox()
            check.setProperty("person_id", person.get("id"))
            self.students.setCellWidget(row, 0, check)
            self.students.setItem(row, 1, QTableWidgetItem(person.get("full_name", "")))
        self.students.setMaximumHeight(180)
        layout.addWidget(self.students)
        layout.addWidget(_button("Найти варианты", self._request, "primary"))
        self.results = _table(["Дата", "Время", "Кабинет"])
        self.results.doubleClicked.connect(lambda _index: self.accept())
        layout.addWidget(self.results, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Создать занятие")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

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
        self.search_requested.emit(self.criteria())

    def set_slots(self, slots: list[dict[str, Any]]) -> None:
        self.slots = slots
        self.results.setRowCount(len(slots))
        for row, slot in enumerate(slots):
            start = datetime.fromisoformat(slot["start_at"]).astimezone()
            end = datetime.fromisoformat(slot["end_at"]).astimezone()
            for column, value in enumerate(
                (f"{start:%d.%m.%Y}", f"{start:%H:%M}–{end:%H:%M}", slot.get("room_name", ""))
            ):
                cell = QTableWidgetItem(str(value))
                cell.setData(Qt.ItemDataRole.UserRole, slot)
                self.results.setItem(row, column, cell)

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
        self.live_timer.timeout.connect(self.refresh_today)
        self.live_timer.start()

    def _today_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        actions = QHBoxLayout()
        actions.addWidget(_button("Добавить занятие", self.add_lesson, "primary"))
        actions.addWidget(_button("Найти свободное время", self.find_free_time))
        actions.addWidget(_button("Обновить", self.refresh))
        actions.addStretch(1)
        self.presence_person = SearchableComboBox(placeholder="Фамилия или имя")
        self.presence_person.setMinimumWidth(220)
        actions.addWidget(self.presence_person)
        actions.addWidget(_button("Пришёл", lambda: self.presence("arrival"), "primary"))
        actions.addWidget(_button("Ушёл", lambda: self.presence("departure"), "warning"))
        layout.addLayout(actions)
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
            ["Время", "Предмет", "Учитель", "Кабинет", "Статус", "Действия"]
        )
        layout.addWidget(self.today_lessons, 2)
        layout.addWidget(QLabel("Сейчас в клубе"))
        self.present_table = _table(["ФИО", "Время прихода"])
        self.present_table.setMaximumHeight(180)
        layout.addWidget(self.present_table)
        return page

    def _calendar_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        filters = QHBoxLayout()
        filters.setSpacing(8)
        filters.addWidget(QLabel("Режим"))
        self.calendar_period = QComboBox()
        self.calendar_period.setMinimumWidth(150)
        self.calendar_period.addItem("День", 1)
        self.calendar_period.addItem("Неделя", 7)
        self.calendar_period.addItem("Произвольный период", 0)
        self.calendar_period.currentIndexChanged.connect(self._calendar_period_changed)
        filters.addWidget(self.calendar_period)
        filters.addSpacing(10)
        filters.addWidget(QLabel("С"))
        self.calendar_date = QDateEdit(QDate.currentDate())
        self.calendar_date.setMinimumWidth(126)
        self.calendar_date.setCalendarPopup(True)
        configure_calendar(self.calendar_date)
        self.calendar_date.dateChanged.connect(self._calendar_period_changed)
        filters.addWidget(self.calendar_date)
        self.calendar_end = QDateEdit(QDate.currentDate())
        self.calendar_end.setCalendarPopup(True)
        configure_calendar(self.calendar_end)
        self.calendar_end.setEnabled(False)
        self.calendar_end.dateChanged.connect(self.load_calendar)
        self.calendar_end.setMinimumWidth(126)
        filters.addWidget(QLabel("по"))
        filters.addWidget(self.calendar_end)
        filters.addSpacing(10)
        filters.addWidget(QLabel("Показать"))
        self.calendar_filter_type = QComboBox()
        self.calendar_filter_type.setMinimumWidth(150)
        for label, value in (
            ("Весь клуб", None),
            ("Преподаватель", "teacher_id"),
            ("Ученик", "student_id"),
            ("Группа", "group_id"),
            ("Кабинет", "room_id"),
        ):
            self.calendar_filter_type.addItem(label, value)
        self.calendar_filter_type.currentIndexChanged.connect(self._calendar_filter_changed)
        filters.addWidget(self.calendar_filter_type)
        self.calendar_filter_value = SearchableComboBox(placeholder="Начните вводить…")
        self.calendar_filter_value.setMinimumWidth(180)
        self.calendar_filter_value.setVisible(False)
        self.calendar_filter_value.currentIndexChanged.connect(self.load_calendar)
        filters.addWidget(self.calendar_filter_value, 1)
        layout.addLayout(filters)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(_button("Добавить занятие", self.add_lesson, "primary"))
        actions.addStretch(1)
        actions.addWidget(_button("Предпросмотр", self.preview_calendar))
        actions.addWidget(_button("Сохранить PDF", self.save_calendar_pdf))
        actions.addWidget(_button("Печать", self.print_calendar))
        layout.addLayout(actions)
        self.calendar_table = _table(
            ["Время", "Предмет", "Учитель", "Кабинет", "Участников", "Статус"]
        )
        self.calendar_table.doubleClicked.connect(self._edit_calendar_selected)
        layout.addWidget(self.calendar_table)
        return page

    def _references_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.reference_tabs = QTabWidget()
        self.reference_tables: dict[str, QTableWidget] = {}
        for kind, title, headers in (
            ("subjects", "Предметы", ["Название", "Цвет", "Состояние"]),
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
            buttons.addStretch(1)
            buttons.addWidget(
                _button(
                    "Добавить", lambda checked=False, key=kind: self.edit_reference(key), "primary"
                )
            )
            if kind == "groups":
                buttons.addWidget(_button("Состав", self.manage_selected_group))
            buttons.addWidget(
                _button(
                    "Изменить", lambda checked=False, key=kind: self.edit_selected_reference(key)
                )
            )
            tab_layout.addLayout(buttons)
            table = _table(headers)
            self.reference_tables[kind] = table
            tab_layout.addWidget(table)
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
        self.journal_student = SearchableComboBox(placeholder="Фамилия или имя")
        self.journal_student.setMinimumWidth(260)
        student_actions.addWidget(self.journal_student)
        student_actions.addWidget(_button("Показать", self.load_student_history))
        student_actions.addStretch(1)
        student_layout.addLayout(student_actions)
        self.student_journal = _table(
            ["Дата", "Предмет", "Учитель", "Время", "Посещение", "Опоздание"]
        )
        student_layout.addWidget(self.student_journal)
        teacher_page = QWidget()
        teacher_layout = QVBoxLayout(teacher_page)
        teacher_actions = QHBoxLayout()
        self.journal_teacher = SearchableComboBox(placeholder="Фамилия или имя")
        self.journal_teacher.setMinimumWidth(260)
        teacher_actions.addWidget(self.journal_teacher)
        teacher_actions.addWidget(_button("Показать", self.load_teacher_history))
        teacher_actions.addStretch(1)
        teacher_layout.addLayout(teacher_actions)
        self.teacher_summary = QLabel()
        teacher_layout.addWidget(self.teacher_summary)
        self.teacher_journal = _table(
            ["Дата", "Предмет", "Кабинет", "Время", "Участников", "Статус"]
        )
        self.student_journal.doubleClicked.connect(self._open_student_journal_lesson)
        self.teacher_journal.doubleClicked.connect(self._open_teacher_journal_lesson)
        teacher_layout.addWidget(self.teacher_journal)
        tabs.addTab(student_page, "Ученики")
        tabs.addTab(teacher_page, "Преподаватели")
        layout.addWidget(tabs)
        return page

    def _run(
        self, fn: Callable[..., Any], *args: Any, done: Callable[[Any], None] | None = None
    ) -> None:
        worker = Worker(lambda: fn(*args))
        self._workers.add(worker)

        def finished(result: object) -> None:
            self._workers.discard(worker)
            (done or (lambda _result: self.refresh()))(result)

        def failed(message: str) -> None:
            self._workers.discard(worker)
            QMessageBox.critical(self, "Ошибка", message)

        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(failed)
        self.pool.start(worker)

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
            table.setRowCount(len(items))
            for row, item in enumerate(items):
                values = [item.get("name", "")]
                if kind == "subjects":
                    values.append(item.get("color", ""))
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
                    table.setItem(row, column, cell)

    def _today_loaded(self, data: object) -> None:
        self.today_data = data if isinstance(data, dict) else {}
        self.notifications_changed.emit(list(self.today_data.get("alerts", [])))
        lessons = self.today_data.get("lessons", [])
        self.today_lessons.setRowCount(len(lessons))
        for row, lesson in enumerate(lessons):
            start = datetime.fromisoformat(lesson["start_at"]).astimezone()
            end = datetime.fromisoformat(lesson["end_at"]).astimezone()
            values = [
                f"{start:%H:%M}–{end:%H:%M}",
                lesson.get("subject_name_snapshot", ""),
                lesson.get("teacher_name_snapshot", ""),
                lesson.get("room_name_snapshot", ""),
                STATUS_LABELS.get(lesson.get("status"), lesson.get("status", "")),
            ]
            for column, value in enumerate(values):
                self.today_lessons.setItem(row, column, QTableWidgetItem(str(value)))
            actions = QWidget()
            bar = QHBoxLayout(actions)
            bar.setContentsMargins(2, 2, 2, 2)
            if lesson.get("status") in {"planned", "scheduled"}:
                bar.addWidget(
                    _button(
                        "Начать",
                        lambda checked=False, item=lesson: self.lesson_action(item, "start"),
                        "primary",
                    )
                )
                bar.addWidget(
                    _button(
                        "Изменить",
                        lambda checked=False, item=lesson: self.edit_lesson(item),
                    )
                )
            elif lesson.get("status") == "in_progress":
                bar.addWidget(
                    _button(
                        "Завершить",
                        lambda checked=False, item=lesson: self.lesson_action(item, "finish"),
                        "primary",
                    )
                )
            bar.addWidget(
                _button("Карточка", lambda checked=False, item=lesson: self.open_lesson(item))
            )
            self.today_lessons.setCellWidget(row, 5, actions)
        present = self.today_data.get("present", [])
        self.present_table.setRowCount(len(present))
        for row, person in enumerate(present):
            arrived = datetime.fromisoformat(person["arrived_at"]).astimezone()
            self.present_table.setItem(row, 0, QTableWidgetItem(person.get("person_name", "")))
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
        local = datetime.now().astimezone().tzinfo
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
        self.calendar_lessons = data if isinstance(data, list) else []
        self.calendar_table.setRowCount(len(self.calendar_lessons))
        show_date = int(self.calendar_period.currentData() or 1) > 1
        for row, lesson in enumerate(self.calendar_lessons):
            start = datetime.fromisoformat(lesson["start_at"]).astimezone()
            values = [
                f"{start:%d.%m %H:%M}" if show_date else f"{start:%H:%M}",
                lesson.get("subject_name_snapshot", ""),
                lesson.get("teacher_name_snapshot", ""),
                lesson.get("room_name_snapshot", ""),
                len(lesson.get("participants", [])),
                STATUS_LABELS.get(lesson.get("status"), lesson.get("status", "")),
            ]
            for column, value in enumerate(values):
                self.calendar_table.setItem(row, column, QTableWidgetItem(str(value)))

    def _edit_calendar_selected(self, _index: object = None) -> None:
        row = self.calendar_table.currentRow()
        if 0 <= row < len(self.calendar_lessons):
            self.edit_lesson(self.calendar_lessons[row])

    def add_lesson(self) -> None:
        dialog = LessonDialog(self.references, parent=self)
        if dialog.exec():
            if dialog.repeat.isChecked():
                self._run(self.api.create_lesson_series, dialog.series_payload())
            else:
                self._run(self.api.create_lesson, dialog.payload())

    def find_free_time(self) -> None:
        dialog = FreeSlotDialog(self.references, self)

        def search(criteria: dict[str, Any]) -> None:
            self._run(
                lambda: self.api.free_slots(**criteria),
                done=lambda result: dialog.set_slots(result if isinstance(result, list) else []),
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
        lesson.start.setDateTime(QDateTime.fromString(slot["start_at"], Qt.DateFormat.ISODate))
        lesson.end.setDateTime(QDateTime.fromString(slot["end_at"], Qt.DateFormat.ISODate))
        selected_students = set(criteria.get("student_ids", []))
        for row in range(lesson.students.rowCount()):
            check = lesson.students.cellWidget(row, 0).findChild(QCheckBox)
            if check:
                check.setChecked(int(check.property("person_id")) in selected_students)
        if lesson.exec():
            self._run(self.api.create_lesson, lesson.payload())

    def edit_lesson(self, lesson: dict[str, Any]) -> None:
        if lesson.get("status") in {"completed", "cancelled"}:
            QMessageBox.information(
                self, "Занятие", "История завершённого занятия доступна только для просмотра."
            )
            return
        dialog = LessonDialog(self.references, lesson, self)
        if dialog.exec():
            if not lesson.get("series_id"):
                self._run(self.api.update_lesson, int(lesson["id"]), dialog.payload())
                return
            choice = QMessageBox(self)
            choice.setWindowTitle("Изменение серии")
            choice.setText("Какие занятия изменить?")
            only_button = choice.addButton("Только это", QMessageBox.ButtonRole.AcceptRole)
            future_button = choice.addButton("Это и будущие", QMessageBox.ButtonRole.ActionRole)
            all_button = choice.addButton("Всю серию", QMessageBox.ButtonRole.ActionRole)
            choice.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
            choice.exec()
            clicked = choice.clickedButton()
            if clicked == only_button:
                self._run(self.api.update_lesson, int(lesson["id"]), dialog.payload())
            elif clicked in {future_button, all_button}:
                payload = dialog.series_payload()
                payload["scope"] = "future" if clicked == future_button else "all"
                payload["anchor_lesson_id"] = int(lesson["id"])
                self._run(
                    self.api.update_lesson_series,
                    int(lesson["series_id"]),
                    payload,
                )

    def open_lesson(self, lesson: dict[str, Any]) -> None:
        dialog = LessonCardDialog(
            lesson,
            self,
            open_person=lambda person_id: self.person_requested.emit(person_id),
        )
        if dialog.exec():
            changes = dialog.attendance()
            actual_time_change = dialog.actual_time_change()
            original = {
                int(item["person_id"]): str(item.get("attendance_status", "expected"))
                for item in lesson.get("participants", [])
            }
            cancellations: dict[int, str] = {}
            for person_id, attendance_status in changes:
                if (
                    lesson.get("status") != "completed"
                    and attendance_status == "excused"
                    and original.get(person_id) != "excused"
                ):
                    reason, accepted = QInputDialog.getText(
                        self,
                        "Отмена участия",
                        "Причина отмены:",
                    )
                    if not accepted or not reason.strip():
                        return
                    cancellations[person_id] = reason.strip()

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
                            {
                                "cancelled_by": "administrator",
                                "reason": cancellations[person_id],
                            },
                        )
                    elif before == "excused":
                        self.api.restore_lesson_participant(int(lesson["id"]), person_id)
                        if attendance_status != "expected":
                            self.api.set_attendance(
                                int(lesson["id"]), person_id, attendance_status
                            )
                    else:
                        self.api.set_attendance(
                            int(lesson["id"]), person_id, attendance_status
                        )

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
        self._prepare_schedule_printer(printer)
        preview = QPrintPreviewDialog(printer, self)
        preview.paintRequested.connect(document.print_)
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
        rows = []
        for lesson in self.calendar_lessons:
            start = datetime.fromisoformat(lesson["start_at"]).astimezone()
            rows.append(
                "<tr>"
                f"<td>{start:%d.%m.%Y}</td>"
                f"<td>{start:%H:%M}</td>"
                f"<td>{escape(str(lesson.get('subject_name_snapshot', '')))}</td>"
                f"<td>{escape(str(lesson.get('teacher_name_snapshot', '')))}</td>"
                f"<td>{escape(str(lesson.get('room_name_snapshot', '')))}</td>"
                "</tr>"
            )
        document = QTextDocument(self)
        period_label = self.calendar_date.date().toString("dd.MM.yyyy")
        if self.calendar_end.date() != self.calendar_date.date():
            period_label += f"–{self.calendar_end.date().toString('dd.MM.yyyy')}"
        filter_label = self.calendar_filter_type.currentText()
        if self.calendar_filter_value.isVisible():
            filter_label += f": {self.calendar_filter_value.currentText()}"
        document.setHtml(
            f"<h2>КРиТ · расписание на {period_label}</h2>"
            f"<p>{escape(filter_label)}</p>"
            "<table style='width:100%; border-collapse:collapse; word-wrap:break-word' "
            "cellspacing='0' cellpadding='6' border='1'>"
            "<tr><th>Дата</th><th>Время</th><th>Предмет</th><th>Учитель</th>"
            f"<th>Кабинет</th></tr>{''.join(rows)}</table>"
        )
        return document

    def _prepare_schedule_printer(self, printer: QPrinter) -> None:
        selected = self.calendar_date.date().toPython()
        selected_end = self.calendar_end.date().toPython()
        if (selected_end - selected).days >= 7 or len(self.calendar_lessons) >= 20:
            printer.setPageOrientation(QPageLayout.Orientation.Landscape)

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
            start = datetime.fromisoformat(lesson["start_at"]).astimezone()
            end = datetime.fromisoformat(lesson["end_at"]).astimezone()
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
            f"Отменено: {summary.get('cancelled', 0)}"
        )
        self.teacher_journal.setRowCount(len(lessons))
        for row, lesson in enumerate(lessons):
            start = datetime.fromisoformat(lesson["start_at"]).astimezone()
            end = datetime.fromisoformat(lesson["end_at"]).astimezone()
            values = [
                f"{start:%d.%m.%Y}",
                lesson.get("subject_name_snapshot", ""),
                lesson.get("room_name_snapshot", ""),
                f"{start:%H:%M}–{end:%H:%M}",
                len(lesson.get("participants", [])),
                STATUS_LABELS.get(lesson.get("status"), lesson.get("status", "")),
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
