from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from html import escape
from typing import Any

from PySide6.QtCore import QDate, QDateTime, Qt, QThreadPool, QTimer
from PySide6.QtGui import QTextDocument
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
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
    def __init__(self, kind: str, item: dict[str, Any] | None = None, parent=None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.item = item or {}
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
        form.addRow("Название", self.name)
        if kind == "rooms":
            form.addRow("Вместимость", self.capacity)
        if kind == "subjects":
            form.addRow("Цвет", self.color)
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
            result["subject_id"] = self.item.get("subject_id")
        return result


class LessonDialog(QDialog):
    def __init__(
        self, references: dict[str, Any], lesson: dict[str, Any] | None = None, parent=None
    ) -> None:
        super().__init__(parent)
        self.references = references
        self.lesson = lesson or {}
        self.setWindowTitle("Занятие")
        self.setMinimumSize(600, 560)
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

    @staticmethod
    def _combo(items: list[dict[str, Any]], label: str, empty: str | None = None) -> QComboBox:
        combo = QComboBox()
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


class LessonCardDialog(QDialog):
    def __init__(self, lesson: dict[str, Any], parent=None) -> None:
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
            f"{start:%d.%m.%Y, %H:%M}–{end:%H:%M} · "
            f"{lesson.get('teacher_name_snapshot', '')} · "
            f"{lesson.get('room_name_snapshot', '')} · "
            f"{STATUS_LABELS.get(lesson.get('status'), lesson.get('status', ''))}"
        )
        layout.addWidget(details)
        self.table = _table(["Участник", "Посещение", "Приход", "Уход", "Опоздание"])
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
        self.student = QComboBox()
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


class LearningPage(QWidget):
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
        actions.addWidget(_button("Обновить", self.refresh))
        actions.addStretch(1)
        self.presence_person = QComboBox()
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
        actions = QHBoxLayout()
        self.calendar_date = QDateEdit(QDate.currentDate())
        self.calendar_date.setCalendarPopup(True)
        self.calendar_date.dateChanged.connect(self.load_calendar)
        actions.addWidget(self.calendar_date)
        self.calendar_period = QComboBox()
        self.calendar_period.addItem("День", 1)
        self.calendar_period.addItem("Неделя", 7)
        self.calendar_period.currentIndexChanged.connect(self.load_calendar)
        actions.addWidget(self.calendar_period)
        actions.addWidget(_button("Добавить занятие", self.add_lesson, "primary"))
        actions.addWidget(_button("Печать / PDF", self.print_calendar))
        actions.addStretch(1)
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
            ("groups", "Группы", ["Название", "Состояние"]),
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
        self.journal_student = QComboBox()
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
        self.journal_teacher = QComboBox()
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
        for kind, table in self.reference_tables.items():
            items = self.references.get(kind, [])
            table.setRowCount(len(items))
            for row, item in enumerate(items):
                values = [item.get("name", "")]
                if kind == "subjects":
                    values.append(item.get("color", ""))
                elif kind == "rooms":
                    values.append(item.get("capacity", ""))
                values.append("Активен" if item.get("active") else "Отключён")
                for column, value in enumerate(values):
                    cell = QTableWidgetItem(str(value))
                    cell.setData(Qt.ItemDataRole.UserRole, item)
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    table.setItem(row, column, cell)

    def _today_loaded(self, data: object) -> None:
        self.today_data = data if isinstance(data, dict) else {}
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

    def load_calendar(self) -> None:
        selected = self.calendar_date.date().toPython()
        local = datetime.now().astimezone().tzinfo
        start = datetime.combine(selected, datetime.min.time(), tzinfo=local)
        days = int(self.calendar_period.currentData() or 1)
        self._run(
            self.api.learning_lessons,
            start.isoformat(),
            (start + timedelta(days=days)).isoformat(),
            done=self._calendar_loaded,
        )

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
        dialog = LessonCardDialog(lesson, self)
        if dialog.exec():
            changes = dialog.attendance()

            def save_all() -> None:
                for person_id, attendance_status in changes:
                    self.api.set_attendance(int(lesson["id"]), person_id, attendance_status)

            self._run(save_all)

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
        dialog = ReferenceDialog(kind, item, self)
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
        dialog = QPrintDialog(printer, self)
        if dialog.exec():
            document.print_(printer)

    def _schedule_document(self) -> QTextDocument:
        rows = []
        for lesson in self.calendar_lessons:
            start = datetime.fromisoformat(lesson["start_at"]).astimezone()
            rows.append(
                "<tr>"
                f"<td>{start:%H:%M}</td>"
                f"<td>{escape(str(lesson.get('subject_name_snapshot', '')))}</td>"
                f"<td>{escape(str(lesson.get('teacher_name_snapshot', '')))}</td>"
                f"<td>{escape(str(lesson.get('room_name_snapshot', '')))}</td>"
                "</tr>"
            )
        document = QTextDocument(self)
        period_label = (
            self.calendar_date.date().toString("dd.MM.yyyy")
            if int(self.calendar_period.currentData() or 1) == 1
            else (
                f"{self.calendar_date.date().toString('dd.MM.yyyy')}–"
                f"{self.calendar_date.date().addDays(6).toString('dd.MM.yyyy')}"
            )
        )
        document.setHtml(
            f"<h2>КРиТ · расписание на {period_label}</h2>"
            "<table cellspacing='0' cellpadding='6' border='1'>"
            "<tr><th>Время</th><th>Предмет</th><th>Учитель</th>"
            f"<th>Кабинет</th></tr>{''.join(rows)}</table>"
        )
        return document

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
                self.student_journal.setItem(row, column, QTableWidgetItem(str(value)))

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
                self.teacher_journal.setItem(row, column, QTableWidgetItem(str(value)))
