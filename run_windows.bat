@echo off
if not exist .venv py -m venv .venv
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
if not exist .env copy .env.example .env
start "TrustGate API" cmd /k "call .venv\Scripts\activate.bat && uvicorn app.main:app --port 8000"
timeout /t 3 >nul
streamlit run gui\dashboard.py
