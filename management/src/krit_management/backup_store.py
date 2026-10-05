from __future__ import annotations

import hashlib
import json
import os
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class BackupEntry:
    archive: Path
    metadata: Path
    created_at: datetime
    trust: str
    reason: str | None
    info: dict[str, Any]


class BackupStore:
    POINTER_NAME = "KRiT-latest-trusted.json"

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def default(cls) -> BackupStore:
        local_appdata = os.environ.get("LOCALAPPDATA")
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return cls(base / "KRiT" / "Backups")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _parse_time(value: object) -> datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def _atomic_json(self, path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".part")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def entries(self) -> list[BackupEntry]:
        if not self.root.is_dir():
            return []
        result: list[BackupEntry] = []
        for metadata_path in self.root.glob("KRiT-*.json"):
            if metadata_path.name == self.POINTER_NAME:
                continue
            try:
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                archive = self.root / str(payload["archive"])
                if not archive.is_file():
                    continue
                result.append(
                    BackupEntry(
                        archive=archive,
                        metadata=metadata_path,
                        created_at=self._parse_time(payload["created_at"]),
                        trust=str(payload["trust"]),
                        reason=payload.get("reason"),
                        info=dict(payload["info"]),
                    )
                )
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
        return sorted(result, key=lambda item: item.created_at)

    def latest_trusted(self) -> BackupEntry | None:
        pointer = self.root / self.POINTER_NAME
        if pointer.is_file():
            try:
                name = str(json.loads(pointer.read_text(encoding="utf-8"))["archive"])
                found = next(
                    (item for item in self.entries() if item.archive.name == name), None
                )
                if found is not None and found.trust == "trusted":
                    return found
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                pass
        trusted = [item for item in self.entries() if item.trust == "trusted"]
        return max(trusted, key=lambda item: item.created_at, default=None)

    def _update_pointer(self) -> None:
        trusted = [item for item in self.entries() if item.trust == "trusted"]
        pointer = self.root / self.POINTER_NAME
        if not trusted:
            pointer.unlink(missing_ok=True)
            return
        latest = max(trusted, key=lambda item: item.created_at)
        self._atomic_json(
            pointer,
            {"archive": latest.archive.name, "created_at": latest.created_at.isoformat()},
        )

    def _verify_archive(self, archive_path: Path, info: dict[str, Any]) -> dict[str, Any]:
        with zipfile.ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            manifest_databases = {
                str(item["name"]): item for item in manifest.get("databases", [])
            }
            expected_names = {str(item["name"]) for item in info.get("databases", [])}
            if set(manifest_databases) != expected_names:
                raise ValueError("backup database set mismatch")
            for item in manifest_databases.values():
                content = archive.read(str(item["filename"]))
                if len(content) != int(item["size"]):
                    raise ValueError("backup database size mismatch")
                if hashlib.sha256(content).hexdigest() != str(item["sha256"]):
                    raise ValueError("backup database checksum mismatch")
        return manifest

    @staticmethod
    def _trust_for(
        previous: BackupEntry | None, manifest: dict[str, Any]
    ) -> tuple[str, str | None]:
        if previous is None:
            return "trusted", None
        previous_databases = {
            str(item["name"]): item for item in previous.info.get("databases", [])
        }
        for current in manifest.get("databases", []):
            former = previous_databases.get(str(current.get("name")))
            if former is None:
                continue
            former_counts = former.get("critical_counts") or {}
            current_counts = current.get("critical_counts") or {}
            decreased = any(
                int(current_counts.get(key, 0)) < int(value)
                for key, value in former_counts.items()
            )
            if decreased:
                former_watermark = former.get("audit_watermark")
                current_watermark = current.get("audit_watermark")
                if current_watermark is None or (
                    former_watermark is not None
                    and int(current_watermark) <= int(former_watermark)
                ):
                    return "suspicious", "unexplained_data_loss"
        return "trusted", None

    def install_from_stream(
        self, info: dict[str, Any], chunks: Iterable[bytes]
    ) -> BackupEntry:
        self.root.mkdir(parents=True, exist_ok=True)
        created_at = self._parse_time(info["created_at"])
        backup_id = str(info["id"])
        if not backup_id or len(backup_id) > 64 or not backup_id.replace("-", "").isalnum():
            raise ValueError("invalid backup id")
        stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
        archive = self.root / f"KRiT-{stamp}-{backup_id}.backup"
        partial = archive.with_suffix(".backup.part")
        metadata = archive.with_suffix(".json")
        digest = hashlib.sha256()
        size = 0
        try:
            with partial.open("wb") as stream:
                for chunk in chunks:
                    stream.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if size != int(info["size"]):
                raise ValueError("backup size mismatch")
            if digest.hexdigest() != str(info["sha256"]):
                raise ValueError("backup checksum mismatch")
            manifest = self._verify_archive(partial, info)
            trust, reason = self._trust_for(self.latest_trusted(), manifest)
            os.replace(partial, archive)
            payload = {
                "archive": archive.name,
                "created_at": created_at.isoformat(),
                "trust": trust,
                "reason": reason,
                "info": {**info, "databases": manifest.get("databases", [])},
            }
            self._atomic_json(metadata, payload)
            if trust == "trusted":
                self._update_pointer()
            return BackupEntry(
                archive=archive,
                metadata=metadata,
                created_at=created_at,
                trust=trust,
                reason=reason,
                info=payload["info"],
            )
        except Exception:
            partial.unlink(missing_ok=True)
            if archive.is_file() and not metadata.is_file():
                archive.unlink(missing_ok=True)
            raise

    def rotate(self, *, daily: int = 7, weekly: int = 4) -> None:
        trusted = sorted(
            (item for item in self.entries() if item.trust == "trusted"),
            key=lambda item: item.created_at,
            reverse=True,
        )
        keep: set[Path] = set()
        daily_dates: set[object] = set()
        for item in trusted:
            if len(daily_dates) >= daily:
                break
            if item.created_at.date() not in daily_dates:
                daily_dates.add(item.created_at.date())
                keep.add(item.archive)
        weekly_keys: set[tuple[int, int]] = set()
        for item in trusted:
            if item.archive in keep:
                continue
            iso = item.created_at.isocalendar()
            key = (iso.year, iso.week)
            if key not in weekly_keys and len(weekly_keys) < weekly:
                weekly_keys.add(key)
                keep.add(item.archive)
        for item in reversed(trusted):
            if item.archive not in keep:
                item.archive.unlink(missing_ok=True)
                item.metadata.unlink(missing_ok=True)
        self._update_pointer()

    def trust(self, archive_name: str) -> None:
        entry = next((item for item in self.entries() if item.archive.name == archive_name), None)
        if entry is None:
            raise FileNotFoundError(archive_name)
        payload = json.loads(entry.metadata.read_text(encoding="utf-8"))
        payload["trust"] = "trusted"
        payload["reason"] = None
        self._atomic_json(entry.metadata, payload)
        self._update_pointer()

    def delete_suspicious(self, archive_name: str) -> None:
        entry = next((item for item in self.entries() if item.archive.name == archive_name), None)
        if entry is None:
            return
        if entry.trust != "suspicious":
            raise ValueError("trusted backup cannot be deleted by suspicious cleanup")
        entry.archive.unlink(missing_ok=True)
        entry.metadata.unlink(missing_ok=True)

    def discard_partial(self) -> None:
        if self.root.is_dir():
            for partial in self.root.glob("*.part"):
                partial.unlink(missing_ok=True)
