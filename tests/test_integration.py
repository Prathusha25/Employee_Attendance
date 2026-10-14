"""Integration tests against a real MongoDB (6.0+).

Run with:  MONGO_TEST_URI=mongodb://localhost:27017 pytest -v
They use a throw-away database hrone_test_<random> (see conftest.py) and drop it at the end.
Without MONGO_TEST_URI the whole module is skipped (reported as skipped, never as passed).

Expected numbers are worked out by hand in the comments, independently of the code under test.
"""
import threading

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from tests.conftest import MONGO_TEST_URI, TEST_DB_NAME
from tests.helpers import ist, ist_dt

if not MONGO_TEST_URI:
    pytest.skip("MONGO_TEST_URI not set: MongoDB integration tests NOT RUN", allow_module_level=True)

_probe = MongoClient(MONGO_TEST_URI, serverSelectionTimeoutMS=3000)
try:
    _probe.admin.command("ping")
except Exception as exc:  # pragma: no cover
    pytest.skip(f"MongoDB at MONGO_TEST_URI unreachable ({exc}): integration tests NOT RUN", allow_module_level=True)

from app import main as m  # noqa: E402  (imported after the env is set in conftest)

assert TEST_DB_NAME.startswith("hrone_test_")
raw = _probe[TEST_DB_NAME]


@pytest.fixture(scope="module", autouse=True)
def api():
    with TestClient(m.app) as c:  # runs start-up: index creation
        yield c
    _probe.drop_database(TEST_DB_NAME)


@pytest.fixture
def clean():
    raw.employees.delete_many({})
    raw.attendance_logs.delete_many({})


def concurrently(n, fn):
    """Run fn() in n threads released at the same instant; return the results."""
    barrier = threading.Barrier(n)
    results = [None] * n

    def run(i):
        c = TestClient(m.app)
        barrier.wait()
        results[i] = fn(c)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def make_emp(api, code, dept="Engineering", start="09:30", end="18:30", joined="2026-01-01"):
    r = api.post("/employees", json={"emp_code": code, "name": f"Name {code}", "email": f"{code.lower()}@x.io",
                                     "department": dept, "shift_start": start, "shift_end": end,
                                     "joined_on": joined})
    assert r.status_code == 201, r.text
    return r.json()


RECORD_KEYS = {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes",
               "overtime_minutes", "half_day", "history"}


# =========================================================================== system / indexes
def test_health_ok(api):
    assert api.get("/health").json() == {"status": "ok"}


def test_indexes_created_at_startup(api):
    att = {tuple(v["key"]) for v in raw.attendance_logs.index_information().values()}
    assert (("emp_code", 1), ("date", 1)) in att
    assert raw.attendance_logs.index_information()["emp_code_1_date_1"]["unique"] is True
    assert raw.employees.index_information()["emp_code_1"]["unique"] is True
    m.ensure_indexes()  # idempotent: a second run is a no-op, not an error


# =========================================================================== employees
def test_create_employee_shape_and_duplicate(api, clean):
    body = make_emp(api, "EMP0001")
    assert set(body) == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on",
                         "created_at"}
    assert isinstance(body["created_at"], int) and body["created_at"] > 100_000_000_000
    assert "_id" not in body and "id" not in body
    stored = raw.employees.find_one({"emp_code": "EMP0001"})
    assert m.dt_to_ms(stored["created_at"]) == body["created_at"]
    r = api.post("/employees", json={"emp_code": "EMP0001", "name": "Dup", "email": "d@x.io",
                                     "department": "X", "joined_on": "2026-01-01"})
    assert r.status_code == 409


def test_create_employee_defaults(api, clean):
    r = api.post("/employees", json={"emp_code": "EMP0002", "name": "A", "email": "a@x.io", "department": "X",
                                     "joined_on": "2026-01-01"})
    assert r.status_code == 201 and (r.json()["shift_start"], r.json()["shift_end"]) == ("09:30", "18:30")


def test_concurrent_duplicate_employee_creates(api, clean):
    body = {"emp_code": "EMP0777", "name": "A", "email": "a@x.io", "department": "X", "joined_on": "2026-01-01"}
    codes = sorted(r.status_code for r in concurrently(8, lambda c: c.post("/employees", json=body)))
    assert codes == [201] + [409] * 7
    assert raw.employees.count_documents({"emp_code": "EMP0777"}) == 1


