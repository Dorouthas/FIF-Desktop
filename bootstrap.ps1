param(
    [ValidateSet('web', 'desktop')][string]$Mode = 'web',
    [switch]$NoLaunch
)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$appPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$probe = if ($Mode -eq 'web') { 'import fif_oral,PySide6,cryptography,oss2,num2words,websocket,fastapi,uvicorn,multipart,edge_tts' } else { 'import fif_oral,PySide6,cryptography,oss2,num2words,websocket,edge_tts' }
$ready = $false
if (Test-Path -LiteralPath $appPython) {
    try {
        & $appPython -c $probe *> $null
        $ready = $LASTEXITCODE -eq 0
    } catch { $ready = $false }
}
if (-not $ready) {
    if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { throw 'Automatic setup currently supports Windows x64.' }
    if (Test-Path -LiteralPath (Join-Path $projectRoot 'data\accounts.lock')) {
        throw 'Close the running FIF Oral client before installing application dependencies.'
    }
    $manifest = Get-Content -LiteralPath (Join-Path $projectRoot 'src\fif_oral\resources\dependency-manifest.json') -Raw | ConvertFrom-Json
    $uvCommand = Get-Command uv.exe -ErrorAction SilentlyContinue
    if ($uvCommand) { $uvPath = $uvCommand.Source }
    else {
        $uvDirectory = Join-Path $projectRoot ('data\tools\uv\' + $manifest.uv.version)
        $uvPath = Join-Path $uvDirectory 'uv.exe'
        if (-not (Test-Path -LiteralPath $uvPath)) {
            $downloadDirectory = Join-Path $projectRoot 'data\downloads'
            New-Item -ItemType Directory -Path $downloadDirectory -Force | Out-Null
            $archive = Join-Path $downloadDirectory 'bootstrap-uv.zip'
            if (-not (Test-Path -LiteralPath $archive) -or (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest.uv.sha256) {
                Write-Host 'Downloading the pinned uv package from Astral GitHub Releases...'
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                Invoke-WebRequest -Uri $manifest.uv.url -OutFile $archive -UseBasicParsing
            }
            if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest.uv.sha256) {
                throw 'uv checksum verification failed. The archive was not executed.'
            }
            New-Item -ItemType Directory -Path $uvDirectory -Force | Out-Null
            Expand-Archive -LiteralPath $archive -DestinationPath $uvDirectory -Force
            if (-not (Test-Path -LiteralPath $uvPath)) {
                $found = Get-ChildItem -LiteralPath $uvDirectory -Filter uv.exe -Recurse | Select-Object -First 1
                if (-not $found) { throw 'uv.exe is missing from the verified archive.' }
                $uvPath = $found.FullName
            }
        }
    }
    $env:UV_CACHE_DIR = Join-Path $projectRoot 'data\cache\uv'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $projectRoot 'data\runtime\python'
    $env:UV_PYTHON_BIN_DIR = Join-Path $projectRoot 'data\runtime\bin'
    $env:UV_NO_MODIFY_PATH = '1'
    Write-Host 'Preparing local Python and locked application package...'
    $syncArguments = @('sync', '--frozen', '--project', $projectRoot, '--managed-python', '--python', '3.11', '--no-dev')
    if ($Mode -eq 'web') { $syncArguments += @('--extra', 'web') }
    & $uvPath @syncArguments
    if ($LASTEXITCODE -ne 0) { throw 'Application installation failed. Re-run setup to reuse the cache.' }
    & $appPython -c $probe
    if ($LASTEXITCODE -ne 0) { throw 'Application imports failed after installation.' }
}
Write-Host 'Application environment is ready.'
Write-Host 'Use Environment & Downloads inside FIF Oral to prepare FFmpeg, TTS and optional models.'
if (-not $NoLaunch) {
    $entry = if ($Mode -eq 'web') { 'fif_oral.web' } else { 'fif_oral' }
    & $appPython -m $entry
    exit $LASTEXITCODE
}
