from __future__ import annotations

from typing import Any

import httpx

from .timeutils import configure_center_timezone, now_center, parse_center

LOGIN_TIMEOUT = httpx.Timeout(30, connect=8)


class ApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: object = None,
        path: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail
        self.path = path


FIELD_LABELS = {
    "username": "логин",
    "password": "пароль",
    "full_name": "ФИО",
    "phone": "телефон",
    "max_auth_phone": "телефон для MAX",
    "roles": "роли",
    "name": "название",
    "capacity": "вместимость",
    "color": "цвет",
    "subject_id": "предмет",
    "teacher_id": "преподаватель",
    "room_id": "кабинет",
    "group_id": "группа",
    "start_at": "дата и время начала",
    "end_at": "время окончания",
    "duration_minutes": "продолжительность",
    "participant_ids": "участники",
    "reason": "причина",
}


def _local_time(value: object) -> str:
    try:
        return parse_center(value).strftime("%d.%m.%Y в %H:%M")
    except (TypeError, ValueError):
        return "в это время"


def _validation_message(item: dict[str, Any]) -> str:
    location = [part for part in item.get("loc", []) if part not in {"body", "query"}]
    field = FIELD_LABELS.get(str(location[-1]), str(location[-1])) if location else "данные"
    raw = str(item.get("msg") or "").strip()
    lowered = raw.lower()
    if lowered == "field required":
        problem = "нужно заполнить"
    elif "valid datetime" in lowered:
        problem = "укажите корректные дату и время"
    elif "valid integer" in lowered:
        problem = "укажите целое число"
    elif "at least" in lowered or "greater than or equal" in lowered:
        problem = "значение слишком маленькое"
    elif "at most" in lowered or "less than or equal" in lowered:
        problem = "значение слишком большое"
    elif lowered.startswith("value error,"):
        problem = raw.split(",", 1)[1].strip()
    else:
        problem = "проверьте значение"
    return f"Поле «{field}»: {problem}."


def _format_conflicts(detail: dict[str, Any]) -> str:
    lines: list[str] = []
    labels = {
        "room": "Выбранный кабинет уже занят",
        "teacher": "Выбранный преподаватель уже занят",
        "student": "Один из выбранных учеников уже занят",
        "person": "Выбранный человек уже занят",
        "capacity": "В кабинете недостаточно мест",
    }
    for conflict in detail.get("conflicts", []):
        if not isinstance(conflict, dict):
            continue
        fallback = str(conflict.get("message") or "Конфликт расписания")
        line = labels.get(str(conflict.get("kind")), fallback)
        if conflict.get("start_at"):
            line += f" {_local_time(conflict['start_at'])}"
        if line not in lines:
            lines.append(line)
    if not lines:
        return str(detail.get("message") or "Выбранное время занято.")
    return (
        "Выбранное время пересекается с другим занятием:\n• "
        + "\n• ".join(lines)
        + "\nИзмените время, кабинет или состав участников."
    )


def _format_dependencies(detail: dict[str, Any]) -> str:
    labels = {
        "teacher_lessons": "будущих занятий как преподаватель",
        "default_groups": "закреплённых групп",
        "student_lessons": "будущих занятий как ученик",
        "memberships": "активных групп",
    }
    parts = []
    for key, value in detail.get("dependencies", {}).items():
        count = value.get("count", 0) if isinstance(value, dict) else 0
        if count:
            parts.append(f"{labels.get(key, key)}: {count}")
    suffix = "\nСначала измените расписание или состав групп."
    return str(detail.get("message") or "Операция невозможна.") + (
        "\n• " + "\n• ".join(parts) + suffix if parts else suffix
    )


