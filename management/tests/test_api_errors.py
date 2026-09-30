from __future__ import annotations

import httpx
import pytest

from krit_management.api import ApiError, ManagementApi, _format_api_error


def test_schedule_conflict_is_explained_without_transport_details() -> None:
    message = _format_api_error(
        409,
        {
            "message": "Обнаружены конфликты расписания",
            "conflicts": [
                {
                    "kind": "room",
                    "lesson_id": 3,
                    "start_at": "2026-09-30T05:30:00+00:00",
                    "message": "Кабинет уже занят",
                }
            ],
        },
    )

    assert "Выбранный кабинет уже занят" in message
    assert "30.09.2026" in message
    assert "Измените время" in message
    assert "409" not in message
    assert "lesson_id" not in message
    assert "conflicts" not in message


def test_capacity_and_dependency_errors_explain_next_action() -> None:
    capacity = _format_api_error(
        409,
        {
            "kind": "capacity",
            "message": "Вместимость кабинета недостаточна",
            "capacity": 8,
            "participants": 10,
        },
    )
    dependencies = _format_api_error(
        409,
        {
            "message": "Сначала разрешите будущие обязательства",
            "dependencies": {
                "teacher_lessons": {"count": 2, "ids": [1, 2]},
                "default_groups": {"count": 1, "ids": [4]},
            },
        },
    )

    assert "8 мест" in capacity
    assert "10" in capacity
    assert "Выберите другой кабинет" in capacity
    assert "будущих занятий как преподаватель: 2" in dependencies
    assert "закреплённых групп: 1" in dependencies


def test_validation_and_common_http_errors_are_human_readable() -> None:
    validation = _format_api_error(
        422,
        [
            {"loc": ["body", "start_at"], "msg": "Input should be a valid datetime"},
            {"loc": ["body", "reason"], "msg": "Field required"},
        ],
    )

    assert "дата и время начала" in validation
    assert "причина" in validation
    assert "Field required" not in validation
    assert _format_api_error(404, "Not Found").startswith("Запись не найдена")
    assert "логин" in _format_api_error(401, "Unauthorized", "/auth/login")
    assert "Войдите" in _format_api_error(401, "Unauthorized", "/people")


def test_request_does_not_expose_http_codes_or_network_internals(monkeypatch) -> None:
    api = ManagementApi("http://127.0.0.1:1")
    request = httpx.Request("GET", "http://127.0.0.1:1/people")

    def fail(*_args, **_kwargs):
        raise httpx.ConnectError("[WinError 10061] secret technical detail", request=request)

    monkeypatch.setattr(api._client, "request", fail)
    with pytest.raises(ApiError) as exc_info:
        api.people()

    message = str(exc_info.value)
    assert "Проверьте подключение к сети" in message
    assert "WinError" not in message
    api.close()


def test_early_leave_falls_back_for_older_server(monkeypatch) -> None:
    api = ManagementApi("http://127.0.0.1:1")
    calls: list[tuple[str, str, dict[str, object]]] = []

    def request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        if path.endswith("/leave-early"):
            raise ApiError("Запись не найдена", status_code=404, path=path)
        return {"attendance_status": "left_early", "note": "Плохое самочувствие"}

    monkeypatch.setattr(api, "_request", request)

    result = api.leave_lesson_early(7, 12, "Плохое самочувствие")

    assert result["attendance_status"] == "left_early"
    assert calls[0][0] == "POST"
    assert calls[1] == (
        "PUT",
        "/learning/lessons/7/participants/12/attendance",
        {"json": {"status": "left_early", "note": "Плохое самочувствие"}},
    )
    api.close()


def test_early_leave_does_not_hide_non_404_errors(monkeypatch) -> None:
    api = ManagementApi("http://127.0.0.1:1")

    def request(*_args, **_kwargs):
        raise ApiError("Ученик фактически не участвует", status_code=409)

    monkeypatch.setattr(api, "_request", request)
    with pytest.raises(ApiError, match="фактически не участвует"):
        api.leave_lesson_early(7, 12, "Плохое самочувствие")
    api.close()
