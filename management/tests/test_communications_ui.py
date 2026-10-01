from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QComboBox, QHeaderView, QLabel

from krit_management.communications_page import (
    PERSON_SEARCH_ROLE,
    CommunicationsPage,
    PollDetailsDialog,
)
from krit_management.widgets import SearchableComboBox


class FakeApi:
    pass


def test_communications_page_keeps_four_simple_tabs_and_escapes_chat_html() -> None:
    app = QApplication.instance() or QApplication([])
    page = CommunicationsPage(FakeApi())  # type: ignore[arg-type]
    page.resize(1100, 760)
    page.show()
    app.processEvents()

    assert [page.tabs.tabText(index) for index in range(page.tabs.count())] == [
        "Отправить",
        "Диалоги",
        "Подтверждения",
        "Настройки",
    ]
    page.people = [
        {
            "id": 1,
            "full_name": "Алексеев Александр Фёдорович",
            "roles": ["student", "parent"],
            "phone": "+7 (001) 000-00-17",
            "max_user_id": None,
        },
        {
            "id": 2,
            "full_name": "Куц Олег Олегович",
            "roles": ["parent"],
            "phone": "+7 (950) 722-00-00",
            "max_user_id": "max-2",
        },
    ]
    page._render_recipients()
    assert page.recipients.item(0, 2).text() == "Ученик, Родитель"
    assert page.recipients.item(0, 4).text() == "Не подключён"
    header = page.recipients.horizontalHeader()
    assert header.sectionResizeMode(1) == QHeaderView.ResizeMode.Stretch
    assert page.recipients.columnWidth(2) == 190
    assert page.recipients.columnWidth(3) == 165
    assert page.recipients.columnWidth(4) == 145
    page._recipient_cell_clicked(0, 1)
    assert page._selected_ids() == [1]
    assert page.recipients.item(0, 0).checkState() == Qt.CheckState.Checked
    page._recipient_cell_clicked(1, 3)
    assert page._selected_ids() == [1, 2]
    assert page.selected_count.text() == "Выбрано: 2"
    page.recipient_search.setText("нет совпадений")
    assert page.recipients.rowCount() == 0
    assert page._selected_ids() == [1, 2]
    page.recipient_search.clear()
    assert page.recipients.item(0, 0).checkState() == Qt.CheckState.Checked
    page._clear_recipient_selection()
    assert page._selected_ids() == []
    page.recipient_search.setText("Куц")
    page._recipient_cell_clicked(0, 1)
    assert page._selected_ids() == [2]
    page.recipient_search.clear()
    page._messages_loaded(
        [
            {
                "direction": "inbound",
                "created_at": "2026-10-01 10:00",
                "delivery_status": "received",
                "text": "<script>alert(1)</script>\nОбычный текст",
            }
        ]
    )
    assert "<script>" not in page.chat_history.toHtml()
    assert "<script>alert(1)</script>" in page.chat_history.toPlainText()
    page.current_person_id = 1
    page.reply_text.setText("Ответ администратора")
    page._reply_sent(1, "Ответ администратора")
    assert "Ответ администратора" in page.chat_history.toPlainText()
    assert "ожидает отправки" in page.chat_history.toPlainText()
    assert page.reply_text.text() == ""
    page._messages_loaded_for(
        1,
        [
            {
                "direction": "outbound",
                "created_at": "2026-10-01 10:01",
                "delivery_status": "sent",
                "text": "Ответ администратора",
            }
        ],
    )
    assert page.chat_history.toPlainText().count("Ответ администратора") == 1
    assert 1 not in page.pending_replies
    page.dialogs.blockSignals(True)
    page._dialogs_loaded(
        [
            {
                "person_id": 1,
                "full_name": "Куц Олег Олегович",
                "last_message_preview": "Нужна помощь",
                "admin_unread_count": 3,
            }
        ]
    )
    page.dialogs.blockSignals(False)
    dialog_item = page.dialogs.item(0)
    dialog_widget = page.dialogs.itemWidget(dialog_item)
    assert dialog_item.text() == "Куц Олег Олегович"
    assert dialog_widget.findChild(QLabel, "dialogName").text() == "Куц Олег Олегович"
    assert dialog_widget.findChild(QLabel, "dialogPreview").text() == "Нужна помощь"
    assert dialog_widget.findChild(QLabel, "unreadBadge").text() == "3"
    page._run = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    page._dialog_selected(dialog_item)
    assert dialog_item.data(Qt.ItemDataRole.UserRole + 2) == 0
    assert page.dialogs.itemWidget(dialog_item).findChild(QLabel, "unreadBadge") is None
    page._settings_loaded(
        [
            {
                "event_code": "lesson_confirmation_request",
                "recipient_context": "student",
                "offset_minutes": 1440,
                "enabled": True,
                "priority": "normal",
                "quiet_start": "22:00",
                "quiet_end": "08:00",
                "configuration": {
                    "follow_up": "once",
                    "follow_up_offset_minutes": 180,
                },
            }
        ]
    )
    assert page.settings.columnCount() == 9
    assert page.settings.cellWidget(0, 7).currentData() == "once"
    assert page.settings.item(0, 8).text() == "180"
    assert page.settings.item(0, 0).text() == "Подтверждение посещения"
    assert page.settings.columnWidth(0) == 250
    assert page.settings.rowHeight(0) >= 40
    assert page.person_overrides.rowCount() == 0
    assert not page.save_person_settings_button.isEnabled()
    assert page.publish_from.displayFormat() == "dd.MM.yyyy"
    assert page.publish_to.displayFormat() == "dd.MM.yyyy"
    priority = page.settings.cellWidget(0, 4)
    assert isinstance(priority, QComboBox)
    assert priority.currentText() == "Обычный"
    page._campaigns_loaded(
        [
            {
                "id": 1,
                "created_at": "2026-10-01T10:00:00",
                "type": "manual_message",
                "title": "Проверка",
                "status": "completed",
                "counts": {"sent": 2, "failed": 1},
            }
        ]
    )
    assert page.campaigns.item(0, 1).text() == "Сообщение"
    assert page.campaigns.item(0, 3).text() == "Завершена"
    assert page.campaigns.item(0, 4).text() == "доставлено: 2, ошибка: 1"
    page.confirmation_rows = [
        {
            "student_name": "Алексеева Анна",
            "lesson": "01.10 · 10:00 · Математика",
            "request_sent": True,
            "student_answer": "yes",
            "guardian_answer": "no",
            "status": "conflict",
            "reason": "—",
            "max_available": True,
            "needs_attention": True,
        }
    ]
    page._render_confirmations()
    assert page.confirmations.item(0, 3).text() == "Да"
    assert page.confirmations.item(0, 4).text() == "Нет"
    page._settings_people_loaded(
        [
            {
                "id": 7,
                "full_name": "Андреева Милана Олеговна",
                "phone": "+7 (001) 000-00-22",
            }
        ]
    )
    assert isinstance(page.settings_person, SearchableComboBox)
    page.settings_person._search("мил")
    assert page.settings_person._proxy.rowCount() == 1
    page.settings_person._search("0022")
    assert page.settings_person._proxy.rowCount() == 1
    assert "Андреева" in str(page.settings_person.itemData(1, PERSON_SEARCH_ROLE))
    assert page.person_overrides.columnWidth(3) == 250
    poll_dialog = PollDetailsDialog(
        {
            "title": "Придёте на занятие?",
            "counts": {"recipients": 3, "yes": 1, "no": 1, "no_response": 1},
            "recipients": [
                {
                    "full_name": "Олег Учитель",
                    "roles": ["teacher"],
                    "delivery_status": "sent",
                    "answer": None,
                    "answered_at": None,
                }
            ],
            "agreements": [
                {
                    "student_name": "Анна Ученица",
                    "student_answer": "no",
                    "guardian_name": "Ирина Родитель",
                    "guardian_answer": "yes",
                    "teacher_name": "Олег Учитель",
                    "teacher_answer": None,
                    "result": "conflict",
                }
            ],
        }
    )
    assert poll_dialog.recipients.item(0, 2).text() == "Доставлено, ответа нет"
    assert poll_dialog.agreements.item(0, 4).text() == "Олег Учитель"
    assert poll_dialog.agreements.item(0, 6).text() == "Ответы расходятся"
    poll_dialog.close()
    page.shutdown()
    page.close()
