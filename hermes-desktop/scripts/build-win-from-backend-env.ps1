param(
  [string]$BackendEnv = "..\backend\.env",
  [string]$UpdateUrl = $env:COWORK_DESKTOP_UPDATE_URL,
  [switch]$Unpacked,
  [switch]$Release
)

$ErrorActionPreference = "Stop"
# Default binary mirrors to npmmirror: direct GitHub downloads time out on
# the intranet and break packaging even when everything is already compiled.
if ([string]::IsNullOrWhiteSpace($env:ELECTRON_MIRROR)) {
  $env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
}
if ([string]::IsNullOrWhiteSpace($env:ELECTRON_BUILDER_BINARIES_MIRROR)) {
  $env:ELECTRON_BUILDER_BINARIES_MIRROR = "https://npmmirror.com/mirrors/electron-builder-binaries/"
}
$cachePath = Join-Path $PSScriptRoot "..\.electron-builder-cache"
if ([string]::IsNullOrWhiteSpace($env:ELECTRON_BUILDER_CACHE)) {
  New-Item -ItemType Directory -Force -Path $cachePath | Out-Null
  $env:ELECTRON_BUILDER_CACHE = (Resolve-Path $cachePath).Path
}
$envPath = (Resolve-Path $BackendEnv).Path
$settings = @{}
foreach ($line in Get-Content -LiteralPath $envPath) {
  if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
    $name = $Matches[1]
    if ($name -notin @("VITE_LEGACY_SSO_LOGIN_BASE_URL", "VITE_LEGACY_SSO_APP_NAME", "PUBLIC_WEB_BASE")) { continue }
    $value = $Matches[2].Trim()
    if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
      $value = $value.Substring(1, $value.Length - 2)
    }
    $settings[$name] = $value
  }
}

foreach ($name in @("VITE_LEGACY_SSO_LOGIN_BASE_URL", "VITE_LEGACY_SSO_APP_NAME", "PUBLIC_WEB_BASE")) {
  if ([string]::IsNullOrWhiteSpace($settings[$name])) { throw "backend/.env 缺少 $name" }
  Set-Item -Path "Env:$name" -Value $settings[$name]
}

$env:VITE_API_BASE_URL = "/api/v1"
$env:VITE_AUTH_MODE = "enterprise_idpAES"
if ([string]::IsNullOrWhiteSpace($env:COWORK_SERVER_ORIGIN)) {
  $env:COWORK_SERVER_ORIGIN = $settings["PUBLIC_WEB_BASE"]
}
$env:COWORK_DESKTOP_UPDATE_URL = $UpdateUrl

# Stage a Windows embeddable CPython into build/python so the installer ships
# a Python for machines that have none. command.run appends its directory to
# the child PATH (never prepends), so systems with their own Python are not
# affected. The zip is cached under .electron-builder-cache/python-embed and
# the staged tree is reused across builds.
$pythonVersion = "3.11.9"
$pythonZipName = "python-$pythonVersion-embed-amd64.zip"
$pythonUrl = if ($env:COWORK_PYTHON_EMBED_URL) { $env:COWORK_PYTHON_EMBED_URL } else { "https://mirrors.huaweicloud.com/python/$pythonVersion/$pythonZipName" }
$pythonCacheDir = Join-Path $cachePath "python-embed"
$pythonZip = Join-Path $pythonCacheDir $pythonZipName
$pythonStage = Join-Path $PSScriptRoot "..\build\python"
if (-not (Test-Path (Join-Path $pythonStage "python.exe"))) {
  New-Item -ItemType Directory -Force -Path $pythonCacheDir, $pythonStage | Out-Null
  if (-not (Test-Path $pythonZip)) {
    Write-Host "downloading python embeddable: $pythonUrl"
    Invoke-WebRequest -Uri $pythonUrl -OutFile $pythonZip -UseBasicParsing
  }
  Expand-Archive -LiteralPath $pythonZip -DestinationPath $pythonStage -Force
  # The embeddable distribution comments out `import site`; enable it so the
  # interpreter behaves like a normal install (site-packages on sys.path,
  # ready for a future bundled pip).
  Get-ChildItem -LiteralPath $pythonStage -Filter "*._pth" | ForEach-Object {
    $content = Get-Content -LiteralPath $_.FullName -Raw
    Set-Content -LiteralPath $_.FullName -Value ($content -replace "(?m)^#\s*import site", "import site")
  }
  Write-Host "staged builtin python $pythonVersion -> $pythonStage"
}

# Clean dist directory to avoid EPERM on win-unpacked rename
$distPath = Join-Path $PSScriptRoot "..\dist"
if (Test-Path $distPath) {
  Write-Host "cleaning dist directory: $distPath"
  Remove-Item -Recurse -Force $distPath
}

npm.cmd run write-update-config
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
npm.cmd run build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($Unpacked) {
  npm.cmd exec electron-builder -- --dir --win --publish never
} elseif ($Release) {
  if ([string]::IsNullOrWhiteSpace($UpdateUrl)) { throw "-Release 必须提供 -UpdateUrl 或 COWORK_DESKTOP_UPDATE_URL" }
  npm.cmd exec electron-builder -- --win --publish always
} else {
  npm.cmd exec electron-builder -- --win --publish never
}
exit $LASTEXITCODE
