from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from krit_management.api import ManagementApi
from krit_management.backup_store import BackupStore


def _archive(
    *,
    created_at: datetime,
    people: int,
    watermark: int,
) -> tuple[dict, bytes]:
    database = b"sqlite database content" + str(people).encode()
    db_hash = hashlib.sha256(database).hexdigest()
    manifest = {
        "format": 1,
        "id": created_at.strftime("%Y%m%d%H%M%S"),
        "created_at": created_at.isoformat(),
        "schema_version": "revision-7",
        "archive_sha256": None,
        "databases": [
            {
                "name": "krit_bot",
                "filename": "krit_bot.sqlite",
                "size": len(database),
                "sha256": db_hash,
                "critical_counts": {"persons": people},
                "audit_watermark": watermark,
            }
        ],
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("krit_bot.sqlite", database)
    body = stream.getvalue()
    info = {
        "id": manifest["id"],
        "created_at": created_at.isoformat(),
        "schema_version": "revision-7",
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "databases": manifest["databases"],
    }
    return info, body


def test_default_store_uses_local_appdata(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert BackupStore.default().root == tmp_path / "KRiT" / "Backups"


def test_install_is_atomic_and_quarantines_unexplained_loss(tmp_path) -> None:
    store = BackupStore(tmp_path)
    first_info, first_body = _archive(
        created_at=datetime(2026, 10, 1, 10, tzinfo=UTC), people=10, watermark=50
    )
    first = store.install_from_stream(first_info, [first_body[:20], first_body[20:]])
    assert first.trust == "trusted"
    assert store.latest_trusted().archive == first.archive
    assert not list(tmp_path.glob("*.part"))

    suspicious_info, suspicious_body = _archive(
        created_at=datetime(2026, 10, 2, 10, tzinfo=UTC), people=2, watermark=50
    )
    suspicious = store.install_from_stream(suspicious_info, [suspicious_body])
    assert suspicious.trust == "suspicious"
    assert store.latest_trusted().archive == first.archive

    trusted_info, trusted_body = _archive(
        created_at=datetime(2026, 10, 3, 10, tzinfo=UTC), people=2, watermark=51
    )
    trusted = store.install_from_stream(trusted_info, [trusted_body])
    assert trusted.trust == "trusted"
    assert store.latest_trusted().archive == trusted.archive

    store.trust(suspicious.archive.name)
    assert store.latest_trusted().archive == trusted.archive
    assert next(
        item for item in store.entries() if item.archive == suspicious.archive
    ).trust == "trusted"
    store.delete_suspicious("missing.backup")


def test_bad_or_interrupted_download_preserves_previous_trusted_set(tmp_path) -> None:
    store = BackupStore(tmp_path)
    info, body = _archive(
        created_at=datetime(2026, 10, 1, 10, tzinfo=UTC), people=10, watermark=50
    )
    previous = store.install_from_stream(info, [body])
    bad_info = {**info, "id": "bad", "sha256": "0" * 64}
    with pytest.raises(ValueError, match="checksum"):
        store.install_from_stream(bad_info, [body])
    assert store.latest_trusted().archive == previous.archive
    assert not list(tmp_path.glob("*.part"))


def test_rotation_keeps_seven_daily_and_four_additional_weekly_sets(tmp_path) -> None:
    store = BackupStore(tmp_path)
    start = datetime(2026, 6, 1, 10, tzinfo=UTC)
    for index in range(60):
        info, body = _archive(
            created_at=start + timedelta(days=index),
            people=10,
            watermark=50 + index,
        )
        store.install_from_stream(info, [body])
    store.rotate(daily=7, weekly=4)
    trusted = [item for item in store.entries() if item.trust == "trusted"]
    assert len(trusted) == 11


def test_management_api_streams_backup_with_progress(tmp_path) -> None:
    body = b"backup-body" * 100

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"id": "backup-1"})
        return httpx.Response(
            200,
            content=body,
            headers={"content-length": str(len(body))},
        )

    api = ManagementApi("https://example.test/api/v1")
    api._client.close()
    api._client = httpx.Client(
        base_url="https://example.test/api/v1",
        transport=httpx.MockTransport(handler),
    )
    assert api.create_backup()["id"] == "backup-1"
    progress: list[tuple[int, int | None]] = []
    destination = tmp_path / "download.backup"
    assert (
        api.download_backup(
            "backup-1", destination, lambda current, total: progress.append((current, total))
        )
        == destination
    )
    assert destination.read_bytes() == body
    assert progress[-1] == (len(body), len(body))
    api.close()
