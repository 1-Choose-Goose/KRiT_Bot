"""Word exports for registration journals."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

HEADERS = {
    "contract": ("Дата регистрации и №", "Дата договора", "Заказчик (ФИО законного представителя)",
                 "Ученик (ФИО)", "Наименование услуги", "Срок оказания услуг",
                 "Стоимость услуг в месяц", "Соглашения об изменении цены"),
    "incoming": ("Дата регистрации и входящий №", "Дата получения", "Дата письма отправителя",
                 "Отправитель", "№ письма отправителя", "Краткое содержание",
                 "Ответственный / срок", "Результат / исходящий ответ"),
    "outgoing": ("Дата регистрации и исходящий №", "Дата отправки", "Дата подписания",
                 "Адресат", "Краткое содержание", "Подписал / исполнитель",
                 "Способ отправки", "Связь с входящим / примечание"),
}
TITLES = {"contract": "ЖУРНАЛ РЕГИСТРАЦИИ ДОГОВОРОВ",
          "incoming": "ЖУРНАЛ РЕГИСТРАЦИИ ВХОДЯЩЕЙ КОРРЕСПОНДЕНЦИИ",
          "outgoing": "ЖУРНАЛ РЕГИСТРАЦИИ ИСХОДЯЩЕЙ КОРРЕСПОНДЕНЦИИ"}


def _date(value: str | None) -> str:
    return date.fromisoformat(value).strftime("%d.%m.%Y") if value else ""


def _values(kind: str, record: dict[str, Any]) -> tuple[str, ...]:
    d = record.get("details") or {}
    number = str(record.get("registration_number") or "")
    registered = _date(record.get("registration_date"))
    base = f"{registered} № {number}".strip()
    if kind == "contract":
        amendments = d.get("amendments") or ""
        additions = [f"Примечание: {d['notes']}"] if d.get("notes") else []
        if d.get("source_number"):
            additions.append(f"№ в старом журнале: {d['source_number']}")
        if d.get("source_date"):
            additions.append(f"Дата в старом журнале: {d['source_date']}")
        return (base, _date(record.get("document_date")), record.get("counterparty") or "",
                d.get("student_name") or "", record.get("subject") or "",
                d.get("service_period") or "", d.get("monthly_price") or "",
                "\n".join(filter(None, (amendments, *additions))))
    if kind == "incoming":
        return (base, d.get("received_date") or "", _date(record.get("document_date")),
                record.get("counterparty") or "",
                record.get("document_number") or "",
                record.get("subject") or "",
                " / ".join(filter(None, (d.get("responsible"), d.get("due_date")))),
                "\n".join(filter(None, (d.get("result"), d.get("reply_number"),
                                          d.get("channel"), d.get("notes")))))
    return (base, d.get("sent_date") or "", _date(record.get("document_date")),
            record.get("counterparty") or "", record.get("subject") or "",
            " / ".join(filter(None, (d.get("signer"), d.get("responsible")))),
            d.get("channel") or "",
            "\n".join(filter(None, (d.get("incoming_number"),
                                      d.get("incoming_document_number"),
                                      d.get("incoming_document_date"), d.get("notes")))))


def export_journal(kind: str, records: list[dict[str, Any]], path: str | Path) -> None:
    if kind not in HEADERS:
        raise ValueError("Неизвестный журнал")
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Cm(29.7), Cm(21)
    section.orientation = WD_ORIENT.LANDSCAPE
    section.top_margin = section.bottom_margin = Cm(1.5)
    section.left_margin = section.right_margin = Cm(1.2)
    normal = document.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(9)
    paragraph = document.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run(TITLES[kind])
    run.bold = True
    run.font.size = Pt(12)
    table = document.add_table(rows=2, cols=8)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True
    for index, heading in enumerate(HEADERS[kind]):
        table.cell(0, index).text = heading
        table.cell(1, index).text = str(index + 1)
    for row in table.rows[:2]:
        for cell in row.cells:
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            for paragraph in cell.paragraphs:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                for run in paragraph.runs:
                    run.bold = True
    for row in table.rows[:2]:
        tr_pr = row._tr.get_or_add_trPr()
        repeat = OxmlElement("w:tblHeader")
        repeat.set(qn("w:val"), "true")
        tr_pr.append(repeat)
    for record in records:
        cells = table.add_row().cells
        for cell, value in zip(cells, _values(kind, record), strict=True):
            cell.text = str(value)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
    document.save(path)
