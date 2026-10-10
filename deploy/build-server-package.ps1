param(
    [string]$Output = "dist/KRiTServer.tar.gz"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$OutputPath = Join-Path $Root $Output
$OutputDirectory = Split-Path -Parent $OutputPath
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$StageRoot = Join-Path ([IO.Path]::GetTempPath()) ("krit-server-" + [guid]::NewGuid().ToString("N"))

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
    "deploy/nginx-krit-api.conf",
    "deploy/krit_nginx_configure.py",
    ".env.example",
    "README.md",
    "RESTORE_DATABASES.txt"
)

function Normalize-LinuxTextFiles([string]$Path) {
    $linuxSuffixes = @(".sh", ".service", ".sudoers", ".conf")
    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    Get-ChildItem -LiteralPath $Path -Recurse -File | Where-Object {
        $linuxSuffixes -contains $_.Extension
    } | ForEach-Object {
        $content = [IO.File]::ReadAllText($_.FullName)
        $normalized = [Text.RegularExpressions.Regex]::Replace($content, "`r`n?", "`n")
        [IO.File]::WriteAllText($_.FullName, $normalized, $utf8WithoutBom)
    }
}

New-Item -ItemType Directory -Force -Path $StageRoot | Out-Null
try {
    foreach ($entry in $Entries) {
        $source = Join-Path $Root $entry
        $destination = Join-Path $StageRoot $entry
        $parent = Split-Path -Parent $destination
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        Copy-Item -LiteralPath $source -Destination $destination -Recurse -Force
    }
    Normalize-LinuxTextFiles $StageRoot

    Push-Location $StageRoot
    try {
        tar --exclude='*/__pycache__' --exclude='*.pyc' -czf $OutputPath @Entries
    }
    finally {
        Pop-Location
    }
    $Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $OutputPath).Hash.ToLowerInvariant()
    Set-Content -LiteralPath "$OutputPath.sha256" -Value "$Hash  $(Split-Path -Leaf $OutputPath)" -Encoding ascii
}
finally {
    Remove-Item -LiteralPath $StageRoot -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "Создан серверный комплект: $OutputPath"
