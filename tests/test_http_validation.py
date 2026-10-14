"""HTTP tests that never reach MongoDB: request validation (422) happens before any database call, and the
"database down" behaviour is tested against a deliberately unreachable server."""
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app import main as m
from tests.helpers import ist

client = TestClient(m.app)  # no `with`: the start-up hook (index creation) is not run here

GOOD_TS = ist(2026, 7, 6, 9, 30)


def assert_422(resp):
    assert resp.status_code == 422, resp.text
    assert isinstance(resp.json()["detail"], list)  # FastAPI's default validation body


# ---------------------------------------------------------------- employees
VALID_EMP = {"emp_code": "EMP0001", "name": "Asha", "email": "asha@example.com", "department": "Eng",
             "joined_on": "2026-01-05"}


@pytest.mark.parametrize("override", [
    {"emp_code": "E1"}, {"emp_code": "EMP123"}, {"emp_code": "EMP1234567"}, {"emp_code": "emp0001"},
    {"emp_code": "EMP١٢٣٤"},           # non-ASCII digits
    {"name": ""}, {"email": "not-an-email"}, {"department": ""},
    {"shift_start": "9:30"}, {"shift_end": "24:00"},
    {"shift_start": "09:30", "shift_end": "09:30"},          # shift_start must differ from shift_end
    {"joined_on": "2026-02-30"}, {"joined_on": "05-01-2026"},
    {"name": 123},
])
def test_create_employee_invalid(override):
    assert_422(client.post("/employees", json={**VALID_EMP, **override}))


@pytest.mark.parametrize("missing", ["emp_code", "name", "email", "department", "joined_on"])
def test_create_employee_missing_field(missing):
    body = {k: v for k, v in VALID_EMP.items() if k != missing}
    assert_422(client.post("/employees", json=body))


@pytest.mark.parametrize("qs", ["page=0", "page_size=0", "page_size=101", "page=abc"])
def test_list_employees_bad_paging(qs):
    assert_422(client.get(f"/employees?{qs}"))


# ---------------------------------------------------------------- punch in / out bodies
@pytest.mark.parametrize("path", ["/attendance/punch-in", "/attendance/punch-out"])
@pytest.mark.parametrize("bad", [
    1783312500,              # seconds, not milliseconds
    1783312500000.0,         # float
    "1783312500000",         # string
    True,
    99_999_999_999,          # just below the millisecond threshold
    4_102_444_800_001,       # out of range
    None,                    # not nullable
])
def test_punch_bad_timestamp(path, bad):
    assert_422(client.post(path, json={"emp_code": "EMP0001", "punched_at": bad}))


@pytest.mark.parametrize("status", ["ABSENT", "LEAVE", "present", "SICK"])
def test_punch_in_rejects_non_presence_status(status):
    assert_422(client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": GOOD_TS,
                                                          "status": status}))


def test_punch_in_missing_emp_code():
    assert_422(client.post("/attendance/punch-in", json={"punched_at": GOOD_TS}))


# ---------------------------------------------------------------- attendance list
@pytest.mark.parametrize("qs", [
    "date_from=2026-07-10&date_to=2026-07-01", "date_from=2026-13-01", "date_to=20260701",
    "status=SICK", "page=0", "page_size=101",
])
def test_list_attendance_invalid(qs):
    assert_422(client.get(f"/attendance?{qs}"))


# ---------------------------------------------------------------- regularize
@pytest.mark.parametrize("body", [
    {"regularized_by": "hr"},                                       # reason missing
    {"reason": "abc", "regularized_by": "hr"},                      # reason too short
    {"reason": "valid reason", "regularized_by": ""},
    {"reason": "valid reason", "regularized_by": "hr", "punch_in": 1783312500},   # seconds
    {"reason": "valid reason", "regularized_by": "hr", "punch_out": "1783312500000"},
    {"reason": "valid reason", "regularized_by": "hr", "status": "SICK"},
    {"reason": "valid reason", "regularized_by": "hr", "punch_in": None},
])
def test_regularize_invalid_body(body):
    assert_422(client.patch("/attendance/EMP0001/2026-07-06", json=body))


def test_regularize_invalid_path_date():
    assert_422(client.patch("/attendance/EMP0001/2026-07-32", json={"reason": "valid reason",
                                                                     "regularized_by": "hr", "status": "LEAVE"}))


# ---------------------------------------------------------------- analytics parameters
@pytest.mark.parametrize("url", [
    "/analytics/employees/EMP0001/monthly",                    # month missing
    "/analytics/employees/EMP0001/monthly?month=2026-13",
    "/analytics/employees/EMP0001/monthly?month=2026-7",
    "/analytics/departments/summary",
    "/analytics/departments/summary?month=202607",
    "/analytics/leaderboard/late?month=2026-07&limit=0",
    "/analytics/leaderboard/late?month=2026-07&limit=51",
    "/analytics/departments/Eng/trend?from=2026-07-10",        # `to` missing
    "/analytics/departments/Eng/trend?from=2026-07-10&to=2026-07-01",
    "/analytics/departments/Eng/trend?from=2026-07-01&to=2026-10-01",   # 93 days
    "/analytics/departments/Eng/trend?from=2026-02-30&to=2026-03-01",
])
def test_analytics_invalid_params(url):
    assert_422(client.get(url))


# ---------------------------------------------------------------- explain parameters
@pytest.mark.parametrize("url", [
    "/admin/explain/nope",
    "/admin/explain/employee_monthly?month=2026-07",            # emp_code missing
    "/admin/explain/employee_monthly?emp_code=EMP0001",         # month missing
    "/admin/explain/department_summary",
    "/admin/explain/late_leaderboard",
    "/admin/explain/department_trend?from=2026-07-01&to=2026-07-31",   # department missing
    "/admin/explain/department_trend?department=Eng&to=2026-07-31",
    "/admin/explain/attendance_list?date_from=2026-07-10&date_to=2026-07-01",
])
def test_explain_invalid_params(url):
    assert_422(client.get(url))


# ---------------------------------------------------------------- database unavailable
@pytest.fixture
def dead_db(monkeypatch):
    dead = MongoClient("mongodb://127.0.0.1:1", serverSelectionTimeoutMS=300, connectTimeoutMS=300)
    monkeypatch.setattr(m, "client", dead)
    monkeypatch.setattr(m, "db", dead["nothing_here"])
    yield
    dead.close()


def test_health_is_503_when_mongodb_is_down(dead_db):
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json() == {"detail": "database unavailable"}


def test_db_error_is_503_not_500(dead_db):
    resp = client.get("/employees")
    assert resp.status_code == 503


def test_openapi_lists_all_contract_paths():
    paths = client.get("/openapi.json").json()["paths"]
    expected = {
        ("/health", "get"), ("/employees", "post"), ("/employees", "get"), ("/attendance/punch-in", "post"),
        ("/attendance/punch-out", "post"), ("/attendance", "get"), ("/attendance/{emp_code}/{date}", "patch"),
        ("/analytics/employees/{emp_code}/monthly", "get"), ("/analytics/departments/summary", "get"),
        ("/analytics/leaderboard/late", "get"), ("/analytics/departments/{department}/trend", "get"),
        ("/admin/explain/{endpoint}", "get"),
    }
    present = {(p, verb) for p, ops in paths.items() for verb in ops}
    assert expected <= present
