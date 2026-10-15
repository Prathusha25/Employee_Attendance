# Employee Attendance & Analytics API

A FastAPI + MongoDB REST API built for the HROne backend assignment.

Covers the full lifecycle: employee management, daily punch-in / punch-out,
manual attendance corrections with a full audit trail, four analytics
aggregation endpoints, and an `/admin/explain` endpoint that exposes the
MongoDB query plan for any supported route.

All application code lives in a single file: [`app/main.py`](app/main.py).

---

## Features

| Area | What is implemented |
|---|---|
| **Employees** | Create, list (paginated, filterable by department) |
| **Attendance** | Punch-in, punch-out, list (paginated, filterable by date range / status) |
| **Corrections** | `PATCH` with optimistic concurrency and append-only audit history |
| **Analytics** | Monthly summary, late-minutes leaderboard, headcount, 92-day trend |
| **Ops** | `/health` (real MongoDB ping), `/admin/explain`, `503` on DB failure |
| **Data integrity** | Unique indexes prevent duplicate employees and duplicate daily punches |

---

## Requirements

- Python 3.11 or later
- MongoDB 6.0 or later (developed and tested against 7.0)

**Quick way to start MongoDB locally with Docker:**

```bash
docker run -d -p 27017:27017 mongo:7
```

---

## Configuration

The app reads two environment variables (a local `.env` file is also
supported for convenience; real environment variables always win):

| Variable | Meaning | Local default |
|---|---|---|
| `MONGO_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGO_DB` | Database name | `attendance_db` |

> **Never commit a `.env` file.** It is already listed in `.gitignore`.

Indexes are created automatically at start-up (the call is idempotent —
safe to restart as many times as you like).

---

## Running the API

### Linux / macOS

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"

uvicorn app.main:app --port 8000 --reload
```

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB  = "attendance_db"

uvicorn app.main:app --port 8000 --reload
```

Interactive API docs are available at **http://localhost:8000/docs**
(OpenAPI JSON at `/openapi.json`).

---

## Running the Tests

```bash
pip install -r requirements-dev.txt
```

### Unit & HTTP-validation tests (no database needed)

```bash
pytest -v tests/test_rules.py tests/test_http_validation.py
```

These 30 tests run against an in-process FastAPI `TestClient` with no real
MongoDB. They check business-rule arithmetic (late minutes, overtime, rounding)
and that invalid HTTP inputs return `422`.

### MongoDB integration tests (require a live database)

```bash
# Linux / macOS
MONGO_TEST_URI="mongodb://localhost:27017" pytest -v tests/test_integration.py

# Windows PowerShell
$env:MONGO_TEST_URI = "mongodb://localhost:27017"
pytest -v tests/test_integration.py
```

The 40 integration tests create a throw-away database called
`hrone_test_<random>` and drop it afterwards — they **never touch `MONGO_DB`**.
Without `MONGO_TEST_URI` the entire module is reported as **skipped**, not
passed.

### Run all tests at once

```bash
# Linux / macOS
MONGO_TEST_URI="mongodb://localhost:27017" pytest -v

# Windows PowerShell
$env:MONGO_TEST_URI = "mongodb://localhost:27017"
pytest -v
```

**Test breakdown:** 16 rule-unit tests · 14 HTTP-validation tests · 40 MongoDB
integration tests = **70 tests total**.

---

## Project Layout

```
app/
  __init__.py
  main.py          ← all application code (~950 lines)
tests/
  conftest.py      ← sets env vars before app import; skip logic
  helpers.py       ← shared IST timestamp helpers
  test_rules.py    ← pure unit tests: R1-R5 arithmetic
  test_http_validation.py  ← 422 / 404 edge-case tests
  test_integration.py      ← end-to-end tests against real MongoDB
requirements.txt
requirements-dev.txt
REVIEW.md          ← 19 bugs found in starter code, with fixes
DECISIONS.md       ← 5 design decisions (indexes, races, ties, …)
```

---

## Design notes

- **Times** — the API accepts and returns epoch milliseconds. MongoDB stores
  BSON dates in UTC. Punch instants are truncated to whole seconds before any
  calculation (rule R1).
- **IST date** — the attendance date of a punch is the IST calendar date of
  the truncated punch instant, accounting for overnight shifts (rule R1).
- **Rounding** — half-up everywhere, using Python `Decimal` and MongoDB
  `$multiply`/`$divide` arithmetic — never `round()` or `$round`, which both
  round half-to-even.
- **Trend range** — "range exceeds 92 days" means more than 92 calendar days
  inclusive (a full quarter is allowed). `moving_avg_7d` averages the
  reported (4-decimal) `attendance_rate` values.
- **Concurrency** — duplicate employees and duplicate daily punches are
  prevented by unique indexes; `DuplicateKeyError` maps to `409`. The
  `PATCH` correction endpoint uses optimistic concurrency (`409` instead of
  a silent overwrite).
- **Ties** — the leaderboard uses `$rank`, which produces gaps (1, 2, 2, 4).
  Everyone tied at the cutoff is returned even if that exceeds the requested
  limit.
