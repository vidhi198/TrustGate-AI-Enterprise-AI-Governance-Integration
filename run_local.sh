#!/usr/bin/env bash
set -e
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
uvicorn app.main:app --port 8000 &
API_PID=$!
trap "kill $API_PID" EXIT
sleep 2
streamlit run gui/dashboard.py
