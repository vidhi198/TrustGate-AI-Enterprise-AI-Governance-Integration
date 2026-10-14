import os
import tempfile

# Must be set before app modules are imported.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_PATH"] = _tmp.name
os.environ.pop("TG_API_KEYS", None)
os.environ["LLM_PROVIDER"] = "mock"
os.environ["RATE_LIMIT_REQUESTS"] = "1000"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.limits import rate_limiter  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    rate_limiter.reset()
    with TestClient(app) as c:
        yield c


EMP = {"X-API-Key": "tg-employee-demo"}
SUP = {"X-API-Key": "tg-support-demo"}
ADM = {"X-API-Key": "tg-admin-demo"}
