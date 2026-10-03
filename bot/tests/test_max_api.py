from __future__ import annotations

import httpx
import pytest

from krit_bot.max_api import MaxApiClient, MaxApiError


async def test_read_timeout_is_classified_as_transient_max_error() -> None:
    async def fail(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("MAX did not respond")

    api = MaxApiClient(token="secret", base_url="https://platform-api2.max.ru")
    await api._client.aclose()
    api._client = httpx.AsyncClient(
        base_url="https://platform-api2.max.ru",
        transport=httpx.MockTransport(fail),
    )

    with pytest.raises(MaxApiError) as error:
        await api.get_me()

    assert error.value.code == "read_timeout"
    assert error.value.transient is True
    await api.close()


async def test_upload_uses_separate_client_without_bot_authorization() -> None:
    requests: list[httpx.Request] = []

    async def uploaded(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"token": "media-token"})

    api = MaxApiClient(token="secret", base_url="https://platform-api2.max.ru")
    await api._upload_client.aclose()
    api._upload_client = httpx.AsyncClient(transport=httpx.MockTransport(uploaded))

    result = await api.upload_media(
        upload_url="https://fu.oneme.ru/api/upload.do?id=1",
        content=b"content",
        filename="report.txt",
        content_type="text/plain",
    )

    assert result == {"token": "media-token"}
    assert requests and "Authorization" not in requests[0].headers
    await api.close()


async def test_upload_rejects_non_https_url() -> None:
    api = MaxApiClient(token="secret", base_url="https://platform-api2.max.ru")

    with pytest.raises(MaxApiError, match="HTTPS"):
        await api.upload_media(
            upload_url="http://example.test/upload",
            content=b"content",
            filename="report.txt",
            content_type="text/plain",
        )

    await api.close()
