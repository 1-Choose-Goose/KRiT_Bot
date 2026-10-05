from __future__ import annotations

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

    assert "User=krit" in bot_service
    assert "Restart=always" in bot_service
    assert "127.0.0.1:8080" in nginx
    assert "proxy_request_buffering off" in nginx
    assert "User=root" in restore_service
    assert "ExecStopPost=/usr/bin/systemctl start krit-bot.service" in restore_service
    assert "--rollback" in rollback_service
    assert "--delete-safety" in delete_service
    assert "NOPASSWD:" in sudoers
    assert "krit-restore@" in sudoers
    assert "ALL=(ALL) ALL" not in sudoers


def test_installer_is_idempotent_generates_secrets_and_never_embeds_real_ones() -> None:
    installer = (DEPLOY / "install-krit-server.sh").read_text(encoding="utf-8")
    assert "set -Eeuo pipefail" in installer
    assert "id -u krit" in installer
    assert "IF NOT EXISTS" in installer
    assert "openssl rand" in installer
    assert "chmod 0640 /etc/krit-bot/krit-bot.env" in installer
    assert "systemctl enable --now krit-bot.service" in installer
    assert "curl --fail" in installer
    assert "/var/lib/krit/.installed" in installer
    assert "178.217.99.218" not in installer
    assert "MAX_BOT_TOKEN=replace_me" not in installer


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
