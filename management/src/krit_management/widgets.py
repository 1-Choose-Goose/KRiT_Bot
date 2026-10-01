from __future__ import annotations

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QColor, QTextCharFormat, QWheelEvent
from PySide6.QtWidgets import QComboBox, QCompleter, QDateEdit, QDateTimeEdit, QFormLayout


def matches_word_prefix(query: str, value: str) -> bool:
    tokens = [part.casefold() for part in query.split() if part]
    words = [part.casefold() for part in value.split() if part]
    return all(any(word.startswith(token) for word in words) for token in tokens)


def configure_calendar(editor: QDateEdit | QDateTimeEdit) -> None:
    calendar = editor.calendarWidget()
    calendar.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
    calendar.setGridVisible(False)
    calendar.setHorizontalHeaderFormat(calendar.HorizontalHeaderFormat.ShortDayNames)
    calendar.setVerticalHeaderFormat(calendar.VerticalHeaderFormat.NoVerticalHeader)
    weekday = QTextCharFormat()
    weekday.setForeground(QColor("#445066"))
    weekend = QTextCharFormat()
    weekend.setForeground(QColor("#875400"))
    for day in (
        Qt.DayOfWeek.Monday,
        Qt.DayOfWeek.Tuesday,
        Qt.DayOfWeek.Wednesday,
        Qt.DayOfWeek.Thursday,
        Qt.DayOfWeek.Friday,
    ):
        calendar.setWeekdayTextFormat(day, weekday)
    for day in (Qt.DayOfWeek.Saturday, Qt.DayOfWeek.Sunday):
        calendar.setWeekdayTextFormat(day, weekend)


def configure_form_layout(form: QFormLayout) -> None:
    """Apply one responsive, scannable form grid throughout the application."""
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    form.setHorizontalSpacing(14)
    form.setVerticalSpacing(9)


class _WordPrefixProxy(QSortFilterProxyModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._tokens: list[str] = []

    def set_query(self, value: str) -> None:
        self.beginFilterChange()
        self._tokens = [part.casefold() for part in value.split() if part]
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if not self._tokens:
            return True
        model = self.sourceModel()
        if model is None:
            return False
        index = model.index(source_row, self.filterKeyColumn(), source_parent)
        return matches_word_prefix(
            " ".join(self._tokens),
            str(model.data(index, self.filterRole()) or ""),
        )


class SafeComboBox(QComboBox):
    """A combo box that does not change a closed selection with the mouse wheel."""

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if self.view().isVisible():
            super().wheelEvent(event)
            return
        event.ignore()


class SearchableComboBox(SafeComboBox):
    """Combo box with case-insensitive prefix search across every word."""

    def __init__(self, parent=None, *, placeholder: str = "Начните вводить…") -> None:
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(14)
        self.lineEdit().setPlaceholderText(placeholder)
        self._proxy = _WordPrefixProxy(self)
        self._proxy.setSourceModel(self.model())
        self._proxy.setFilterKeyColumn(self.modelColumn())
        self._completer = QCompleter(self._proxy, self)
        self._completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setCompletionColumn(self.modelColumn())
        self._completer.popup().setObjectName("searchCompleterPopup")
        self._completer.popup().setUniformItemSizes(True)
        self._completer.activated[str].connect(self._completion_activated)
        self.setCompleter(self._completer)
        self.lineEdit().textEdited.connect(self._search)
        self.lineEdit().editingFinished.connect(self._commit_exact_match)
        self.activated.connect(self._selection_made)
        self.currentIndexChanged.connect(
            lambda _index: QTimer.singleShot(0, self._show_text_from_start)
        )

    def _selection_made(self, _index: int) -> None:
        self._proxy.set_query("")
        self._show_text_from_start()

    def _completion_activated(self, text: str) -> None:
        index = self._exact_text_index(text)
        if index >= 0:
            self.setCurrentIndex(index)
        self._selection_made(index)

    def _exact_text_index(self, text: str) -> int:
        normalized = " ".join(text.split()).casefold()
        for index in range(self.count()):
            if " ".join(self.itemText(index).split()).casefold() == normalized:
                return index
        return -1

    def _commit_exact_match(self) -> None:
        index = self._exact_text_index(self.currentText())
        if index >= 0:
            self.setCurrentIndex(index)

    def currentData(self, role: int = Qt.ItemDataRole.UserRole):  # noqa: N802
        text = self.currentText()
        current_index = self.currentIndex()
        if current_index >= 0 and self.itemText(current_index) == text:
            return super().currentData(role)
        exact_index = self._exact_text_index(text)
        return self.itemData(exact_index, role) if exact_index >= 0 else None

    def _show_text_from_start(self) -> None:
        editor = self.lineEdit()
        editor.deselect()
        editor.setCursorPosition(0)

    def _search(self, text: str) -> None:
        self._proxy.set_query(text)
        if text.strip():
            self._completer.complete()

    def setSearchRole(self, role: int) -> None:  # noqa: N802
        """Use extra hidden text for filtering while keeping labels compact."""
        self._proxy.setFilterRole(role)

    def setModelColumn(self, visible_column: int) -> None:
        super().setModelColumn(visible_column)
        if hasattr(self, "_proxy"):
            self._proxy.setFilterKeyColumn(visible_column)
            self._completer.setCompletionColumn(visible_column)
