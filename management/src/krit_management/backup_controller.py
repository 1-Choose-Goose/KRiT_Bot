from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QThreadPool, Signal

from .api import ManagementApi
from .backup_store import BackupEntry, BackupStore
from .workers import Worker


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class BackupController(QObject):
    completed = Signal(object)
    failed = Signal(str)
    state_changed = Signal()

    def __init__(
        self,
        api: ManagementApi,
        *,
        store: BackupStore | None = None,
        installation_id: str | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.api = api
        self.store = store or BackupStore.default()
        self.installation_id = installation_id or self._load_installation_id()
        self.pool = QThreadPool(self)
        self._workers: set[Worker] = set()
        self.running = False

    @staticmethod
    def _load_installation_id() -> str:
        settings = QSettings("KRiT", "KRiTManagement")
        existing = str(settings.value("backup/installation_id", "") or "")
        if 8 <= len(existing) <= 64:
            return existing
        generated = uuid.uuid4().hex
        settings.setValue("backup/installation_id", generated)
        settings.sync()
        return generated

    def start_if_due(self) -> bool:
        latest = self.store.latest_trusted()
        if latest is not None and datetime.now(UTC) - latest.created_at < timedelta(hours=24):
            self.state_changed.emit()
            return False
        return self.start()

    def start(self, *, manual: bool = False) -> bool:
        del manual
        if self.running:
            return False
        self.running = True
        self.state_changed.emit()
        worker = Worker(self._create_local_backup)
        self._workers.add(worker)

        def complete(result: object) -> None:
            self._workers.discard(worker)
            self.running = False
            self.state_changed.emit()
            self.completed.emit(result)

        def failed(message: str) -> None:
            self._workers.discard(worker)
            self.running = False
            self.state_changed.emit()
            self.failed.emit(message)

        worker.signals.finished.connect(complete)
        worker.signals.failed.connect(failed)
        self.pool.start(worker)
        return True

    def _create_local_backup(self) -> BackupEntry:
        self.store.root.mkdir(parents=True, exist_ok=True)
        self.store.discard_partial()
        export = self.api.begin_backup_export(self.installation_id)
        export_id = str(export["id"])
        downloaded: dict[str, Path] = {}
        try:
            for row in export.get("databases", []):
                database_name = str(row["name"])
                destination = self.store.root / f"download-{export_id}-{database_name}.part"
                self.api.download_backup_database(
                    export_id, database_name, destination
                )
                downloaded[database_name] = destination
            entry = self.store.install_export(export, downloaded)
            digest = _sha256_file(entry.archive)
            try:
                self.api.complete_backup_export(
                    export_id,
                    self.installation_id,
                    status="success",
                    size=entry.archive.stat().st_size,
                    sha256=digest,
                )
            except Exception:
                # The verified local archive remains valid even if the lightweight
                # server receipt cannot be delivered during a network interruption.
                pass
            return entry
        except Exception as exc:
            try:
                self.api.complete_backup_export(
                    export_id,
                    self.installation_id,
                    status="failed",
                    error=str(exc),
                )
            except Exception:
                pass
            raise
        finally:
            for path in downloaded.values():
                path.unlink(missing_ok=True)
            for path in self.store.root.glob(f"download-{export_id}-*.part"):
                path.unlink(missing_ok=True)

    def shutdown(self) -> None:
        self.pool.clear()
        self.pool.waitForDone(16_000)
        self._workers.clear()