def test_list_employees_filter_sort_pagination(api, clean):
    for code in ["EMP0005", "EMP0001", "EMP0004", "EMP0002", "EMP0003"]:
        make_emp(api, code, dept="Eng")
    for code in ["EMP0010", "EMP0011"]:
        make_emp(api, code, dept="Sales")
    r = api.get("/employees", params={"department": "Eng", "page": 2, "page_size": 2}).json()
    assert r["total"] == 5  # filtered total, not 7
    assert [e["emp_code"] for e in r["items"]] == ["EMP0003", "EMP0004"]  # skip = (2-1)*2
    assert (r["page"], r["page_size"]) == (2, 2)
    r = api.get("/employees", params={"department": "Eng", "page": 3, "page_size": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0005"]
    r = api.get("/employees", params={"department": "eng"}).json()  # case-sensitive
    assert r["total"] == 0 and r["items"] == []
    r = api.get("/employees").json()
    assert r["total"] == 7 and [e["emp_code"] for e in r["items"]][:2] == ["EMP0001", "EMP0002"]


# =========================================================================== punch-in
def test_punch_in_unknown_employee_404(api, clean):
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP9999", "punched_at": ist(2026, 7, 6, 9, 30)})
    assert r.status_code == 404


@pytest.mark.parametrize("hms, late", [((9, 40, 0), 0), ((9, 40, 1), 10), ((10, 15, 59), 45)])
def test_punch_in_late_boundary(api, clean, hms, late):
    make_emp(api, "EMP0001")
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, *hms)})
    assert r.status_code == 201, r.text
    rec = r.json()
    assert set(rec) == RECORD_KEYS
    assert rec["late_minutes"] == late and rec["date"] == "2026-07-06" and rec["status"] == "PRESENT"
    assert (rec["punch_out"], rec["work_hours"], rec["overtime_minutes"], rec["half_day"], rec["history"]) == \
        (None, None, 0, False, [])


def test_punch_in_truncates_milliseconds(api, clean):
    make_emp(api, "EMP0001")
    rec = api.post("/attendance/punch-in", json={"emp_code": "EMP0001",
                                                  "punched_at": ist(2026, 7, 6, 9, 40, 0, ms=900)}).json()
    assert rec["punch_in"] == ist(2026, 7, 6, 9, 40, 0) and rec["late_minutes"] == 0
    stored = raw.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert stored["punch_in"] == ist_dt(2026, 7, 6, 9, 40, 0)  # a real BSON date, truncated value
    assert stored["history"] == [] and stored["punch_out"] is None


def test_punch_in_status_and_duplicate(api, clean):
    make_emp(api, "EMP0001")
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 9, 0),
                                               "status": "WFH"})
    assert r.status_code == 201 and r.json()["status"] == "WFH"
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 14, 0)})
    assert r.status_code == 409


def test_punch_in_ignores_client_derived_fields(api, clean):
    make_emp(api, "EMP0001")
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 10, 5),
                                               "late_minutes": 0, "work_hours": 99})
    assert r.status_code == 201 and r.json()["late_minutes"] == 35 and r.json()["work_hours"] is None


def test_concurrent_punch_in_exactly_one_201(api, clean):
    make_emp(api, "EMP0001")
    body = {"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 9, 30)}
    codes = sorted(r.status_code for r in concurrently(10, lambda c: c.post("/attendance/punch-in", json=body)))
    assert codes == [201] + [409] * 9
    assert raw.attendance_logs.count_documents({"emp_code": "EMP0001"}) == 1


@pytest.mark.parametrize("wall, date, late", [
    ((2026, 7, 6, 21, 55), "2026-07-06", 0),
    ((2026, 7, 7, 0, 0), "2026-07-06", 120),      # midnight belongs to the shift that started on the 6th
    ((2026, 7, 7, 5, 30), "2026-07-06", 450),     # 05:30 < 06:00 -> previous day's shift, 7 h 30 after 22:00
    ((2026, 7, 7, 6, 0), "2026-07-07", 0),        # exactly shift_end -> own date, before 22:00 start
])
def test_overnight_punch_in_date(api, clean, wall, date, late):
    make_emp(api, "EMP0005", dept="Support", start="22:00", end="06:00")
    rec = api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ist(*wall)}).json()
    assert (rec["date"], rec["late_minutes"]) == (date, late)


