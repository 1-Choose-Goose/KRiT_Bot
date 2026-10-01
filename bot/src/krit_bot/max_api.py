from __future__ import annotations

import base64
import hashlib
import hmac
import ssl
from pathlib import Path
from typing import Any

import httpx
import truststore


class MaxApiError(RuntimeError):
    def __init__(
        self, message: str, *, status_code: int | None = None, code: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code

    @property
    def transient(self) -> bool:
        return (
            self.code == "attachment.not.ready"
            or self.status_code is None
            or self.status_code in {408, 425, 429}
            or self.status_code >= 500
        )


class MaxApiClient:
    def __init__(self, *, token: str, base_url: str) -> None:
        self._token = token
        ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        russian_ca = Path(__file__).with_name("certs") / "russian_trusted_ca.pem"
        ssl_context.load_verify_locations(cafile=str(russian_ca))
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": token, "User-Agent": "KRiT-Bot/0.1"},
            timeout=httpx.Timeout(100, connect=15),
            verify=ssl_context,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get_me(self) -> dict[str, Any]:
        return await self._request("GET", "/me")

    async def get_updates(
        self, *, marker: int | None, poll_timeout: int, limit: int = 100
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "timeout": poll_timeout,
            "limit": limit,
            "types": "message_created,message_callback,bot_started,bot_stopped,user_removed",
        }
        if marker is not None:
            params["marker"] = marker
        return await self._request("GET", "/updates", params=params)

    async def send_text(
        self, *, user_id: int, text: str, attachments: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"text": text, "notify": True}
        if attachments:
            body["attachments"] = attachments
        return await self._request(
            "POST",
            "/messages",
            params={"user_id": user_id},
            json=body,
        )

    async def get_chat(self, *, chat_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/chats/{chat_id}")

    async def get_membership(self, *, chat_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/chats/{chat_id}/members/me")

    async def get_chat_members(self, *, chat_id: int, user_ids: list[int]) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/chats/{chat_id}/members",
            params={"user_ids": ",".join(str(value) for value in user_ids)},
        )

    async def answer_callback(
        self, *, callback_id: str, notification: str | None = None
    ) -> dict[str, Any]:
        body = {"notification": notification} if notification else {}
        return await self._request(
            "POST", "/answers", params={"callback_id": callback_id}, json=body
        )

    async def send_to_chat(
        self, *, chat_id: int, text: str, attachments: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"text": text, "notify": True}
        if attachments:
            body["attachments"] = attachments
        return await self._request("POST", "/messages", params={"chat_id": chat_id}, json=body)

    async def create_upload(self, *, media_type: str) -> dict[str, Any]:
        return await self._request("POST", "/uploads", params={"type": media_type})

    async def upload_media(
        self, *, upload_url: str, content: bytes, filename: str, content_type: str
    ) -> dict[str, Any]:
        response = await self._client.post(
            upload_url,
            files={"data": (filename, content, content_type)},
        )
        if response.is_error:
            raise MaxApiError(
                f"MAX upload returned {response.status_code}", status_code=response.status_code
            )
        data = response.json()
        if not isinstance(data, dict):
            raise MaxApiError("MAX upload returned a non-object JSON response")
        return data

    async def request_contact(self, *, user_id: int, text: str) -> dict[str, Any]:
        return await self.send_text(
            user_id=user_id,
            text=text,
            attachments=[
                {
                    "type": "inline_keyboard",
                    "payload": {
                        "buttons": [
                            [
                                {
                                    "type": "request_contact",
                                    "text": "Поделиться номером",
                                }
                            ]
                        ]
                    },
                }
            ],
        )

    def verify_contact(self, *, vcf_info: str, signature: str) -> bool:
        digest = hmac.new(
            self._token.encode("utf-8"), vcf_info.encode("utf-8"), hashlib.sha256
        ).digest()
        provided = signature.strip()
        hex_digest = digest.hex()
        if len(provided) == len(hex_digest) and hmac.compare_digest(provided.lower(), hex_digest):
            return True
        base64_candidates = (
            base64.b64encode(digest).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
        )
        return any(hmac.compare_digest(provided, item) for item in base64_candidates)

    async def subscribe_webhook(
        self, *, url: str, secret: str, update_types: list[str] | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/subscriptions",
            json={
                "url": url,
                "update_types": update_types or ["message_created"],
                "secret": secret,
            },
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._client.request(method, path, **kwargs)
        if response.is_error:
            safe_body = response.text[:500]
            code = None
            try:
                error_body = response.json()
                if isinstance(error_body, dict) and error_body.get("code"):
                    code = str(error_body["code"])
            except ValueError:
                pass
            raise MaxApiError(
                f"MAX API returned {response.status_code}: {safe_body}",
                status_code=response.status_code,
                code=code,
            )
        data = response.json()
        if not isinstance(data, dict):
            raise MaxApiError("MAX API returned a non-object JSON response")
        return data
