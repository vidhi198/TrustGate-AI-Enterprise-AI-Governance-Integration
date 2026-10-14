# TrustGate AI — Enterprise AI Governance & Integration Platform (Prototype)

A locally run, standalone governance gateway that sits between users, an AI
assistant, and enterprise tools. Python · FastAPI · Streamlit · SQLite.
No external organisation or service is required; mock AI mode is the default.

## What it demonstrates
Role-based access · prompt-injection heuristic · PII redaction · allowlisted
tools · human approval for (simulated) remediation · rate limiting · token and
cost estimates with a daily budget · audit log · metrics dashboard · request-ID
correlation · structured policy decisions and error envelopes.

## Quick start

### Windows (PowerShell) — run each line separately
```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass   # only if activation is blocked
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```
Then start the backend in this terminal:
```powershell
uvicorn app.main:app --reload --port 8000
```
Open a **second** terminal in the same folder:
```powershell
.\.venv\Scripts\Activate.ps1
streamlit run gui/dashboard.py
```
Shortcut: double-click or run `run_windows.bat` from Command Prompt.

### How to run it in VS Code on Windows
Terminal 1-
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.main:app --reload --port 8000

Terminal 2-
.\.venv\Scripts\Activate.ps1
python -m streamlit run gui/dashboard.py 


### macOS / Linux
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000     # terminal 1
streamlit run gui/dashboard.py                # terminal 2
```
Or `./run_local.sh`, or `docker compose up --build`.

- API docs: http://localhost:8000/docs
- Dashboard: http://localhost:8501
- Note: `&&` is not valid in Windows PowerShell 5.x; run commands on separate lines.

## Demo identities (public demo keys, NOT production secrets)
| Role | X-API-Key | Can |
|---|---|---|
| Employee | `tg-employee-demo` | chat, read incidents/services, discover tools |
| Support | `tg-support-demo` | + create pending remediation requests |
| Admin | `tg-admin-demo` | + approvals, audit, metrics |

## Tests
```bash
pytest -q
python scripts/run_eval.py
```

## Honest limitations
Heuristic injection detection and regex redaction are illustrative; SQLite
audit is not tamper-proof; rate limiting is process-local; costs are estimates
and the budget check is not atomic; `/mcp/*` is MCP-style REST, not the real
protocol; demo keys are not production identity; approval changes a record only
and never executes anything. See `docs/` for the threat model and roadmap.
