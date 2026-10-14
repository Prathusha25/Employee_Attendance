"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Started from a colleague's unreviewed starter; the defects found in it are listed in REVIEW.md.
Contract: openapi.yaml. Stored data: DATA_MODEL.md. Business rules are referred to as R1-R10.

Layout of this file
  1. Configuration and database connection
  2. Time helpers (epoch ms <-> datetime, IST dates, shift instants)
  3. Business-rule helpers (R1-R5, R7, R8)
  4. Request models and validation helpers
  5. Response conversion (MongoDB document -> API JSON)
  6. Indexes and app start-up
  7. Endpoints: health, employees, attendance
  8. Analytics: pipeline builders (shared with /admin/explain) and endpoints
  9. Admin explain
"""
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration and database connection
# --------------------------------------------------------------------------- #
load_dotenv(override=False)  # a local .env is a convenience; real environment variables take priority

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")  # local-dev default only, no credentials
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: PyMongo returns timezone-aware UTC datetimes instead of naive ones, so every instant we
# read can be compared with and converted like the ones we create. The short server-selection timeout keeps
# /health from hanging for 30 s when MongoDB is down.
client = MongoClient(
    MONGO_URI, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000
)
db = client[MONGO_DB]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MIN_EPOCH_MS = 100_000_000_000  # below this the value looks like seconds -> 422
MAX_EPOCH_MS = 4_102_444_800_000  # 2100-01-01

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_BELOW_HOURS = Decimal("4.50")  # R5
MAX_SHIFT = timedelta(hours=24)
MAX_TREND_DAYS = 92

DATE_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
EMP_CODE_RE = r"^EMP[0-9]{4,6}$"
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

# --------------------------------------------------------------------------- #
# 2. Time helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1).

    Integer arithmetic only: 1783312500999 ms becomes ...500 s, never rounded up by float error."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Datetime -> epoch milliseconds. A naive datetime (what PyMongo returns without tz_aware) is UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def truncate_to_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def now_utc() -> datetime:
    """Current time, truncated to milliseconds (the precision a BSON date keeps)."""
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def parse_hhmm(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(h, m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def ist_date(dt: datetime) -> date:
    """The IST calendar date of an instant."""
    return dt.astimezone(IST).date()


def ist_instant(day: date, hhmm: str) -> datetime:
    """The instant at which it is `hhmm` IST on `day`, as aware UTC."""
    return datetime.combine(day, parse_hhmm(hhmm), tzinfo=IST).astimezone(UTC)


def shift_start_instant(date_str: str, shift_start: str) -> datetime:
    return ist_instant(date.fromisoformat(date_str), shift_start)


def shift_end_instant(date_str: str, shift_start: str, shift_end: str) -> datetime:
    """R1: the end of an overnight shift falls on the next calendar day."""
    end = ist_instant(date.fromisoformat(date_str), shift_end)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return end


# --------------------------------------------------------------------------- #
# 3. Business-rule helpers
# --------------------------------------------------------------------------- #
def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1. The IST date of the punch-in, except that on an overnight shift a punch-in earlier than
    shift_end belongs to the shift that started the previous day.

    Shift 22:00-06:00: 2026-07-07 05:30 IST -> "2026-07-06"; 2026-07-06 21:55 IST -> "2026-07-06"."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < parse_hhmm(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    """R2. Late only when strictly more than 10:00 after shift start; then floor minutes since shift start.

    Shift 09:30: 09:40:00 -> 0, 09:40:01 -> 10, 10:15:59 -> 45."""
    seconds = int((truncate_to_seconds(punch_in) - shift_start_instant(date_str, shift_start)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R3. Floor minutes from shift end (next day for overnight shifts) to punch-out, only if >= 30."""
    seconds = int((truncate_to_seconds(punch_out) - shift_end_instant(date_str, shift_start, shift_end)).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def round_half_up(value: Any, places: int) -> Optional[float]:
    """R4/R8. Decimal half-up rounding (Python's round() is half-to-even and suffers float error).

    Floats go through repr() so 0.125 is the decimal 0.125, not its binary approximation."""
    if value is None:
        return None
    if isinstance(value, Decimal128):
        value = value.to_decimal()
    if not isinstance(value, Decimal):
        value = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4. Whole seconds / 3600, rounded to 2 decimals half-up. 450 s -> 0.125 h -> 0.13."""
    seconds = int((truncate_to_seconds(punch_out) - truncate_to_seconds(punch_in)).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5. Compares the *rounded* work hours: 4.495 h rounds to 4.50 and is not a half day."""
    return work_hours is not None and Decimal(repr(work_hours)) < HALF_DAY_BELOW_HOURS


def derived_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], emp: dict,
                   date_str: str) -> dict:
    """All four derived values for a record, following the table in DATA_MODEL.md section 3."""
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, emp["shift_start"], date_str)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, emp["shift_start"], emp["shift_end"], date_str),
        "half_day": compute_half_day(hours),
    }


