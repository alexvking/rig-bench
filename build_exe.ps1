$ErrorActionPreference = "Stop"

Write-Host "Building standalone RigBench.exe with PyInstaller..." -ForegroundColor Cyan
uv run --python 3.12 --with pyinstaller pyinstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "RigBench" `
    --add-data "docs;docs" `
    --collect-all duckdb `
    --collect-all rich `
    gui.py

Write-Host "Built dist\RigBench.exe cleanly!" -ForegroundColor Green
Get-Item "dist\RigBench.exe" | Select-Object Name, Length, LastWriteTime