# =========================================================================== punch-out
def test_overnight_punch_out_sample_7(api, clean):
    make_emp(api, "EMP0005", dept="Support", start="22:00", end="06:00")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ist(2026, 7, 6, 21, 55)})
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ist(2026, 7, 7, 6, 40)})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["date"] == "2026-07-06"
    assert (rec["work_hours"], rec["overtime_minutes"], rec["half_day"]) == (8.75, 40, False)
    assert rec["punch_out"] == ist(2026, 7, 7, 6, 40) and rec["history"] == []


@pytest.mark.parametrize("out_hms, hours, ot, half", [
    ((18, 35, 0), 9.08, 0, False),     # in 09:30 -> 9 h 05 m = 9.0833 -> 9.08; overtime 5 < 30
    ((19, 0, 0), 9.5, 30, False),      # overtime exactly 30 counts
    ((18, 59, 59), 9.5, 0, False),     # 9.49972 -> 9.50; overtime 29:59 -> 0
    ((14, 0, 0), 4.5, 0, False),       # 4.50 is not < 4.50
    ((13, 59, 42), 4.5, 0, False),     # 16182 s = 4.495 h -> 4.50 half-up -> not a half day
    ((13, 59, 41), 4.49, 0, True),
    ((9, 37, 30), 0.13, 0, True),      # 450 s = 0.125 h -> 0.13 (not banker's 0.12)
])
def test_punch_out_rules(api, clean, out_hms, hours, ot, half):
    make_emp(api, "EMP0001")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 9, 30)})
    rec = api.post("/attendance/punch-out", json={"emp_code": "EMP0001",
                                                   "punched_at": ist(2026, 7, 6, *out_hms)}).json()
    assert (rec["work_hours"], rec["overtime_minutes"], rec["half_day"]) == (hours, ot, half)


def test_punch_out_errors(api, clean):
    make_emp(api, "EMP0001")
    out = lambda ts: api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts})  # noqa
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP9999",
                                                   "punched_at": ist(2026, 7, 6, 18)}).status_code == 404
    assert out(ist(2026, 7, 6, 18)).status_code == 404  # no punch-in at all
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 9, 30)})
    assert out(ist(2026, 7, 6, 9, 0)).status_code == 404      # nothing punched in at or before 09:00
    assert out(ist(2026, 7, 6, 9, 30)).status_code == 422     # punched_at == punch_in
    assert out(ist(2026, 7, 7, 9, 30, 1)).status_code == 422  # more than 24 h later
    assert out(ist(2026, 7, 6, 18, 30)).status_code == 200
    assert out(ist(2026, 7, 6, 18, 45)).status_code == 409    # already punched out