def working_days(month_start: date, month_end: date, joined_on: str) -> int:
    """R7. Mon-Fri days in the month, counted from joined_on for a mid-month joiner (0 if not joined yet)."""
    start = max(month_start, date.fromisoformat(joined_on))
    days = 0
    d = start
    while d <= month_end:  # at most 31 iterations: calendar arithmetic, not data processing
        if d.weekday() < 5:
            days += 1
        d += timedelta(days=1)
    return days


# --------------------------------------------------------------------------- #
# 4. Request models and validation helpers
# --------------------------------------------------------------------------- #
# StrictInt rejects floats (1783312500000.0), numeric strings and booleans; ge/le reject seconds-looking values.
EpochMillis = StrictInt


def epoch_field(description: str):
    return Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS, description=description)


def invalid(loc: tuple, msg: str, value: Any = None):
    """Raise a 422 with FastAPI's default validation body ({"detail": [ {...} ]})."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": value}])


def parse_date(value: str, loc: tuple) -> date:
    if not isinstance(value, str) or not re.fullmatch(DATE_RE, value):
        invalid(loc, "must be a date in YYYY-MM-DD format", value)
    try:
        return date.fromisoformat(value)
    except ValueError:
        invalid(loc, "not a valid calendar date", value)


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = map(int, month.split("-"))
    first = date(year, mon, 1)
    nxt = date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)
    return first, nxt - timedelta(days=1)


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=EMP_CODE_RE)
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=EMAIL_RE)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_RE)
    shift_end: str = Field(default="18:30", pattern=HHMM_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_date(cls, v: str) -> str:
        if not re.fullmatch(DATE_RE, v):
            raise ValueError("must be a date in YYYY-MM-DD format")
        date.fromisoformat(v)  # raises ValueError for 2026-02-30
        return v

    @model_validator(mode="after")
    def _shift_not_empty(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: EpochMillis = epoch_field("Epoch milliseconds. Defaults to now.")
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMillis = epoch_field("Epoch milliseconds. Defaults to now.")


class RegularizeIn(BaseModel):
    """Only these fields can be changed; derived fields sent by a client are ignored."""
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] = None
    punch_in: EpochMillis = epoch_field("New punch-in, epoch milliseconds.")
    punch_out: EpochMillis = epoch_field("New punch-out, epoch milliseconds.")
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def instant_or_now(ms: Optional[int]) -> datetime:
    return ms_to_dt(ms) if ms is not None else truncate_to_seconds(datetime.now(UTC))


# --------------------------------------------------------------------------- #
# 5. Response conversion
# --------------------------------------------------------------------------- #
TIME_FIELDS = ("punch_in", "punch_out")


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def history_out(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        if field in TIME_FIELDS:
            changes[field] = {"from": dt_to_ms(change.get("from")), "to": dt_to_ms(change.get("to"))}
        else:
            changes[field] = {"from": change.get("from"), "to": change.get("to")}
    return {"at": dt_to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    """Stored record -> API record. Missing legacy fields get their documented defaults; no _id/id."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day") or False),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


def plain_number(value: Any) -> Any:
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    return value


