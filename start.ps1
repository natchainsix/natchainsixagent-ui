$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (!(Test-Path '.venv/Scripts/python.exe')) { throw 'Create the virtual environment and install requirements first.' }
if (!(Test-Path '.env')) { Copy-Item .env.example .env }
& .\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
exit $LASTEXITCODE
