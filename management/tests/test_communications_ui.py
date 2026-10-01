from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from krit_management.communications_page import CommunicationsPage


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
    page.shutdown()
    page.close()