# --------------------------------------------------------------------------- #
# 6. Indexes and start-up
# --------------------------------------------------------------------------- #
INDEXES = {
    "employees": [
        ([("emp_code", ASCENDING)], {"unique": True}),  # identity + race-safe 409 on create; leaderboard $lookup
        ([("department", ASCENDING), ("emp_code", ASCENDING)], {}),  # GET /employees?department= sorted by emp_code
        ([("joined_on", ASCENDING)], {}),  # department summary: joined_on <= month end (R9)
        ([("department", ASCENDING), ("joined_on", ASCENDING)], {}),  # summary with department filter; trend
    ],
    "attendance_logs": [
        # one record per (emp_code, date): makes punch-in race-safe; serves PATCH, monthly and $lookups by emp_code
        ([("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True}),
        # GET /attendance default sort; month range for leaderboard
        ([("date", DESCENDING), ("emp_code", ASCENDING)], {}),
        # GET /attendance?status=... with the same sort
        ([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {}),
        # punch-out: latest record whose punch_in <= punched_at
        ([("emp_code", ASCENDING), ("punch_in", DESCENDING)], {}),
    ],
}

_indexes_ready = threading.Event()


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when an identical index already exists."""
    for coll, specs in INDEXES.items():
        for keys, opts in specs:
            db[coll].create_index(keys, **opts)
    _indexes_ready.set()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # keep starting; /health reports 503 and retries index creation
        log.warning("could not create indexes at start-up: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_request: Request, exc: PyMongoError):
    log.error("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 7. Endpoints: health, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    if not _indexes_ready.is_set():  # start-up could not reach MongoDB: try again now that it answers
        try:
            ensure_indexes()
        except PyMongoError as exc:
            log.error("index creation failed: %s", exc)
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # the unique index, not a prior find_one, decides who wins a race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)  # same filter as the page (R10)
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = instant_or_now(body.punched_at)
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # Unique (emp_code, date) index: of two simultaneous inserts exactly one succeeds.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = instant_or_now(body.punched_at)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = truncate_to_seconds(rec["punch_in"])
    if ts <= pin:
        invalid(("body", "punched_at"), "punch-out must be after punch-in", body.punched_at)
    if ts - pin > MAX_SHIFT:
        invalid(("body", "punched_at"), "punch-out must be within 24 hours of punch-in", body.punched_at)
    derived = derived_fields(rec["status"], pin, ts, emp, rec["date"])
    # Conditional update: only the request that still sees punch_out == null wins; the other gets 409.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **{k: derived[k] for k in ("work_hours", "overtime_minutes", "half_day")}}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def attendance_list_query(emp_code, date_from, date_to, status) -> dict:
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    df = parse_date(date_from, ("query", "date_from")) if date_from is not None else None
    dt = parse_date(date_to, ("query", "date_to")) if date_to is not None else None
    if df and dt and df > dt:
        invalid(("query", "date_from"), "date_from must not be after date_to", date_from)
    if df or dt:
        q["date"] = {}
        if df:
            q["date"]["$gte"] = date_from
        if dt:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]
StatusParam = Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: StatusParam = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_list_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, body: RegularizeIn, day: str = Path(..., alias="date")):
    parse_date(day, ("path", "date"))
    sent = body.model_fields_set
    for field in ("status", "punch_in", "punch_out"):  # schema fields are not nullable
        if field in sent and getattr(body, field) is None:
            invalid(("body", field), "may not be null", None)

    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    new_status = body.status if body.status is not None else rec["status"]
    new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
    new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")

    if new_status not in PRESENCE_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            invalid(("body",), f"{new_status} clears punch times; do not send punch_in or punch_out", None)
        new_in = new_out = None
    else:
        if new_in is None:
            invalid(("body", "punch_in"), f"{new_status} requires a punch_in", None)
        new_in = truncate_to_seconds(new_in)
        if body.punch_in is not None and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != day:
            invalid(("body", "punch_in"), "punch_in must stay on the record's attendance date", body.punch_in)
        if new_out is not None:
            new_out = truncate_to_seconds(new_out)
            if (body.punch_in is not None or body.punch_out is not None) and not (
                new_in < new_out <= new_in + MAX_SHIFT
            ):
                invalid(("body", "punch_out"), "punch_out must be after punch_in and within 24 hours",
                        body.punch_out)

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           **derived_fields(new_status, new_in, new_out, emp, day)}
    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day") or False),
    }
    changes = {k: {"from": old[k], "to": new[k]} for k in new if old[k] != new[k]}
    if not changes:
        invalid(("body",), "the request changes nothing", None)

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: the update only applies if the record (including its history) is still exactly
    # what we read. A concurrent correction that got in first makes this match fail -> 409, never a lost entry.
    guard = {"_id": rec["_id"], "status": rec["status"], "punch_in": rec.get("punch_in"),
             "punch_out": rec.get("punch_out"), "history": rec.get("history")}
    updated = db.attendance_logs.find_one_and_update(
        guard, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "record was changed by another request; retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# 8. Analytics
# --------------------------------------------------------------------------- #
# Reusable aggregation expressions. They read the *stored* derived values (never recompute from punches) and
# treat missing late_minutes / overtime_minutes / half_day as 0 / 0 / false.
DATE_AS_DATE = {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}
IS_WEEKDAY = {"$not": [{"$in": [{"$dayOfWeek": DATE_AS_DATE}, [1, 7]]}]}  # $dayOfWeek: 1 = Sunday, 7 = Saturday
IS_PRESENT = {"$in": ["$status", list(PRESENCE_STATUSES)]}
PRESENT_VALUE = {"$cond": [IS_PRESENT, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}
LATE = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$cond": [{"$gt": [LATE, 0]}, 1, 0]}


def count_if(expr) -> dict:
    return {"$sum": {"$cond": [expr, 1, 0]}}


def mongo_half_up(expr, places: int) -> dict:
    """Half-up rounding inside MongoDB ($round is half-to-even). `expr` should be a decimal for exactness."""
    scale = Decimal128(str(10 ** places))
    return {"$cond": [
        {"$eq": [{"$ifNull": [expr, None]}, None]},
        None,
        {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, scale]}, Decimal128("0.5")]}}, scale]},
    ]}


def month_range(month: str) -> tuple[str, str]:
    first, last = month_bounds(month)
    return first.isoformat(), last.isoformat()


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    start, end = month_range(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [IS_WEEKDAY, PRESENT_VALUE, 0]}},  # R5-R7
            "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": {"$sum": IS_LATE},
            "total_late_minutes": {"$sum": LATE},  # weekend records included (R7)
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    start, end = month_range(month)
    match: dict = {"joined_on": {"$lte": end}}  # R9: joined on or before the last day of the month
    if department is not None:
        match["department"] = department
    return [
        # Start from employees, so an employee with no logs is still a row (and counts in headcount).
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [IS_WEEKDAY, PRESENT_VALUE, 0]}},
                    "wh_sum": {"$sum": {"$cond": [
                        {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}, {"$toDecimal": "$work_hours"}, 0]}},
                    "wh_count": count_if({"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}),
                    "late_count": {"$sum": IS_LATE},
                    "total_late_minutes": {"$sum": LATE},
                    "leave_count": count_if({"$eq": ["$status", "LEAVE"]}),
                    "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
                }},
            ],
            "as": "stats",
        }},
        # Zero logs -> empty array -> {} so the sums below simply add nothing for this employee.
        {"$set": {"stats": {"$ifNull": [{"$first": "$stats"}, {"$literal": {}}]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$stats.present_days"},
            "wh_sum": {"$sum": "$stats.wh_sum"},
            "wh_count": {"$sum": "$stats.wh_count"},
            "late_count": {"$sum": "$stats.late_count"},
            "total_late_minutes": {"$sum": "$stats.total_late_minutes"},
            "leave_count": {"$sum": "$stats.leave_count"},
            "on_duty_count": {"$sum": "$stats.on_duty_count"},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            # mean over records (sum / count), not a mean of per-employee means
            "avg_work_hours": {"$cond": [{"$gt": ["$wh_count", 0]},
                                         {"$divide": [{"$toDecimal": "$wh_sum"}, "$wh_count"]}, None]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    start, end = month_range(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code",
                     "pipeline": [{"$project": {"_id": 0, "name": 1, "department": 1}}], "as": "emp"}},
        {"$unwind": "$emp"},  # drops logs whose employee does not exist
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank is standard competition ranking: totals 90, 60, 60, 30 -> ranks 1, 2, 2, 4.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking, so ties at the cutoff all stay
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def department_trend_pipeline(department: str, start: date, days: int) -> list:
    start_dt = datetime.combine(start, time(0), tzinfo=UTC)
    present_rate = {"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]}
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"}, "joined": {"$push": "$joined_on"}}},
        # Gap filling in the database: one row per calendar day from `from` to `to`, records or not.
        {"$set": {"date": {"$map": {
            "input": {"$range": [0, days]},
            "as": "i",
            "in": {"$dateToString": {"format": "%Y-%m-%d",
                                     "date": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$i"}}}},
        }}}},
        {"$unwind": "$date"},
        # The department's records for that day (localField is the array of emp_codes: matches any of them).
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "codes",
            "foreignField": "emp_code",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                {"$group": {"_id": None, "present_count": {"$sum": PRESENT_VALUE}, "late_count": {"$sum": IS_LATE}}},
            ],
            "as": "day",
        }},
        {"$set": {
            "day": {"$first": "$day"},
            "is_working_day": IS_WEEKDAY,
            "headcount": {"$size": {"$filter": {"input": "$joined", "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}},
        }},
        {"$set": {"present_count": {"$ifNull": ["$day.present_count", 0]},
                  "late_count": {"$ifNull": ["$day.late_count", 0]}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, mongo_half_up(present_rate, 4), None]}}},
        # $avg ignores nulls and returns null if the window has none; the window never reaches before `from`.
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": {"$toDouble": "$attendance_rate"},
            "moving_avg_7d": {"$toDouble": mongo_half_up("$moving_avg_7d", 4)},
        }},
        {"$sort": {"date": 1}},
    ]


def trend_range(from_: str, to: str) -> tuple[date, int]:
    start = parse_date(from_, ("query", "from"))
    end = parse_date(to, ("query", "to"))
    if end < start:
        invalid(("query", "to"), "to must not be before from", to)
    days = (end - start).days + 1
    if days > MAX_TREND_DAYS:
        invalid(("query", "to"), f"range exceeds {MAX_TREND_DAYS} days", to)
    return start, days


MonthParam = Query(..., pattern=MONTH_RE, description="Calendar month, YYYY-MM.")


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = MonthParam):
    emp = get_employee_or_404(emp_code)
    first, last = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    s = rows[0] if rows else {}
    wd = working_days(first, last, emp["joined_on"])
    present = s.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": round_half_up(present, 2),
        "leave_days": s.get("leave_days", 0),
        "late_count": s.get("late_count", 0),
        "total_late_minutes": s.get("total_late_minutes", 0),
        "total_overtime_minutes": s.get("total_overtime_minutes", 0),
        "attendance_pct": round_half_up(Decimal(repr(float(present))) * 100 / wd, 2) if wd else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = MonthParam, department: Optional[str] = None):
    items = []
    for row in db.employees.aggregate(department_summary_pipeline(month, department)):
        items.append({
            "department": row["department"],
            "headcount": row["headcount"],
            "present_days": round_half_up(plain_number(row["present_days"]), 2),
            "avg_work_hours": round_half_up(row["avg_work_hours"], 2),
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = MonthParam, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    rows = db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department))
    items = [{"rank": r["rank"], "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
              "total_late_minutes": r["total_late_minutes"], "late_count": r["late_count"]} for r in rows]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    start, days = trend_range(from_, to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    rows = db.employees.aggregate(department_trend_pipeline(department, start, days))
    items = [{
        "date": r["date"],
        "is_working_day": r["is_working_day"],
        "headcount": r["headcount"],
        "present_count": plain_number(r["present_count"]),
        "late_count": r["late_count"],
        "attendance_rate": r.get("attendance_rate"),
        "moving_avg_7d": r.get("moving_avg_7d"),
    } for r in rows]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 9. Admin explain
# --------------------------------------------------------------------------- #
def require(value, name: str):
    if value is None:
        invalid(("query", name), "Field required", None)
    return value


def to_json(doc: dict) -> dict:
    """Raw explain output -> plain JSON (Timestamps, ObjectIds, Int64 etc. via relaxed Extended JSON)."""
    return json.loads(json_util.dumps(doc, json_options=json_util.RELAXED_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard",
                      "department_trend"] = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: StatusParam = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Explains the main query exactly as the endpoint builds it (same builder functions)."""
    if endpoint == "attendance_list":
        coll = "attendance_logs"
        cmd = {
            "find": coll,
            "filter": attendance_list_query(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            coll, pipeline = "attendance_logs", employee_monthly_pipeline(require(emp_code, "emp_code"),
                                                                          require(month, "month"))
        elif endpoint == "department_summary":
            coll, pipeline = "employees", department_summary_pipeline(require(month, "month"), department)
        elif endpoint == "late_leaderboard":
            coll, pipeline = "attendance_logs", late_leaderboard_pipeline(require(month, "month"), limit, department)
        else:  # department_trend
            start, days = trend_range(require(from_, "from"), require(to, "to"))
            coll, pipeline = "employees", department_trend_pipeline(require(department, "department"), start, days)
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}
    result = db.command("explain", cmd, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": coll, "explain": to_json(result)}