def test_concurrent_punch_out_exactly_one_200(api, clean):
    make_emp(api, "EMP0001")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 9, 30)})
    body = {"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 18, 30)}
    codes = sorted(r.status_code for r in concurrently(10, lambda c: c.post("/attendance/punch-out", json=body)))
    assert codes == [200] + [409] * 9


# =========================================================================== attendance list
def seed_list(api):
    for code in ["EMP0001", "EMP0002", "EMP0003"]:
        make_emp(api, code)
    docs = []
    for day in range(1, 11):  # 2026-07-01 .. 2026-07-10, three employees -> 30 records
        for code in ["EMP0001", "EMP0002", "EMP0003"]:
            status = "LEAVE" if (code == "EMP0002" and day % 2 == 0) else "PRESENT"
            pin = ist_dt(2026, 7, day, 9, 30) if status == "PRESENT" else None
            docs.append({"emp_code": code, "date": f"2026-07-{day:02d}", "status": status, "punch_in": pin,
                         "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
                         "half_day": False, "history": []})
    raw.attendance_logs.insert_many(docs)


def test_list_attendance_sort_and_pagination(api, clean):
    seed_list(api)
    r = api.get("/attendance", params={"page": 1, "page_size": 4}).json()
    assert r["total"] == 30
    assert [(i["date"], i["emp_code"]) for i in r["items"]] == [
        ("2026-07-10", "EMP0001"), ("2026-07-10", "EMP0002"), ("2026-07-10", "EMP0003"), ("2026-07-09", "EMP0001")]
    r = api.get("/attendance", params={"page": 2, "page_size": 4}).json()
    assert [(i["date"], i["emp_code"]) for i in r["items"]][0] == ("2026-07-09", "EMP0002")
    assert all(set(i) == RECORD_KEYS for i in r["items"])
    assert all(isinstance(i["punch_in"], int) for i in r["items"])
    r = api.get("/attendance", params={"page": 8, "page_size": 4}).json()
    assert len(r["items"]) == 2 and r["total"] == 30   # 30 = 7*4 + 2
    assert api.get("/attendance", params={"page": 9, "page_size": 4}).json()["items"] == []


def test_list_attendance_filters_and_filtered_total(api, clean):
    seed_list(api)
    r = api.get("/attendance", params={"emp_code": "EMP0002", "status": "LEAVE", "page_size": 2}).json()
    assert r["total"] == 5 and len(r["items"]) == 2           # days 2,4,6,8,10
    assert [i["date"] for i in r["items"]] == ["2026-07-10", "2026-07-08"]
    assert all(i["punch_in"] is None for i in r["items"])
    r = api.get("/attendance", params={"date_from": "2026-07-03", "date_to": "2026-07-05"}).json()
    assert r["total"] == 9                                    # inclusive: 3 days x 3 employees
    r = api.get("/attendance", params={"date_from": "2026-07-05", "date_to": "2026-07-05",
                                       "emp_code": "EMP0003"}).json()
    assert r["total"] == 1 and r["items"][0]["date"] == "2026-07-05"
    assert api.get("/attendance", params={"emp_code": "EMP4040"}).json() == \
        {"items": [], "total": 0, "page": 1, "page_size": 20}


def test_list_attendance_legacy_record_defaults(api, clean):
    make_emp(api, "EMP0002")
    raw.attendance_logs.insert_one({"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
                                    "punch_in": ist_dt(2026, 7, 14, 9, 32), "punch_out": ist_dt(2026, 7, 14, 18, 31),
                                    "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    item = api.get("/attendance", params={"emp_code": "EMP0002"}).json()["items"][0]
    assert item["half_day"] is False and item["history"] == []


# =========================================================================== regularize / audit trail
def seed_late_day(api):
    make_emp(api, "EMP0001")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 7, 10, 5)})
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 7, 18, 30)})


def patch(api, body, code="EMP0001", day="2026-07-07"):
    return api.patch(f"/attendance/{code}/{day}", json={"reason": "biometric glitch", "regularized_by": "hr.admin",
                                                        **body})


def test_regularize_punch_in_sample_8(api, clean):
    seed_late_day(api)  # 10:05 -> 18:30: late 35, 8.42 h
    r = patch(api, {"punch_in": ist(2026, 7, 7, 9, 28)})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert (rec["late_minutes"], rec["work_hours"], rec["punch_in"]) == (0, 9.03, ist(2026, 7, 7, 9, 28))
    assert len(rec["history"]) == 1
    h = rec["history"][0]
    assert (h["by"], h["reason"]) == ("hr.admin", "biometric glitch") and isinstance(h["at"], int)
    assert h["changes"] == {
        "punch_in": {"from": ist(2026, 7, 7, 10, 5), "to": ist(2026, 7, 7, 9, 28)},  # epoch ms in the API
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 8.42, "to": 9.03},
    }
    stored = raw.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-07"})
    assert stored["history"][0]["changes"]["punch_in"]["to"] == ist_dt(2026, 7, 7, 9, 28)  # BSON date in Mongo


def test_regularize_appends_never_overwrites(api, clean):
    seed_late_day(api)
    first = patch(api, {"punch_in": ist(2026, 7, 7, 9, 28)}).json()["history"][0]
    rec = patch(api, {"punch_out": ist(2026, 7, 7, 19, 10)}).json()
    assert len(rec["history"]) == 2 and rec["history"][0] == first
    assert rec["history"][1]["changes"] == {
        "punch_out": {"from": ist(2026, 7, 7, 18, 30), "to": ist(2026, 7, 7, 19, 10)},
        "work_hours": {"from": 9.03, "to": 9.7},     # 09:28 -> 19:10 = 9 h 42 m = 9.70
        "overtime_minutes": {"from": 0, "to": 40},
    }


