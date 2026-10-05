from __future__ import annotations

import asyncio
import os
import platform
import shutil
import socket
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()
StatusCollector = Callable[[], Awaitable[dict[str, Any]]]


class SystemStatusProvider:
    """Build an allowlisted on-demand health snapshot with a short bounded cache."""

    def __init__(
        self,
        *,
        data_root: Path,
        database_collector: StatusCollector,
        worker_collector: StatusCollector,
        queue_collector: StatusCollector,
        backup_collector: StatusCollector,
        now: Callable[[], datetime] | None = None,
        cache_seconds: float = 10.0,
    ) -> None:
        self.data_root = data_root
        self.database_collector = database_collector
        self.worker_collector = worker_collector
        self.queue_collector = queue_collector
        self.backup_collector = backup_collector
        self.now = now or (lambda: datetime.now(UTC))
        self.cache_seconds = cache_seconds
        self.started_monotonic = time.monotonic()
        self._cached_at: datetime | None = None
        self._cached: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _system_uptime_seconds() -> int | None:
        try:
            return int(float(Path("/proc/uptime").read_text(encoding="ascii").split()[0]))
        except (OSError, ValueError, IndexError):
            return None

    @staticmethod
    def _memory_values() -> tuple[int | None, int | None, int | None]:
        try:
            values: dict[str, int] = {}
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                key, raw = line.split(":", 1)
                values[key] = int(raw.strip().split()[0]) * 1024
            total = values["MemTotal"]
            available = values["MemAvailable"]
            return total, total - available, available
        except (OSError, ValueError, KeyError):
            return None, None, None

    @staticmethod
    def _percentage(used: int | None, total: int | None) -> float | None:
        if used is None or total in {None, 0}:
            return None
        return round(used * 100 / total, 1)

    async def _collect(self, name: str, collector: StatusCollector) -> dict[str, Any]:
        try:
            return await collector()
        except Exception as exc:
            log.warning(
                "system_status_collection_failed",
                collector=name,
                error_type=type(exc).__name__,
            )
            return {"available": False, "error": "unavailable"}

    def _local_values(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        memory_total, memory_used, memory_available = self._memory_values()
        try:
            disk_total, disk_used, disk_free = shutil.disk_usage(self.data_root)
        except OSError:
            disk_total = disk_used = disk_free = None
        try:
            load_average = [round(item, 2) for item in os.getloadavg()]
        except (AttributeError, OSError):
            load_average = None
        try:
            krit_version = version("krit-max-bot")
        except PackageNotFoundError:
            krit_version = "development"
        server = {
            "hostname": socket.gethostname(),
            "os": platform.system(),
            "kernel": platform.release(),
            "python_version": platform.python_version(),
            "krit_version": krit_version,
            "system_uptime_seconds": self._system_uptime_seconds(),
            "process_uptime_seconds": int(time.monotonic() - self.started_monotonic),
        }
        resources = {
            "logical_cpus": os.cpu_count(),
            "load_average": load_average,
            "memory": {
                "total_bytes": memory_total,
                "used_bytes": memory_used,
                "available_bytes": memory_available,
                "used_percent": self._percentage(memory_used, memory_total),
            },
            "disk": {
                "total_bytes": disk_total,
                "used_bytes": disk_used,
                "free_bytes": disk_free,
                "used_percent": self._percentage(disk_used, disk_total),
            },
        }
        api = {
            "available": True,
            "uptime_seconds": server["process_uptime_seconds"],
        }
        return server, resources, api

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            now = self.now()
            if (
                self._cached is not None
                and self._cached_at is not None
                and (now - self._cached_at).total_seconds() <= self.cache_seconds
            ):
                return self._cached
            server, resources, api = self._local_values()
            database, bot, queues, backups = await asyncio.gather(
                self._collect("database", self.database_collector),
                self._collect("bot", self.worker_collector),
                self._collect("queues", self.queue_collector),
                self._collect("backups", self.backup_collector),
            )
            snapshot = {
                "collected_at": now.isoformat(),
                "server": server,
                "resources": resources,
                "database": database,
                "api": api,
                "bot": bot,
                "queues": queues,
                "backups": backups,
            }
            self._cached = snapshot
            self._cached_at = now
            return snapshot
