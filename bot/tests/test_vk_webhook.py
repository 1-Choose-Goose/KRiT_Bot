from __future__ import annotations

from fastapi.testclient import TestClient

from krit_bot.config import Settings
from krit_bot.webhook import create_app


def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'webhook.db'}",
        max_bot_token="test-max-token",
        max_token_file=tmp_path / "missing",
        bot_mode="webhook",
        vk_callback_secret="callback-secret",
        vk_callback_confirmation="confirmation-code",
        vk_community_id=225565387,
        max_channel_id=777,
        vk_syndication_enabled=False,
    )


def event(**overrides):
    payload = {
        "type": "wall_post_new",
        "group_id": 225565387,
        "secret": "callback-secret",
        "event_id": "event-1",
        "object": {"id": 10, "owner_id": -225565387},
    }
    payload.update(overrides)
    return payload


def test_confirmation(tmp_path) -> None:
    with TestClient(create_app(settings(tmp_path))) as client:
        response = client.post("/webhooks/vk", json=event(type="confirmation"))
    assert response.status_code == 200
    assert response.text == "confirmation-code"


def test_wrong_secret_rejected(tmp_path) -> None:
    with TestClient(create_app(settings(tmp_path))) as client:
        response = client.post("/webhooks/vk", json=event(secret="wrong"))
    assert response.status_code == 404


def test_other_community_rejected(tmp_path) -> None:
    with TestClient(create_app(settings(tmp_path))) as client:
        response = client.post("/webhooks/vk", json=event(group_id=1))
    assert response.status_code == 404


def test_valid_event_and_duplicate_are_acknowledged(tmp_path) -> None:
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        first = client.post("/webhooks/vk", json=event())
        second = client.post("/webhooks/vk", json=event(event_id="event-duplicate"))
    assert first.status_code == second.status_code == 200
    assert first.text == second.text == "ok"


def test_unrelated_event_is_acknowledged_without_job(tmp_path) -> None:
    with TestClient(create_app(settings(tmp_path))) as client:
        response = client.post("/webhooks/vk", json=event(type="wall_reply_new"))
    assert response.status_code == 200
    assert response.text == "ok"