def test_regularize_to_absent_clears_everything(api, clean):
    seed_late_day(api)
    rec = patch(api, {"status": "ABSENT"}).json()
    assert (rec["status"], rec["punch_in"], rec["punch_out"], rec["work_hours"], rec["late_minutes"],
            rec["overtime_minutes"], rec["half_day"]) == ("ABSENT", None, None, None, 0, 0, False)
    assert set(rec["history"][0]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes"}
    # back to PRESENT needs a punch_in (none stored any more)
    assert patch(api, {"status": "PRESENT"}).status_code == 422
    rec = patch(api, {"status": "WFH", "punch_in": ist(2026, 7, 7, 9, 45)}).json()
    assert (rec["status"], rec["late_minutes"], rec["work_hours"], len(rec["history"])) == ("WFH", 15, None, 2)


def test_regularize_rule_violations(api, clean):
    seed_late_day(api)
    assert patch(api, {"status": "LEAVE", "punch_in": ist(2026, 7, 7, 9, 0)}).status_code == 422
    assert patch(api, {"punch_in": ist(2026, 7, 8, 9, 0)}).status_code == 422          # other attendance date
    assert patch(api, {"punch_out": ist(2026, 7, 7, 10, 0)}).status_code == 422        # before punch-in
    assert patch(api, {"punch_out": ist(2026, 7, 8, 10, 6)}).status_code == 422        # > 24 h
    assert patch(api, {"punch_in": ist(2026, 7, 7, 10, 5)}).status_code == 422         # changes nothing
    assert patch(api, {}).status_code == 422                                           # changes nothing
    assert patch(api, {"status": "LEAVE"}, code="EMP9999").status_code == 404
    assert patch(api, {"status": "LEAVE"}, day="2026-07-01").status_code == 404
    stored = raw.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert stored["history"] == [] and stored["late_minutes"] == 35   # nothing was written by failed requests


def test_regularize_legacy_record_without_history(api, clean):
    make_emp(api, "EMP0002")
    raw.attendance_logs.insert_one({"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
                                    "punch_in": ist_dt(2026, 7, 14, 9, 32), "punch_out": ist_dt(2026, 7, 14, 18, 31),
                                    "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    rec = patch(api, {"status": "ON_DUTY"}, code="EMP0002", day="2026-07-14").json()
    assert rec["status"] == "ON_DUTY" and len(rec["history"]) == 1
    assert rec["history"][0]["changes"] == {"status": {"from": "PRESENT", "to": "ON_DUTY"}}


def test_concurrent_regularize_never_loses_history(api, clean):
    seed_late_day(api)
    outs = [ist(2026, 7, 7, 19, i) for i in range(10)]
    it = iter(outs)
    lock = threading.Lock()

    def one(c):
        with lock:
            ts = next(it)
        return patch(c, {"punch_out": ts})

    results = concurrently(10, one)
    ok = [r for r in results if r.status_code == 200]
    assert ok and all(r.status_code in (200, 409) for r in results)
    stored = raw.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-07"})
    assert len(stored["history"]) == len(ok)  # every 200 left exactly one entry; none silently lost


# =========================================================================== analytics fixture
JULY_EMPLOYEES = [
    # code, department, shift, joined_on
    ("EMP1001", "Engineering", ("09:30", "18:30"), "2026-01-01"),
    ("EMP1002", "Engineering", ("09:30", "18:30"), "2026-07-15"),   # mid-month joiner
    ("EMP1003", "Engineering", ("09:30", "18:30"), "2026-01-01"),   # no logs at all
    ("EMP1004", "Engineering", ("09:30", "18:30"), "2026-08-05"),   # joins after July
    ("EMP2001", "Sales", ("09:30", "18:30"), "2026-02-01"),
    ("EMP2002", "Sales", ("09:30", "18:30"), "2026-02-01"),
    ("EMP3001", "Support", ("22:00", "06:00"), "2026-03-10"),       # overnight
    ("EMP4001", "Legal", ("09:30", "18:30"), "2026-01-01"),         # whole department has no logs
    ("EMP5001", "Design", ("09:30", "18:30"), "2026-08-10"),        # whole department joins later
]

# code, date, status, work_hours, late, overtime, half_day   (None half_day = field missing: legacy)
JULY_LOGS = [
    ("EMP1001", "2026-06-30", "PRESENT", 9.0, 100, 0, False),       # June: outside the month
    ("EMP1001", "2026-07-06", "PRESENT", 9.0, 35, 0, False),        # Mon
    ("EMP1001", "2026-07-07", "WFH", 3.5, 0, 0, True),              # Tue, half day
    ("EMP1001", "2026-07-08", "ABSENT", None, 0, 0, False),
    ("EMP1001", "2026-07-09", "LEAVE", None, 0, 0, False),
    ("EMP1001", "2026-07-11", "PRESENT", 5.0, 40, 45, False),       # Sat: not a present day, late/OT count
    ("EMP1001", "2026-07-13", "ON_DUTY", 8.0, 15, 30, False),       # Mon
    ("EMP1001", "2026-07-14", "PRESENT", None, 0, 0, False),        # Tue, open record
    ("EMP1001", "2026-07-31", "PRESENT", 8.5, 0, 0, None),          # Fri, legacy (no half_day / history)
    ("EMP1001", "2026-08-03", "PRESENT", 9.0, 100, 0, False),       # August: outside the month
    ("EMP1002", "2026-07-15", "PRESENT", 9.0, 30, 0, False),
    ("EMP1002", "2026-07-16", "PRESENT", 4.25, 0, 0, True),
    ("EMP2001", "2026-07-06", "PRESENT", 9.1, 60, 0, False),
    ("EMP2001", "2026-07-07", "PRESENT", 8.0, 0, 0, False),
    ("EMP2001", "2026-07-08", "LEAVE", None, 0, 0, False),
    ("EMP2002", "2026-07-06", "PRESENT", 7.0, 20, 0, False),
    ("EMP2002", "2026-07-10", "PRESENT", 8.0, 40, 0, False),
    ("EMP2002", "2026-07-13", "ON_DUTY", None, 0, 0, False),        # open ON_DUTY record
    ("EMP3001", "2026-07-06", "PRESENT", 8.75, 30, 40, False),
    ("EMP9999", "2026-07-06", "PRESENT", 9.0, 500, 0, False),       # orphan log: employee does not exist
]


@pytest.fixture(scope="module")
def july(api):
    raw.employees.delete_many({})
    raw.attendance_logs.delete_many({})
    for code, dept, (start, end), joined in JULY_EMPLOYEES:
        make_emp(api, code, dept=dept, start=start, end=end, joined=joined)
    docs = []
    for code, day, status, wh, late, ot, half in JULY_LOGS:
        present = status in ("PRESENT", "WFH", "ON_DUTY")
        y, mo, d = map(int, day.split("-"))
        doc = {"emp_code": code, "date": day, "status": status,
               "punch_in": ist_dt(y, mo, d, 9, 30) if present else None,
               "punch_out": ist_dt(y, mo, d, 18, 30) if wh is not None else None,
               "work_hours": wh, "late_minutes": late, "overtime_minutes": ot}
        if half is not None:
            doc["half_day"] = half
            doc["history"] = []
        docs.append(doc)
    raw.attendance_logs.insert_many(docs)
    yield
    raw.employees.delete_many({})
    raw.attendance_logs.delete_many({})


# =========================================================================== employee monthly
def test_monthly_full_employee(api, july):
    r = api.get("/analytics/employees/EMP1001/monthly", params={"month": "2026-07"})
    assert r.status_code == 200
    assert r.json() == {
        "emp_code": "EMP1001", "month": "2026-07",
        "working_days": 23,                # Mon-Fri in July 2026
        "present_days": 4.5,               # 07-06 1 + 07-07 0.5 + 07-13 1 + 07-14 1 + 07-31 1 (Sat 07-11 excluded)
        "leave_days": 1,
        "late_count": 3,                   # 35, 40 (Sat), 15
        "total_late_minutes": 90,
        "total_overtime_minutes": 75,      # 45 (Sat) + 30
        "attendance_pct": 19.57,           # 4.5 / 23 * 100 = 19.5652...
    }


def test_monthly_mid_month_joiner(api, july):
    r = api.get("/analytics/employees/EMP1002/monthly", params={"month": "2026-07"}).json()
    assert (r["working_days"], r["present_days"], r["late_count"], r["total_late_minutes"], r["attendance_pct"]) \
        == (13, 1.5, 1, 30, 11.54)       # 1.5 / 13 * 100 = 11.538...


def test_monthly_no_logs_and_not_joined(api, july):
    r = api.get("/analytics/employees/EMP1003/monthly", params={"month": "2026-07"}).json()
    assert r == {"emp_code": "EMP1003", "month": "2026-07", "working_days": 23, "present_days": 0,
                 "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0,
                 "attendance_pct": 0.0}
    r = api.get("/analytics/employees/EMP1004/monthly", params={"month": "2026-07"}).json()
    assert r["working_days"] == 0 and r["attendance_pct"] is None


def test_monthly_unknown_employee_404(api, july):
    assert api.get("/analytics/employees/EMP0404/monthly", params={"month": "2026-07"}).status_code == 404


# =========================================================================== department summary
def test_department_summary(api, july):
    r = api.get("/analytics/departments/summary", params={"month": "2026-07"})
    assert r.status_code == 200
    assert r.json() == {"month": "2026-07", "items": [
        # EMP1001, EMP1002, EMP1003 (no logs) -> 3; EMP1004 joins in August -> not counted.
        # work hours of present records with a value: 9, 3.5, 5 (Sat), 8, 8.5, 9, 4.25 = 47.25 / 7 = 6.75
        {"department": "Engineering", "headcount": 3, "present_days": 6.0, "avg_work_hours": 6.75,
         "late_count": 4, "total_late_minutes": 120, "leave_count": 1, "on_duty_count": 1},
        # only employee has no logs: still a row with headcount 1
        {"department": "Legal", "headcount": 1, "present_days": 0, "avg_work_hours": None,
         "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0},
        # 9.1 + 8 + 7 + 8 = 32.1 / 4 = 8.025 -> 8.03 half-up (float round() would give 8.02)
        {"department": "Sales", "headcount": 2, "present_days": 5.0, "avg_work_hours": 8.03,
         "late_count": 3, "total_late_minutes": 120, "leave_count": 1, "on_duty_count": 1},
        {"department": "Support", "headcount": 1, "present_days": 1.0, "avg_work_hours": 8.75,
         "late_count": 1, "total_late_minutes": 30, "leave_count": 0, "on_duty_count": 0},
    ]}  # Design (joins in August) is omitted


def test_department_summary_filter(api, july):
    items = api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Legal"}).json()
    assert [i["department"] for i in items["items"]] == ["Legal"]
    assert api.get("/analytics/departments/summary",
                   params={"month": "2026-07", "department": "Design"}).json()["items"] == []


def test_department_summary_empty_month(api, july):
    # January 2026: Engineering EMP1001/EMP1003 and Legal have joined, nobody has logs
    items = api.get("/analytics/departments/summary", params={"month": "2026-01"}).json()["items"]
    assert [(i["department"], i["headcount"], i["present_days"]) for i in items] == [
        ("Engineering", 2, 0), ("Legal", 1, 0)]


# =========================================================================== leaderboard
FULL_BOARD = [
    {"rank": 1, "emp_code": "EMP1001", "name": "Name EMP1001", "department": "Engineering",
     "total_late_minutes": 90, "late_count": 3},
    {"rank": 2, "emp_code": "EMP2001", "name": "Name EMP2001", "department": "Sales",
     "total_late_minutes": 60, "late_count": 1},
    {"rank": 2, "emp_code": "EMP2002", "name": "Name EMP2002", "department": "Sales",
     "total_late_minutes": 60, "late_count": 2},
    {"rank": 4, "emp_code": "EMP1002", "name": "Name EMP1002", "department": "Engineering",
     "total_late_minutes": 30, "late_count": 1},
    {"rank": 4, "emp_code": "EMP3001", "name": "Name EMP3001", "department": "Support",
     "total_late_minutes": 30, "late_count": 1},
]


@pytest.mark.parametrize("limit, n_rows", [(1, 1), (2, 3), (3, 3), (4, 5), (10, 5)])
def test_leaderboard_competition_ranking_and_ties_at_cutoff(api, july, limit, n_rows):
    r = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": limit}).json()
    assert r == {"month": "2026-07", "items": FULL_BOARD[:n_rows]}   # orphan EMP9999 (500 min) ignored


def test_leaderboard_default_limit_and_department(api, july):
    assert api.get("/analytics/leaderboard/late", params={"month": "2026-07"}).json()["items"] == FULL_BOARD
    eng = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "Engineering"}).json()
    assert [(i["rank"], i["emp_code"]) for i in eng["items"]] == [(1, "EMP1001"), (2, "EMP1002")]
    sales = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "Sales"}).json()
    assert [(i["rank"], i["emp_code"]) for i in sales["items"]] == [(1, "EMP2001"), (1, "EMP2002")]
    assert api.get("/analytics/leaderboard/late", params={"month": "2026-05"}).json()["items"] == []


