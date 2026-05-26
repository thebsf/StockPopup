$ErrorActionPreference = "Stop"

Write-Host "Installing dependencies..."
npm install

Write-Host "Building portable Windows app..."
npm run dist

Write-Host ""
Write-Host "Done. The portable exe is in the release directory."
