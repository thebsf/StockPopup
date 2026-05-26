$ErrorActionPreference = "Stop"

Write-Host "Installing PyInstaller..."
python -m pip install pyinstaller

Write-Host "Building single-file Windows exe..."
python -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --windowed `
  --name "桌面实时行情" `
  desktop_ticker.py

Write-Host ""
Write-Host "Done. Use dist\桌面实时行情.exe"