# =========================================================================== trend
def row(date, working, hc, present, late, rate, avg):
    return {"date": date, "is_working_day": working, "headcount": hc, "present_count": present,
            "late_count": late, "attendance_rate": rate, "moving_avg_7d": avg}


def test_trend_engineering_week(api, july):
    r = api.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-11", "to": "2026-07-17"})
    assert r.status_code == 200
    assert r.json() == {"department": "Engineering", "items": [
        row("2026-07-11", False, 2, 1, 1, None, None),        # Sat: record counted, no rate
        row("2026-07-12", False, 2, 0, 0, None, None),        # Sun: no records at all, still a row
        row("2026-07-13", True, 2, 1, 1, 0.5, 0.5),
        row("2026-07-14", True, 2, 1, 0, 0.5, 0.5),
        row("2026-07-15", True, 3, 1, 1, 0.3333, 0.4444),     # EMP1002 joins: headcount 3; (0.5+0.5+0.3333)/3
        row("2026-07-16", True, 3, 0.5, 0, 0.1667, 0.375),    # half day; (1.3333+0.1667)/4 = 0.375
        row("2026-07-17", True, 3, 0, 0, 0.0, 0.3),           # working day with no records -> 0.0; 1.5/5
    ]}


def test_trend_window_slides_and_starts_at_from(api, july):
    items = api.get("/analytics/departments/Engineering/trend",
                    params={"from": "2026-07-14", "to": "2026-07-22"}).json()["items"]
    assert [i["date"] for i in items] == [f"2026-07-{d}" for d in range(14, 23)]
    rates = [i["attendance_rate"] for i in items]
    assert rates == [0.5, 0.3333, 0.1667, 0.0, None, None, 0.0, 0.0, 0.0]
    avgs = [i["moving_avg_7d"] for i in items]
    # 07-14: only itself (07-13 is before `from`) -> 0.5
    # 07-20: rows 14..20 -> (0.5+0.3333+0.1667+0+0)/5 = 0.2 ; 07-21: rows 15..21 -> 0.5/5 = 0.1
    # 07-22: rows 16..22 -> 0.1667/5 = 0.03334 -> 0.0333
    assert avgs[0] == 0.5 and avgs[6:] == [0.2, 0.1, 0.0333]


