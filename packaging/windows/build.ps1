# Dorie Play for the Microsoft Store: dist\DoriePlay-<version>.msix (unsigned: the Store signs it after review).
# Run from the project folder on Windows, with Python 3.14 (requirements-window.txt + pyinstaller) and the
# Windows SDK (makeappx.exe):
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..\..')   # the project folder, wherever it is started from
$env:PYTHONUTF8 = '1'
python -m PyInstaller packaging\dorieplay.spec --noconfirm --distpath dist --workpath build
if ($LASTEXITCODE) { exit $LASTEXITCODE }
$version = python -c "import version; print(version.VERSION)"
$root = 'build\msix'
if (Test-Path $root) { Remove-Item $root -Recurse -Force }
New-Item -ItemType Directory $root | Out-Null
Copy-Item dist\DoriePlay "$root\DoriePlay" -Recurse
Copy-Item packaging\windows\Assets "$root\Assets" -Recurse
Copy-Item packaging\windows\AppxManifest.xml $root
$makeappx = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\makeappx.exe" |
    Sort-Object { [version]$_.Directory.Parent.Name } | Select-Object -Last 1
if (-not $makeappx) { throw 'makeappx.exe not found: install the Windows SDK' }
& $makeappx.FullName pack /d $root /p "dist\DoriePlay-$version.msix" /o
if ($LASTEXITCODE) { exit $LASTEXITCODE }
Write-Output "dist\DoriePlay-$version.msix"
