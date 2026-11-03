$ErrorActionPreference = "Stop"

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    throw "Virtual environment not found. Create it with: python -m venv .venv"
}

& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed."
}

& .\.venv\Scripts\python.exe -c "from PIL import Image; Image.open(r'app\static\favicon_ffc.png').save(r'app\static\favicon_ffc.ico', sizes=[(16, 16), (32, 32), (48, 48), (96, 96)])"
if ($LASTEXITCODE -ne 0) {
    throw "Favicon conversion to a Windows icon failed."
}

& .\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm chronometrage.spec
if ($LASTEXITCODE -ne 0) {
    throw "Application build failed."
}

Write-Host "Application built at .\dist\ChronometrageCycliste.exe"
