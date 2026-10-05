from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

DATABASE_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,62}$")
CommandRunner = Callable[[list[str], dict[str, str]], None]


def run_command(argv: list[str], environment: dict[str, str]) -> None:
    subprocess.run(
        argv,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env={**os.environ, **environment},
    )


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    owner_source = path if path.exists() else path.parent
    owner = owner_source.stat()
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    change_owner = getattr(os, "chown", None)
    if change_owner is not None:
        change_owner(temporary, owner.st_uid, owner.st_gid)
    os.replace(temporary, path)


def safe_extract(archive_path: Path, destination: Path) -> dict[str, Any]:
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        for item in manifest.get("databases", []):
            filename = str(item["filename"])
            pure = PurePosixPath(filename)
            if pure.is_absolute() or ".." in pure.parts or len(pure.parts) != 1:
                raise ValueError("Unsafe database archive member")
            target = destination / filename
            with archive.open(filename) as source, target.open("wb") as output:
                while block := source.read(1024 * 1024):
                    output.write(block)
    return manifest


def postgres_args(environment: dict[str, str]) -> list[str]:
    return [
        "--host",
        environment.get("KRIT_PGHOST", "localhost"),
        "--port",
        environment.get("KRIT_PGPORT", "5432"),
        "--username",
        environment.get("KRIT_PGUSER", "krit"),
    ]


def replace_database(
    database: str,
    dump: Path,
    *,
    environment: dict[str, str],
    runner: CommandRunner,
) -> None:
    args = postgres_args(environment)
    runner(
        [
            "psql",
            *args,
            "--dbname",
            "postgres",
            "--set",
            f"target_db={database}",
            "--command",
            (
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :'target_db' AND pid <> pg_backend_pid()"
            ),
        ],
        environment,
    )
    runner(["dropdb", *args, "--if-exists", database], environment)
    runner(["createdb", *args, database], environment)
    runner(
        [
            "pg_restore",
            *args,
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            "--dbname",
            database,
            str(dump),
        ],
        environment,
    )


def restore_operation(
    root: Path,
    operation_id: str,
    *,
    environment: dict[str, str] | None = None,
    runner: CommandRunner = run_command,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ValueError("Invalid operation id")
    config = dict(os.environ if environment is None else environment)
    config["PGPASSWORD"] = config.get("KRIT_PGPASSWORD", "")
    database_names = tuple(
        item.strip()
        for item in config.get("KRIT_DATABASE_NAMES", "krit_bot").split(",")
        if item.strip()
    )
    if not database_names or any(not DATABASE_NAME.fullmatch(name) for name in database_names):
        raise ValueError("Invalid database allowlist")
    operation_dir = root / operation_id
    state_path = operation_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("phase") != "applying":
        raise ValueError("Operation is not ready")
    credentials_path = operation_dir / "protected-admin.json"
    credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    admin_hash = str(credentials.get("password_hash") or "")
    if not admin_hash.startswith("$argon2") or len(admin_hash) > 512:
        raise ValueError("Invalid protected administrator hash")
    credentials_path.unlink()
    safety_root = root / "safety-pending"
    if safety_root.exists():
        raise RuntimeError("A pending safety set already exists")
    safety_root.mkdir(mode=0o700)
    extracted = operation_dir / "extracted"
    extracted.mkdir(mode=0o700)
    manifest = safe_extract(operation_dir / "upload.backup", extracted)
    members = {str(item["name"]): extracted / str(item["filename"]) for item in manifest["databases"]}
    args = postgres_args(config)
    try:
        for database in database_names:
            runner(
                [
                    "pg_dump",
                    *args,
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    "--dbname",
                    database,
                    "--file",
                    str(safety_root / f"{database}.dump"),
                ],
                config,
            )
        for database in database_names:
            replace_database(
                database,
                members[database],
                environment=config,
                runner=runner,
            )
        runner(["/opt/krit-bot/venv/bin/krit-migrate"], config)
        runner(
            [
                "psql",
                *args,
                "--dbname",
                "krit_bot",
                "--command",
                "UPDATE admin_users SET auth_version=auth_version+1",
            ],
            config,
        )
        runner(
            [
                "psql",
                *args,
                "--dbname",
                "krit_bot",
                "--set",
                f"admin_hash={admin_hash}",
                "--command",
                (
                    "INSERT INTO admin_users "
                    "(username, full_name, password_hash, role, active, "
                    "must_change_password, auth_version, is_protected, created_at, updated_at) "
                    "VALUES ('choose_goose', 'Суперадминистратор', :'admin_hash', "
                    "'superadmin', TRUE, TRUE, 1, TRUE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
                    "ON CONFLICT (username) DO UPDATE SET role='superadmin', active=TRUE, "
                    "must_change_password=TRUE, is_protected=TRUE, "
                    "auth_version=admin_users.auth_version+1, password_hash=EXCLUDED.password_hash, "
                    "updated_at=CURRENT_TIMESTAMP"
                ),
            ],
            config,
        )
    except Exception:
        for database in database_names:
            safety_dump = safety_root / f"{database}.dump"
            if safety_dump.exists():
                replace_database(
                    database,
                    safety_dump,
                    environment=config,
                    runner=runner,
                )
        state["phase"] = "failed"
        state["error"] = "restore_failed_and_safety_reapplied"
        atomic_json(state_path, state)
        raise
    safety = {
        "id": operation_id,
        "created_at": datetime.now(UTC).isoformat(),
        "databases": list(database_names),
        "size": sum(path.stat().st_size for path in safety_root.glob("*.dump")),
    }
    atomic_json(root / "pending-safety.json", safety)
    state["phase"] = "completed"
    state["error"] = None
    state["safety_set"] = safety
    atomic_json(state_path, state)
    return safety


def rollback_pending_safety(
    root: Path,
    *,
    environment: dict[str, str] | None = None,
    runner: CommandRunner = run_command,
) -> None:
    config = dict(os.environ if environment is None else environment)
    config["PGPASSWORD"] = config.get("KRIT_PGPASSWORD", "")
    pending_path = root / "pending-safety.json"
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    database_names = tuple(str(item) for item in pending.get("databases", []))
    if not database_names or any(not DATABASE_NAME.fullmatch(name) for name in database_names):
        raise ValueError("Invalid safety database set")
    safety_root = root / "safety-pending"
    for database in database_names:
        dump = safety_root / f"{database}.dump"
        if not dump.is_file():
            raise FileNotFoundError(dump)
    for database in database_names:
        replace_database(
            database,
            safety_root / f"{database}.dump",
            environment=config,
            runner=runner,
        )
    runner(["/opt/krit-bot/venv/bin/krit-migrate"], config)
    for dump in safety_root.glob("*.dump"):
        dump.unlink()
    safety_root.rmdir()
    pending_path.unlink()


def delete_pending_safety(root: Path) -> None:
    pending_path = root / "pending-safety.json"
    if not pending_path.is_file():
        raise FileNotFoundError(pending_path)
    safety_root = root / "safety-pending"
    if safety_root.is_dir():
        shutil.rmtree(safety_root)
    pending_path.unlink()


def main() -> int:
    if len(sys.argv) != 2:
        print(
            "Usage: krit_restore_helper.py OPERATION_ID|--rollback|--delete-safety",
            file=sys.stderr,
        )
        return 2
    root = Path("/var/lib/krit/restore")
    if sys.argv[1] == "--rollback":
        rollback_pending_safety(root)
    elif sys.argv[1] == "--delete-safety":
        delete_pending_safety(root)
    else:
        restore_operation(root, sys.argv[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
