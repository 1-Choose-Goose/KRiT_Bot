[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$managementRoot = $PSScriptRoot
$versionFile = Join-Path $managementRoot "src\krit_management\version.py"
$versionMatch = Select-String -LiteralPath $versionFile -Pattern 'APP_VERSION\s*=\s*"([^"]+)"'
if (-not $versionMatch) { throw "Не удалось прочитать версию" }
$appVersion = $versionMatch.Matches[0].Groups[1].Value

$pythonLauncher = Get-Command py.exe -ErrorAction Stop
$pythonHome = (& $pythonLauncher.Source -3.13 -c "import sys; print(sys.base_prefix)").Trim()
$buildRoot = Join-Path $env:TEMP "krit-management-build-$appVersion"
$releaseDir = Join-Path $projectRoot "release\v$appVersion"
$stagingDir = Join-Path $buildRoot "dist\KRiTManagement"
$savedPath = $env:PATH
$process = $null

if (Test-Path -LiteralPath $buildRoot) { [IO.Directory]::Delete($buildRoot, $true) }
[IO.Directory]::CreateDirectory($releaseDir) | Out-Null

try {
    & $pythonLauncher.Source -3.13 -m venv (Join-Path $buildRoot "venv")
    $python = Join-Path $buildRoot "venv\Scripts\python.exe"
    & $python -m pip install --disable-pip-version-check --quiet --upgrade pip
    & $python -m pip install --disable-pip-version-check --quiet -e $managementRoot "pyinstaller==6.22.3"

    # Изолируем PyInstaller от DLL из Codex, Poppler, Git и других SDK.
    $env:PATH = @(
        (Join-Path $buildRoot "venv\Scripts"),
        $pythonHome,
        (Join-Path $pythonHome "Scripts"),
        (Join-Path $env:SystemRoot "System32"),
        $env:SystemRoot
    ) -join ";"

    $distDir = Join-Path $buildRoot "dist"
    $workDir = Join-Path $buildRoot "work"
    $specDir = Join-Path $buildRoot "spec"
    $sourceDir = Join-Path $managementRoot "src"
    $assets = Join-Path $sourceDir "krit_management\assets"

    & $python -m PyInstaller --noconfirm --clean --onefile --windowed `
        --name KRiTManagementUpdater --icon (Join-Path $assets "app_icon.ico") `
        --add-data "$assets\app_icon.ico;krit_management\assets" `
        --distpath $distDir `
        --workpath (Join-Path $workDir "updater") --specpath $specDir `
        --paths $sourceDir (Join-Path $sourceDir "krit_management\updater.py")
    if ($LASTEXITCODE -ne 0) { throw "Не собран updater" }

    & $python -m PyInstaller --noconfirm --clean --onedir --windowed `
        --name KRiTManagement --icon (Join-Path $assets "app_icon.ico") `
        --add-data "$assets;krit_management\assets" --hidden-import krit_management.main `
        --distpath $distDir --workpath (Join-Path $workDir "application") `
        --specpath $specDir --paths $sourceDir (Join-Path $managementRoot "main.py")
    if ($LASTEXITCODE -ne 0) { throw "Не собрана программа" }

    Copy-Item -LiteralPath (Join-Path $distDir "KRiTManagementUpdater.exe") `
        -Destination (Join-Path $stagingDir "_internal\KRiTManagementUpdater.exe") -Force

    $foreignIcu = Get-ChildItem -LiteralPath (Join-Path $stagingDir "_internal") `
        -Filter "icu*.dll" -File -Recurse -ErrorAction SilentlyContinue
    if ($foreignIcu) {
        throw "В сборку попала чужая ICU DLL: $($foreignIcu.FullName -join ', ')"
    }
    $analysisText = Get-Content -LiteralPath `
        (Join-Path $workDir "application\KRiTManagement\Analysis-00.toc") -Raw
    if ($analysisText -match "codex-runtimes|poppler") {
        throw "В сборку попала DLL из посторонней среды"
    }

    $process = Start-Process -FilePath (Join-Path $stagingDir "KRiTManagement.exe") `
        -WorkingDirectory $stagingDir -WindowStyle Hidden -PassThru
    Start-Sleep -Seconds 5
    $running = Get-Process -Id $process.Id -ErrorAction SilentlyContinue
    if (-not $running) { throw "Программа завершилась до проверки окна" }
    Stop-Process -Id $running.Id -Force
    $running.WaitForExit(10000) | Out-Null
    $process = $null

    $zipPath = Join-Path $releaseDir "KRiT-Management-Windows-x64.zip"
    if (Test-Path -LiteralPath $zipPath) { [IO.File]::Delete($zipPath) }
    Compress-Archive -LiteralPath $stagingDir -DestinationPath $zipPath -CompressionLevel Optimal

    $iscc = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
    if (-not (Test-Path -LiteralPath $iscc)) { throw "Не установлен Inno Setup 6" }
    & $iscc "/DAppVersion=$appVersion" "/DSourceDir=$stagingDir" `
        "/DOutputDir=$releaseDir" (Join-Path $managementRoot "packaging\windows-installer.iss")
    if ($LASTEXITCODE -ne 0) { throw "Не собран Setup" }

    Get-ChildItem -LiteralPath $releaseDir -File |
        Where-Object Name -in @(
            "KRiT-Management-Windows-x64.zip",
            "SetupKrit.exe"
        ) | ForEach-Object {
            [pscustomobject]@{ Name = $_.Name; Size = $_.Length; SHA256 = (Get-FileHash $_).Hash }
        }
}
finally {
    if ($process) {
        $running = Get-Process -Id $process.Id -ErrorAction SilentlyContinue
        if ($running) {
            Stop-Process -Id $running.Id -Force
            $running.WaitForExit(10000) | Out-Null
        }
    }
    $env:PATH = $savedPath
    if (Test-Path -LiteralPath $buildRoot) { [IO.Directory]::Delete($buildRoot, $true) }
}
