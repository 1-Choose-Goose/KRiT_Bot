from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"


def test_systemd_and_nginx_assets_are_locked_down() -> None:
    bot_service = (DEPLOY / "krit-bot.service").read_text(encoding="utf-8")
    restore_service = (DEPLOY / "krit-restore@.service").read_text(encoding="utf-8")
    rollback_service = (DEPLOY / "krit-restore-rollback.service").read_text(
        encoding="utf-8"
    )
    delete_service = (DEPLOY / "krit-restore-delete.service").read_text(encoding="utf-8")
    nginx = (DEPLOY / "nginx-krit.conf").read_text(encoding="utf-8")
    sudoers = (DEPLOY / "krit-restore.sudoers").read_text(encoding="utf-8")
    dispatcher = DEPLOY / "krit_restore_dispatch.py"

    assert "User=krit" in bot_service
    assert "Restart=always" in bot_service
    assert "127.0.0.1:8080" in nginx
    assert "proxy_request_buffering off" in nginx
    assert "User=root" in restore_service
    assert "ExecStopPost=/usr/bin/systemctl start krit-bot.service" in restore_service
    assert "--rollback" in rollback_service
    assert "--delete-safety" in delete_service
    assert "NOPASSWD:" in sudoers
    assert "/usr/local/sbin/krit-restore-dispatch" in sudoers
    assert "*" not in sudoers
    assert "ALL=(ALL) ALL" not in sudoers
    assert dispatcher.is_file()

    for asset in DEPLOY.iterdir():
        if asset.is_file() and asset.suffix in {".sh", ".service", ".sudoers", ".conf"}:
            relative_path = str(asset.relative_to(ROOT))
            attributes = subprocess.run(
                ["git", "check-attr", "eol", "--", relative_path],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            assert attributes.rstrip().endswith("eol: lf")


def test_installer_is_idempotent_generates_secrets_and_never_embeds_real_ones() -> None:
    installer = (DEPLOY / "install-krit-server.sh").read_text(encoding="utf-8")
    assert "set -Eeuo pipefail" in installer
    assert "id -u krit" in installer
    assert "IF NOT EXISTS" in installer
    assert "openssl rand" in installer
    assert "chmod 0640 /etc/krit-bot/krit-bot.env" in installer
    assert "chown -R root:krit /opt/krit-bot/venv" in installer
    assert "chmod -R u=rwX,g=rX,o= /opt/krit-bot/venv" in installer
    assert "/usr/local/sbin/krit-restore-dispatch" in installer
    assert "systemctl enable --now krit-bot.service" in installer
    assert "curl --fail" in installer
    assert "/var/lib/krit/.installed" in installer
    assert "178.217.99.218" not in installer
    assert "MAX_BOT_TOKEN=replace_me" not in installer


def test_installer_checks_python_313_before_server_mutations_and_preserves_env() -> None:
    installer = (DEPLOY / "install-krit-server.sh").read_text(encoding="utf-8")

    version_check = installer.index("Python 3.13")
    first_server_mutation = installer.index("useradd --system")
    assert version_check < first_server_mutation
    assert "PYTHON_BIN" in installer
    assert "EXISTING_ENV=/etc/krit-bot/krit-bot.env" in installer
    assert "if [[ -f ${EXISTING_ENV} ]]" in installer
    assert "DB_PASSWORD=${DB_PASSWORD:-$(openssl rand -hex 24)}" in installer
    assert "JWT_SECRET=${JWT_SECRET:-$(openssl rand -hex 32)}" in installer
    assert "WEBHOOK_SECRET=${MAX_WEBHOOK_SECRET:-$(openssl rand -hex 32)}" in installer
    assert "MAX_BOT_TOKEN=${MAX_BOT_TOKEN}" in installer


def test_nginx_limits_large_uploads_to_restore_content_route() -> None:
    nginx = (DEPLOY / "nginx-krit.conf").read_text(encoding="utf-8")

    assert "client_max_body_size 2m;" in nginx
    assert "location ~ ^/krit-api/api/v1/administration/restores/" in nginx
    restore_location = nginx.split(
        "location ~ ^/krit-api/api/v1/administration/restores/", 1
    )[1].split("location /krit-api/", 1)[0]
    assert "client_max_body_size 20g;" in restore_location
    assert "proxy_request_buffering off;" in restore_location


def test_server_package_builder_and_beginner_restore_guide_cover_required_assets() -> None:
    builder = (DEPLOY / "build-server-package.ps1").read_text(encoding="utf-8")
    guide = (ROOT / "RESTORE_DATABASES.txt").read_text(encoding="utf-8")
    for entry in (
        "bot/src",
        "bot/alembic",
        "bot/alembic.ini",
        "bot/pyproject.toml",
        "deploy/install-krit-server.sh",
        "deploy/krit-bot.service",
        "deploy/krit-restore@.service",
        "deploy/krit-restore-rollback.service",
        "deploy/krit-restore-delete.service",
        "deploy/krit-restore.sudoers",
        "deploy/krit_restore_dispatch.py",
        "deploy/krit_restore_helper.py",
        "deploy/nginx-krit.conf",
        ".env.example",
        "README.md",
        "RESTORE_DATABASES.txt",
    ):
        assert f'"{entry}"' in builder
    assert "Get-FileHash -Algorithm SHA256" in builder
    for instruction in (
        "ssh root@SERVER_IP",
        "scp KRiTServer.tar.gz",
        "bash deploy/install-krit-server.sh",
        "Choose_Goose",
        "Администрирование",
        "KRiT-latest-trusted.json",
        "pg_restore",
    ):
        assert instruction in guide
