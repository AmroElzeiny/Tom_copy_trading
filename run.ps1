$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path ".venv")) {
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3.11 -m venv .venv } else { python -m venv .venv }
}
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .
python -m tg_mt5_copytrader @args
