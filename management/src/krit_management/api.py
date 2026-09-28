from __future__ import annotations

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