def test_trend_department_without_logs_and_single_day(api, july):
    items = api.get("/analytics/departments/Legal/trend", params={"from": "2026-07-03", "to": "2026-07-05"}).json()
    assert items["items"] == [row("2026-07-03", True, 1, 0, 0, 0.0, 0.0),
                              row("2026-07-04", False, 1, 0, 0, None, 0.0),
                              row("2026-07-05", False, 1, 0, 0, None, 0.0)]
    one = api.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-13", "to": "2026-07-13"})
    assert one.json()["items"] == [row("2026-07-13", True, 2, 1, 1, 0.5, 0.5)]


def test_trend_headcount_zero_and_errors(api, july):
    items = api.get("/analytics/departments/Design/trend", params={"from": "2026-07-30", "to": "2026-07-31"}).json()
    assert [(i["headcount"], i["attendance_rate"], i["moving_avg_7d"]) for i in items["items"]] == \
        [(0, None, None), (0, None, None)]
    assert api.get("/analytics/departments/Nope/trend",
                   params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    ok = api.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-01", "to": "2026-09-30"})
    assert ok.status_code == 200 and len(ok.json()["items"]) == 92


# =========================================================================== explain
@pytest.mark.parametrize("endpoint, params, collection", [
    ("attendance_list", {}, "attendance_logs"),
    ("attendance_list", {"emp_code": "EMP1001", "date_from": "2026-07-01", "date_to": "2026-07-31"},
     "attendance_logs"),
    ("attendance_list", {"status": "LEAVE", "page": 2, "page_size": 5}, "attendance_logs"),
    ("employee_monthly", {"emp_code": "EMP1001", "month": "2026-07"}, "attendance_logs"),
    ("department_summary", {"month": "2026-07"}, "employees"),
    ("department_summary", {"month": "2026-07", "department": "Sales"}, "employees"),
    ("late_leaderboard", {"month": "2026-07"}, "attendance_logs"),
    ("late_leaderboard", {"month": "2026-07", "department": "Sales", "limit": 3}, "attendance_logs"),
    ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"}, "employees"),
])
def test_explain_uses_index(api, july, endpoint, params, collection):
    r = api.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["endpoint"], body["collection"]) == (endpoint, collection)
    text = str(body["explain"])
    assert "IXSCAN" in text and "COLLSCAN" not in text
    assert "executionStats" in text
