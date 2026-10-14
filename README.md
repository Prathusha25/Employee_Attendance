# Employee Attendance & Analytics API

FastAPI + MongoDB backend for the HROne attendance assignment: employees, punch-in/out, manual corrections with
an audit trail, four analytics endpoints built as MongoDB aggregation pipelines, and an explain endpoint.
All application code is in `app/main.py`.

## Requirements
* Python 3.11+
* MongoDB 6.0+ (developed for 7.0). Local example: `docker run -d -p 27017:27017 mongo:7`

## Configuration
| Variable | Meaning | Local default |
|---|---|---|
| `MONGO_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGO_DB` | Database name | `attendance_db` |

A local `.env` file is read for convenience, but real environment variables always take priority.
Never commit `.env`. Indexes are created automatically at start-up (idempotent).

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
Interactive API docs: http://localhost:8000/docs (OpenAPI JSON at `/openapi.json`).

## Tests
```bash
pip install -r requirements-dev.txt
pytest -v                                                    # unit + validation tests, no database needed
MONGO_TEST_URI="mongodb://localhost:27017" pytest -v         # also runs the MongoDB integration tests
```
Integration tests create a throw-away database named `hrone_test_<random>` and drop it afterwards; they never use
`MONGO_DB`. Without `MONGO_TEST_URI` they are reported as skipped.

## Notes and interpretations
* Times in the API are epoch milliseconds; MongoDB stores BSON dates (UTC). Punch instants are truncated to whole
  seconds before any calculation (R1).
* Trend: "range exceeds 92 days" is read as more than 92 calendar days inclusive (a full quarter is allowed).
  `moving_avg_7d` averages the reported (4-decimal) `attendance_rate` values.
* PATCH uses optimistic concurrency: if another correction changed the record first, the later one gets 409
  instead of overwriting it, so no history entry is lost.
* Rounding is half-up everywhere (Python `Decimal`, and decimal arithmetic inside MongoDB for the trend), never
  `round()` or `$round` (both round half to even).
