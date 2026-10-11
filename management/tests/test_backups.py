from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

import krit_management.backup_store as backup_store_module
from krit_management.api import ManagementApi
from krit_management.backup_controller import BackupController
from krit_management.backup_store import BackupStore, application_directory


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


def test_default_store_uses_directory_next_to_frozen_executable(
    monkeypatch, tmp_path
) -> None:
    executable = tmp_path / "KRiTManagement.exe"
    monkeypatch.setattr(backup_store_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(backup_store_module.sys, "executable", str(executable))

    assert application_directory() == tmp_path
    assert BackupStore.default().root == tmp_path / "Backups"


def test_source_default_store_uses_management_directory(monkeypatch) -> None:
    monkeypatch.delattr(backup_store_module.sys, "frozen", raising=False)
    expected = Path(backup_store_module.__file__).resolve().parents[2]

    assert application_directory() == expected
    assert BackupStore.default().root == expected / "Backups"


def test_default_store_never_falls_back_to_localappdata(monkeypatch, tmp_path) -> None:
    monkeypatch.delattr(backup_store_module.sys, "frozen", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert tmp_path not in BackupStore.default().root.parents


def test_installer_preserves_writable_backup_directory() -> None:
    installer = (
        Path(__file__).resolve().parents[1] / "packaging" / "windows-installer.iss"
    ).read_text(encoding="utf-8-sig")

    assert '[Dirs]' in installer
    assert 'Name: "{app}\\Backups"' in installer
    assert "uninsneveruninstall" in installer


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


def _framed_database(body: bytes, *, footer_hash: str | None = None) -> bytes:
    footer = json.dumps(
        {
            "complete": True,
            "size": len(body),
            "sha256": footer_hash or hashlib.sha256(body).hexdigest(),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return (
        b"KRITDB1\n"
        + len(body).to_bytes(4, "big")
        + body
        + (0).to_bytes(4, "big")
        + len(footer).to_bytes(4, "big")
        + footer
    )


def test_management_api_streams_verified_database_with_progress(tmp_path) -> None:
    body = b"PGDMP" + b"backup-body" * 100
    framed = _framed_database(body)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path.endswith("/backups/exports")
            assert json.loads(request.content)["installation_id"] == "computer-001"
            return httpx.Response(
                201,
                json={
                    "id": "export-1",
                    "created_at": datetime.now(UTC).isoformat(),
                    "schema_version": "revision-7",
                    "databases": [{"name": "krit_bot"}],
                },
            )
        return httpx.Response(
            200,
            content=framed,
            headers={"content-length": str(len(framed))},
        )

    api = ManagementApi("https://example.test/api/v1")
    api._client.close()
    api._client = httpx.Client(
        base_url="https://example.test/api/v1",
        transport=httpx.MockTransport(handler),
    )
    assert api.begin_backup_export("computer-001")["id"] == "export-1"
    progress: list[tuple[int, int | None]] = []
    destination = tmp_path / "krit_bot.dump"
    footer = api.download_backup_database(
        "export-1",
        "krit_bot",
        destination,
        lambda current, total: progress.append((current, total)),
    )
    assert destination.read_bytes() == body
    assert footer["sha256"] == hashlib.sha256(body).hexdigest()
    assert progress[-1] == (len(body), len(body))
    api.close()


def test_database_stream_without_valid_footer_is_deleted(tmp_path) -> None:
    body = b"PGDMPtruncated"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"KRITDB1\n" + len(body).to_bytes(4, "big") + body)

    api = ManagementApi("https://example.test/api/v1")
    api._client.close()
    api._client = httpx.Client(
        base_url="https://example.test/api/v1", transport=httpx.MockTransport(handler)
    )
    destination = tmp_path / "krit_bot.dump"

    with pytest.raises(ValueError, match="incomplete backup stream"):
        api.download_backup_database("export-1", "krit_bot", destination)

    assert not destination.exists()
    api.close()


def test_install_export_builds_compatible_trusted_archive(tmp_path) -> None:
    store = BackupStore(tmp_path / "Backups")
    database = tmp_path / "krit_bot.dump"
    database.write_bytes(b"PGDMPdatabase-content")
    created_at = datetime(2026, 10, 11, 12, tzinfo=UTC)
    export = {
        "id": "a" * 32,
        "created_at": created_at.isoformat(),
        "schema_version": "revision-7",
        "databases": [
            {
                "name": "krit_bot",
                "critical_counts": {"persons": 3},
                "audit_watermark": 12,
            }
        ],
    }

    entry = store.install_export(export, {"krit_bot": database})

    assert entry.trust == "trusted"
    assert entry.archive.parent == store.root
    assert entry.archive.suffix == ".backup"
    assert store.latest_trusted().archive == entry.archive
    assert not list(store.root.glob("*.part"))
    with zipfile.ZipFile(entry.archive) as archive:
        assert set(archive.namelist()) == {"manifest.json", "krit_bot.dump"}


def _wait_for_backup(controller: BackupController, timeout_ms: int = 3_000) -> None:
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    loop = QEventLoop()
    controller.completed.connect(loop.quit)
    controller.failed.connect(loop.quit)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    app.processEvents()
    assert controller.running is False


def test_backup_controller_runs_once_and_respects_24_hour_guard(tmp_path) -> None:
    body = b"PGDMPdatabase-content"

    class Api:
        def __init__(self) -> None:
            self.starts = 0
            self.completions: list[dict] = []

        def begin_backup_export(self, installation_id: str) -> dict:
            self.starts += 1
            assert installation_id == "computer-001"
            return {
                "id": "b" * 32,
                "created_at": datetime.now(UTC).isoformat(),
                "schema_version": "revision-7",
                "databases": [
                    {
                        "name": "krit_bot",
                        "critical_counts": {"persons": 3},
                        "audit_watermark": 12,
                    }
                ],
            }

        def download_backup_database(
            self, _export_id: str, _name: str, destination, _progress=None
        ) -> dict:
            destination.write_bytes(body)
            return {
                "complete": True,
                "size": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }

        def complete_backup_export(
            self, _export_id: str, _installation_id: str, **payload
        ) -> dict:
            self.completions.append(payload)
            return payload

    api = Api()
    store = BackupStore(tmp_path / "Backups")
    controller = BackupController(
        api, store=store, installation_id="computer-001"  # type: ignore[arg-type]
    )

    assert controller.start_if_due() is True
    _wait_for_backup(controller)
    assert api.starts == 1
    assert store.latest_trusted() is not None
    assert api.completions[0]["status"] == "success"
    assert api.completions[0]["size"] > 0
    assert len(api.completions[0]["sha256"]) == 64
    assert controller.start_if_due() is False
    assert api.starts == 1
    controller.shutdown()


def test_backup_controller_rejects_parallel_start(tmp_path) -> None:
    class Api:
        def begin_backup_export(self, _installation_id: str) -> dict:
            return {
                "id": "c" * 32,
                "created_at": datetime.now(UTC).isoformat(),
                "schema_version": "revision-7",
                "databases": [{"name": "krit_bot"}],
            }

        def download_backup_database(self, *_args, **_kwargs) -> dict:
            import time

            time.sleep(0.1)
            destination = _args[2]
            destination.write_bytes(b"PGDMPcontent")
            return {}

        def complete_backup_export(self, *_args, **_kwargs) -> dict:
            return {}

    controller = BackupController(
        Api(),  # type: ignore[arg-type]
        store=BackupStore(tmp_path / "Backups"),
        installation_id="computer-001",
    )
    assert controller.start(manual=True) is True
    assert controller.start(manual=True) is False
    _wait_for_backup(controller)
    controller.shutdown()


def test_management_api_exposes_current_server_address() -> None:
    api = ManagementApi("https://krit.example/krit-api/api/v1")
    try:
        assert api.server_url() == "https://krit.example"
    finally:
        api.close()