def _format_api_error(status_code: int, detail: object, path: str = "") -> str:
    if status_code == 401:
        return (
            "Неверный логин или пароль."
            if path.endswith("/auth/login")
            else "Сеанс завершён. Войдите в программу ещё раз."
        )
    if status_code == 403:
        return "Недостаточно прав для этого действия."
    if status_code == 404:
        return "Запись не найдена. Возможно, она уже была изменена или удалена."
    if status_code >= 500:
        return (
            "Сервис временно недоступен. Повторите позже."
            if status_code == 503
            else "На сервере возникла ошибка. Повторите действие позже."
        )
    if isinstance(detail, list):
        messages = [_validation_message(item) for item in detail if isinstance(item, dict)]
        return (
            "Проверьте заполнение формы:\n• " + "\n• ".join(messages)
            if messages
            else "Проверьте заполнение полей."
        )
    if isinstance(detail, dict):
        if detail.get("conflicts"):
            return _format_conflicts(detail)
        if detail.get("kind") == "capacity" or ("capacity" in detail and "participants" in detail):
            return (
                f"В кабинете {detail.get('capacity')} мест, "
                f"а выбрано участников: {detail.get('participants')}. "
                "Выберите другой кабинет или уменьшите состав."
            )
        if detail.get("dependencies"):
            return _format_dependencies(detail)
        if detail.get("message"):
            return str(detail["message"])
    if isinstance(detail, str):
        normalized = detail.strip()
        translated = {
            "Unknown role": "Выбрана неизвестная роль клиента.",
            "Invalid phone": "Проверьте формат контактного телефона.",
            "Invalid MAX authorization phone": ("Проверьте формат телефона для авторизации MAX."),
        }.get(normalized)
        if translated:
            return translated
        technical_defaults = {
            "Not Found",
            "Unauthorized",
            "Bad Request",
            "Internal Server Error",
            "Service Unavailable",
        }
        if normalized and normalized not in technical_defaults:
            return normalized
    defaults = {
        400: "Сервер не смог обработать запрос. Проверьте введённые данные.",
        409: "Действие конфликтует с текущими данными. Обновите информацию и повторите.",
        422: "Проверьте заполнение полей.",
        429: "Слишком много запросов. Подождите немного и повторите.",
        500: "На сервере возникла ошибка. Повторите действие позже.",
        503: "Сервис временно недоступен. Повторите позже.",
    }
    return defaults.get(status_code, "Операцию не удалось выполнить. Повторите позже.")


