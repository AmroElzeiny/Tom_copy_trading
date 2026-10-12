@echo off
cd /d "%~dp0"
if not exist .venv (
  where py >nul 2>nul && (py -3.11 -m venv .venv) || (python -m venv .venv)
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -e .
python -m tg_mt5_copytrader %*
