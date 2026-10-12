# Builds dist\CryRec.exe, dist\CryRec-<ver>-portable.zip and dist\CryRec-<ver>-setup.exe
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$ver = (Select-String -Path "$root\cryrec.pyw" -Pattern '^VERSION = "(.+)"').Matches[0].Groups[1].Value
$work = Join-Path $env:TEMP 'cryrec-build'
$dist = Join-Path $root 'dist'

python -m PyInstaller --noconfirm --clean --onefile --windowed --name CryRec `
    --icon "$root\cryrec.ico" --add-data "$root\cryrec.ico;." `
    --distpath $dist --workpath $work --specpath $work "$root\cryrec.pyw"
if ($LASTEXITCODE) { throw 'PyInstaller failed' }

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
$iscc = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
$env:CRYREC_VERSION = $ver
& $iscc /Q "$PSScriptRoot\cryrec.iss"
if ($LASTEXITCODE) { throw 'Inno Setup failed' }

Get-ChildItem $dist | Select-Object Name, @{n='MB'; e={[math]::Round($_.Length / 1MB, 1)}}