class ManagementApi:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"User-Agent": "KRiT-Management/0.1"},
            timeout=httpx.Timeout(15, connect=8),
        )

    def login(self, username: str, password: str) -> None:
        data = self._request(
            "POST",
            "/auth/login",
            json={"username": username, "password": password},
            timeout=LOGIN_TIMEOUT,
        )
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            raise ApiError("Сервер не выдал токен доступа")
        self._client.headers["Authorization"] = f"Bearer {token}"

    def close(self) -> None:
        self._client.close()

    def check(self) -> dict[str, Any]:
        data = self._request("GET", "/status")
        if isinstance(data, dict) and data.get("center_timezone"):
            configure_center_timezone(str(data["center_timezone"]))
        return data

    def snapshot(self) -> dict[str, Any]:
        data = self._request("GET", "/snapshot")
        if isinstance(data, dict) and data.get("center_timezone"):
            configure_center_timezone(str(data["center_timezone"]))
        return data if isinstance(data, dict) else {}

    def people(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/people")
        return data if isinstance(data, list) else []

    def archived_people(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/people-archive")
        return data if isinstance(data, list) else []

    def create_person(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/people", json=payload)

    def update_person(self, person_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/people/{person_id}", json=payload)

    def create_person_aggregate(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/people/aggregate", json=payload)

    def update_person_aggregate(self, person_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/people/{person_id}/aggregate", json=payload)

    def archive_person(
        self, person_id: int, *, resolve_future_student_dependencies: bool = False
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/people/{person_id}/archive",
            json={"resolve_future_student_dependencies": resolve_future_student_dependencies},
        )

    def restore_person(self, person_id: int) -> dict[str, Any]:
        return self._request("POST", f"/people/{person_id}/restore")

    def delete_person(self, person_id: int) -> dict[str, Any]:
        return self._request("DELETE", f"/people/{person_id}")

    def link_guardian(self, student_id: int, guardian_id: int) -> dict[str, Any]:
        return self._request("POST", f"/people/{student_id}/guardians/{guardian_id}")

    def unlink_guardian(self, student_id: int, guardian_id: int) -> dict[str, Any]:
        return self._request("DELETE", f"/people/{student_id}/guardians/{guardian_id}")

    def access_attempts(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/access-attempts")
        return data if isinstance(data, list) else []

    def learning_reference_data(self) -> dict[str, Any]:
        data = self._request("GET", "/learning/reference-data")
        if not isinstance(data, dict):
            return {}
        # Keep group membership current even when the management app works
        # with a server version whose reference-data response did not embed it.
        for group in data.get("groups", []):
            group_id = group.get("id")
            if group_id is not None:
                try:
                    group["memberships"] = self.group_memberships(int(group_id))
                except ApiError:
                    group.setdefault("memberships", [])
        return data

    def learning_today(self) -> dict[str, Any]:
        data = self._request("GET", "/learning/today")
        return data if isinstance(data, dict) else {}

    def learning_lessons(
        self,
        date_from: str,
        date_to: str,
        **filters: int | None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"date_from": date_from, "date_to": date_to}
        params.update({key: value for key, value in filters.items() if value is not None})
        data = self._request(
            "GET",
            "/learning/lessons",
            params=params,
        )
        return data if isinstance(data, list) else []

    def create_learning_item(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._request("POST", f"/learning/{kind}", json=payload)
        self._verify_subject_assignments(kind, payload, result)
        return result

    def update_learning_item(
        self, kind: str, item_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        result = self._request("PUT", f"/learning/{kind}/{item_id}", json=payload)
        self._verify_subject_assignments(kind, payload, result)
        return result

    @staticmethod
    def _verify_subject_assignments(kind: str, payload: dict[str, Any], result: object) -> None:
        if kind != "subjects" or "teacher_ids" not in payload:
            return
        if not isinstance(result, dict) or "teacher_ids" not in result:
            raise ApiError(
                "Сервер ещё не поддерживает закрепление преподавателей за предметами. "
                "Обновите серверную часть программы и повторите сохранение."
            )
        requested = {int(value) for value in payload.get("teacher_ids") or []}
        saved = {int(value) for value in result.get("teacher_ids") or []}
        if requested != saved:
            raise ApiError("Сервер сохранил не всех выбранных преподавателей")

    def create_lesson(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/learning/lessons", json=payload)

    def learning_lesson(self, lesson_id: int) -> dict[str, Any]:
        data = self._request("GET", f"/learning/lesson/{lesson_id}")
        return data if isinstance(data, dict) else {}

    def create_lesson_series(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/learning/series", json=payload)

    def update_lesson(self, lesson_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/learning/lessons/{lesson_id}", json=payload)

    def update_lesson_series(self, series_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/learning/series/{series_id}", json=payload)

    def lesson_action(
        self, lesson_id: int, action: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._request("POST", f"/learning/lessons/{lesson_id}/{action}", json=payload or {})

    def reconcile_lesson(self, lesson_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST", f"/learning/lessons/{lesson_id}/reconcile", json=payload
        )

    def presence_action(self, person_id: int, action: str) -> dict[str, Any]:
        return self._request("POST", f"/learning/presence/{person_id}/{action}")

    def student_history(self, person_id: int) -> dict[str, Any]:
        data = self._request("GET", f"/learning/history/person/{person_id}")
        return data if isinstance(data, dict) else {}

    def teacher_history(self, person_id: int) -> dict[str, Any]:
        data = self._request("GET", f"/learning/history/teacher/{person_id}")
        return data if isinstance(data, dict) else {}

    def set_attendance(
        self,
        lesson_id: int,
        person_id: int,
        attendance_status: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/learning/lessons/{lesson_id}/participants/{person_id}/attendance",
            json={"status": attendance_status, "note": note},
        )

    def correct_attendance(
        self,
        lesson_id: int,
        person_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/learning/lessons/{lesson_id}/participants/{person_id}/correct",
            json=payload,
        )

    def correct_actual_time(self, lesson_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/learning/lessons/{lesson_id}/correct-time", json=payload)

    def read_admin_notification(self, notification_id: int) -> dict[str, Any]:
        return self._request("POST", f"/learning/admin-notifications/{notification_id}/read")

    def admin_notifications(self, *, unread_only: bool = False) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/learning/admin-notifications",
            params={"unread_only": str(unread_only).lower()},
        )
        return data if isinstance(data, list) else []

    def read_all_admin_notifications(self) -> dict[str, Any]:
        return self._request("POST", "/learning/admin-notifications/read-all")

    def cancel_lesson_participant(
        self,
        lesson_id: int,
        person_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/learning/lessons/{lesson_id}/participants/{person_id}/cancel",
            json=payload,
        )

    def restore_lesson_participant(self, lesson_id: int, person_id: int) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/learning/lessons/{lesson_id}/participants/{person_id}/restore",
        )

    def leave_lesson_early(self, lesson_id: int, person_id: int, reason: str) -> dict[str, Any]:
        try:
            return self._request(
                "POST",
                f"/learning/lessons/{lesson_id}/participants/{person_id}/leave-early",
                json={"reason": reason},
            )
        except ApiError as exc:
            if exc.status_code != 404:
                raise
            # Older KRiT servers recorded early departure through attendance.
            # Keep desktop updates usable while the server is being rolled out.
            return self.set_attendance(lesson_id, person_id, "left_early", reason)

    def finish_lesson_early(
        self, lesson_id: int, reason: str, public_comment: str = ""
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/learning/lessons/{lesson_id}/finish-early",
            json={"reason": reason, "public_comment": public_comment or None},
        )

    def transition_lesson_teacher(self, lesson_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST", f"/learning/lessons/{lesson_id}/teacher-transition", json=payload
        )

    def free_slots(
        self,
        *,
        day: str,
        duration_minutes: int,
        teacher_id: int | None,
        room_id: int | None,
        student_ids: list[int],
    ) -> list[dict[str, Any]]:
        params: list[tuple[str, Any]] = [
            ("day", day),
            ("duration_minutes", duration_minutes),
        ]
        if teacher_id is not None:
            params.append(("teacher_id", teacher_id))
        if room_id is not None:
            params.append(("room_id", room_id))
        params.extend(("student_ids", item) for item in student_ids)
        data = self._request("GET", "/learning/free-slots", params=params)
        return data if isinstance(data, list) else []

    def group_memberships(self, group_id: int) -> list[dict[str, Any]]:
        data = self._request("GET", f"/learning/groups/{group_id}/memberships")
        return data if isinstance(data, list) else []

    def communication_send(
        self, person_ids: list[int], text: str, *, urgent: bool = False, preview: bool = False
    ) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/communications/send-message",
            json={
                "person_ids": person_ids,
                "text": text,
                "urgent": urgent,
                "preview": preview,
            },
        )
        return data if isinstance(data, dict) else {}

    def communication_poll(
        self, person_ids: list[int], text: str, *, preview: bool = False
    ) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/communications/polls",
            json={"person_ids": person_ids, "text": text, "preview": preview},
        )
        return data if isinstance(data, dict) else {}

    def communication_campaigns(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/communications/campaigns")
        return data if isinstance(data, list) else []

    def communication_poll_details(self, campaign_id: int) -> dict[str, Any]:
        data = self._request("GET", f"/communications/campaigns/{campaign_id}/poll")
        return data if isinstance(data, dict) else {}

    def retry_communication_campaign(self, campaign_id: int) -> dict[str, Any]:
        data = self._request(
            "POST", f"/communications/campaigns/{campaign_id}/retry-failed"
        )
        return data if isinstance(data, dict) else {}

    def communication_conversations(self, search: str = "") -> list[dict[str, Any]]:
        data = self._request("GET", "/communications/conversations", params={"search": search})
        return data if isinstance(data, list) else []

    def communication_messages(self, person_id: int) -> list[dict[str, Any]]:
        data = self._request("GET", f"/communications/conversations/{person_id}/messages")
        return data if isinstance(data, list) else []

    def communication_reply(self, person_id: int, text: str) -> dict[str, Any]:
        data = self._request(
            "POST",
            f"/communications/conversations/{person_id}/messages",
            json={"person_ids": [person_id], "text": text},
        )
        return data if isinstance(data, dict) else {}

    def communication_mark_read(self, person_id: int) -> dict[str, Any]:
        data = self._request("POST", f"/communications/conversations/{person_id}/read")
        return data if isinstance(data, dict) else {}

    def communication_confirmations(self, date_from: str, date_to: str) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/communications/confirmations",
            params={"date_from": date_from, "date_to": date_to},
        )
        return data if isinstance(data, list) else []

    def communication_global_settings(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/communications/settings/global")
        return data if isinstance(data, list) else []

    def save_communication_global_settings(
        self, rules: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        data = self._request("PUT", "/communications/settings/global", json=rules)
        return data if isinstance(data, list) else []

    def communication_person_settings(self, person_id: int) -> dict[str, Any]:
        data = self._request("GET", f"/communications/settings/person/{person_id}")
        return data if isinstance(data, dict) else {}

    def save_communication_person_settings(
        self, person_id: int, overrides: list[dict[str, Any]]
    ) -> dict[str, Any]:
        data = self._request(
            "PUT", f"/communications/settings/person/{person_id}", json=overrides
        )
        return data if isinstance(data, dict) else {}

    def publish_schedule(
        self, date_from: str, date_to: str, *, preview: bool = False
    ) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/communications/schedule/publish",
            json={"date_from": date_from, "date_to": date_to, "preview": preview},
        )
        return data if isinstance(data, dict) else {}

    def add_group_membership(self, group_id: int, person_id: int) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/learning/groups/{group_id}/memberships",
            json={"person_id": person_id, "start_at": now_center().isoformat()},
        )

    def end_group_membership(self, group_id: int, membership_id: int) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/learning/groups/{group_id}/memberships/{membership_id}/end",
            json={},
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise ApiError(
                "Сервер не ответил вовремя. Проверьте сеть и повторите действие."
            ) from exc
        except httpx.HTTPError as exc:
            raise ApiError(
                "Не удалось связаться с сервером. "
                "Проверьте подключение к сети и доступность сервера."
            ) from exc
        if response.is_error:
            try:
                payload = response.json()
                detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
            except ValueError:
                detail = response.text
            raise ApiError(
                _format_api_error(response.status_code, detail, path),
                status_code=response.status_code,
                detail=detail,
                path=path,
            )
        return response.json()
