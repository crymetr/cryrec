# Builds dist\CryRec.exe, dist\CryRec-<ver>-portable.zip and dist\CryRec-<ver>-setup.exe
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$ver = (Select-String -Path "$root\cryrec.pyw" -Pattern '^VERSION = "(.+)"').Matches[0].Groups[1].Value
$work = Join-Path $env:TEMP 'cryrec-build'
$dist = Join-Path $root 'dist'

$common = @('--noconfirm', '--clean', '--windowed', '--name', 'CryRec',
    '--icon', "$root\cryrec.ico", '--add-data', "$root\cryrec.ico;.", '--paths', $root,
    '--collect-data', 'faster_whisper', '--collect-binaries', 'ctranslate2', '--collect-binaries', 'onnxruntime', '--hidden-import', 'pystray._win32', '--collect-data', 'sv_ttk',
    '--workpath', $work, '--specpath', $work, "$root\cryrec.pyw")
# Folder build for the installer: starts fast, nothing to unpack on launch
python -m PyInstaller @common --onedir --distpath "$work\onedir"
if ($LASTEXITCODE) { throw 'PyInstaller (onedir) failed' }
# Single exe for portable: one file to carry and to swap on update
python -m PyInstaller @common --onefile --distpath $dist
if ($LASTEXITCODE) { throw 'PyInstaller (onefile) failed' }

# Portable: exe + "portable" marker, settings and recordings stay next to the exe
$stage = Join-Path $work 'portable\CryRec'
Remove-Item (Split-Path $stage) -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $stage | Out-Null
Copy-Item "$dist\CryRec.exe" $stage
Set-Content "$stage\portable" 'Settings and recordings are stored in this folder. Delete this file to use %APPDATA% and Documents instead.'
$zip = "$dist\CryRec-$ver-portable.zip"
Remove-Item $zip -ErrorAction SilentlyContinue
Compress-Archive -Path $stage -DestinationPath $zip

# Installer
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw 'Inno Setup 6 not found' }
$env:CRYREC_VERSION = $ver
$env:CRYREC_APPDIR = "$work\onedir\CryRec"
& $iscc /Q "$PSScriptRoot\cryrec.iss"
if ($LASTEXITCODE) { throw 'Inno Setup failed' }

Get-ChildItem $dist | Select-Object Name, @{n='MB'; e={[math]::Round($_.Length / 1MB, 1)}}
