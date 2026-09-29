from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx


class ApiError(RuntimeError):
    pass


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
            "POST", "/auth/login", json={"username": username, "password": password}
        )
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            raise ApiError("Сервер не выдал токен доступа")
        self._client.headers["Authorization"] = f"Bearer {token}"

    def close(self) -> None:
        self._client.close()

    def check(self) -> dict[str, Any]:
        return self._request("GET", "/status")

    def snapshot(self) -> dict[str, Any]:
        data = self._request("GET", "/snapshot")
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

    def archive_person(self, person_id: int) -> dict[str, Any]:
        return self._request("POST", f"/people/{person_id}/archive")

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
        return data if isinstance(data, dict) else {}

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
        return self._request("POST", f"/learning/{kind}", json=payload)

    def update_learning_item(
        self, kind: str, item_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request("PUT", f"/learning/{kind}/{item_id}", json=payload)

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

    def presence_action(self, person_id: int, action: str) -> dict[str, Any]:
        return self._request("POST", f"/learning/presence/{person_id}/{action}")

    def student_history(self, person_id: int) -> dict[str, Any]:
        data = self._request("GET", f"/learning/history/person/{person_id}")
        return data if isinstance(data, dict) else {}

    def teacher_history(self, person_id: int) -> dict[str, Any]:
        data = self._request("GET", f"/learning/history/teacher/{person_id}")
        return data if isinstance(data, dict) else {}

    def set_attendance(
        self, lesson_id: int, person_id: int, attendance_status: str
    ) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/learning/lessons/{lesson_id}/participants/{person_id}/attendance",
            json={"status": attendance_status},
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

    def correct_actual_time(
        self, lesson_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request(
            "POST", f"/learning/lessons/{lesson_id}/correct-time", json=payload
        )

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

    def add_group_membership(self, group_id: int, person_id: int) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/learning/groups/{group_id}/memberships",
            json={"person_id": person_id, "start_at": datetime.now().astimezone().isoformat()},
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
        except httpx.HTTPError as exc:
            raise ApiError(f"Не удалось подключиться к серверу: {exc}") from exc
        if response.is_error:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            if response.status_code == 401:
                detail = "Неверный логин или пароль"
            raise ApiError(f"Ошибка {response.status_code}: {detail}")
        return response.json()
