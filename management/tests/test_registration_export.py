from __future__ import annotations

from docx import Document

from krit_management.registration_export import HEADERS, export_journal


def test_every_journal_exports_wrapped_rows_and_repeated_headers(tmp_path) -> None:
    for kind in HEADERS:
        path = tmp_path / f"{kind}.docx"
        export_journal(kind, [{
            "registration_number": "ТЕСТ-001",
            "registration_date": "2026-11-21",
            "document_date": "2026-11-20",
            "counterparty": "Длинное наименование " * 12,
            "subject": "Подробное содержание " * 15,
            "details": {"student_name": "Ученик", "received_date": "21.11.2026"},
        }], path)
        doc = Document(path)
        assert len(doc.tables[0].rows) == 3
        assert [cell.text for cell in doc.tables[0].rows[0].cells] == list(HEADERS[kind])
        assert doc.tables[0].rows[2].cells[0].text == "21.11.2026 № ТЕСТ-001"
        assert doc.tables[0].rows[0]._tr.trPr is not None
