param(
    [string]$Output = "dist/KRiTServer.tar.gz"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$OutputPath = Join-Path $Root $Output
$OutputDirectory = Split-Path -Parent $OutputPath
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

$Entries = @(
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
    "RESTORE_DATABASES.txt"
)

Push-Location $Root
try {
    tar --exclude='*/__pycache__' --exclude='*.pyc' -czf $OutputPath @Entries
    $Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $OutputPath).Hash.ToLowerInvariant()
    Set-Content -LiteralPath "$OutputPath.sha256" -Value "$Hash  $(Split-Path -Leaf $OutputPath)" -Encoding ascii
}
finally {
    Pop-Location
}

Write-Host "Создан серверный комплект: $OutputPath"
