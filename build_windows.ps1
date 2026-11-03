$ErrorActionPreference = "Stop"

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    throw "Environnement virtuel introuvable. Creez-le avec : python -m venv .venv"
}

& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    throw "Installation des dependances echouee."
}

& .\.venv\Scripts\python.exe -c "from PIL import Image; Image.open(r'app\static\favicon_ffc.png').save(r'app\static\favicon_ffc.ico', sizes=[(16, 16), (32, 32), (48, 48), (96, 96)])"
if ($LASTEXITCODE -ne 0) {
    throw "Conversion du favicon en icone Windows echouee."
}

& .\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm chronometrage.spec
if ($LASTEXITCODE -ne 0) {
    throw "Construction de l'application echouee."
}

Write-Host "Application construite dans .\dist\ChronometrageCycliste.exe"
