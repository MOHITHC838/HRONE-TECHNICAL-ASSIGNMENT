"""Employee Attendance & Analytics API."""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, tzinfo=UTC
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

def create_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employee_code_unique")
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined_on"
    )
    db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined_on")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="attendance_employee_date_unique",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_employee_punch_in"
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)],
        name="attendance_late_ranking",
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def _parse_date(value: str, label: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{label} must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(422, f"{label} must be YYYY-MM-DD")
    return parsed


def _validate_month(value: str) -> tuple[date, date]:
    try:
        year, month = map(int, value.split("-"))
        if len(value) != 7 or date(year, month, 1).strftime("%Y-%m") != value:
            raise ValueError
    except (AttributeError, ValueError):
        raise HTTPException(422, "month must be YYYY-MM")
    start = date(year, month, 1)
    if month == 12:
        following = date(year + 1, 1, 1)
    else:
        following = date(year, month + 1, 1)
    return start, following


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _truncate_seconds(value: datetime) -> datetime:
    return _utc_datetime(value).replace(microsecond=0)


def _epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    elapsed = _utc_datetime(value) - datetime(1970, 1, 1, tzinfo=UTC)
    return elapsed.days * 86_400_000 + elapsed.seconds * 1_000 + elapsed.microseconds // 1_000


def _from_epoch_ms(value: int) -> datetime:
    if isinstance(value, bool) or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(422, "timestamp must be epoch milliseconds in the supported range")
    try:
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        return _truncate_seconds(epoch + timedelta(milliseconds=value))
    except (OverflowError, OSError, ValueError):
        raise HTTPException(422, "timestamp is out of range")


def _hour_minute(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def _shift_start(attendance_day: str, employee: dict) -> datetime:
    hour, minute = _hour_minute(employee["shift_start"])
    day = _parse_date(attendance_day)
    return datetime.combine(day, time(hour, minute), IST)


def _shift_end(attendance_day: str, employee: dict) -> datetime:
    hour, minute = _hour_minute(employee["shift_end"])
    start = _hour_minute(employee["shift_start"])
    day = _parse_date(attendance_day)
    if (hour, minute) <= start:
        day += timedelta(days=1)
    return datetime.combine(day, time(hour, minute), IST)


def _attendance_day(punch_in: datetime, employee: dict) -> str:
    local = _utc_datetime(punch_in).astimezone(IST)
    shift_end = _hour_minute(employee["shift_end"])
    shift_start = _hour_minute(employee["shift_start"])
    day = local.date()
    if shift_end <= shift_start and (local.hour, local.minute) < shift_end:
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in: datetime, attendance_day: str, employee: dict) -> int:
    start = _shift_start(attendance_day, employee)
    elapsed = int((_utc_datetime(punch_in).astimezone(IST) - start).total_seconds())
    return elapsed // 60 if elapsed > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((_utc_datetime(punch_out) - _utc_datetime(punch_in)).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, attendance_day: str, employee: dict) -> int:
    elapsed = int((_utc_datetime(punch_out).astimezone(IST) - _shift_end(attendance_day, employee)).total_seconds())
    minutes = elapsed // 60
    return minutes if minutes >= 30 else 0


def _round_number(value, places: int = 2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _serialize_attendance(doc: dict) -> dict:
    result = {key: value for key, value in doc.items() if key != "_id"}
    result.setdefault("punch_in", None)
    result.setdefault("punch_out", None)
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    result.setdefault("history", [])
    result["punch_in"] = _epoch_ms(result["punch_in"])
    result["punch_out"] = _epoch_ms(result["punch_out"])
    history = []
    for entry in result["history"]:
        converted = dict(entry)
        converted["at"] = _epoch_ms(converted.get("at"))
        changes = {}
        for field, change in converted.get("changes", {}).items():
            values = dict(change)
            if field in ("punch_in", "punch_out"):
                values["from"] = _epoch_ms(values.get("from"))
                values["to"] = _epoch_ms(values.get("to"))
            changes[field] = values
        converted["changes"] = changes
        history.append(converted)
    result["history"] = history
    return result


def _month_match(month: str) -> dict:
    start, following = _validate_month(month)
    return {"$gte": start.isoformat(), "$lt": following.isoformat()}


def _employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    start, following = _validate_month(month)
    start_s, following_s = start.isoformat(), following.isoformat()
    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [
                        {"$eq": ["$emp_code", "$$code"]},
                        {"$gte": ["$date", start_s]},
                        {"$lt": ["$date", following_s]},
                    ]}}},
                    {"$addFields": {"_day": {"$dateFromString": {"dateString": "$date"}}}},
                    {"$addFields": {"_weekday": {"$dayOfWeek": "$_day"}}},
                    {"$group": {"_id": None,
                        "present_days": {"$sum": {"$cond": [
                            {"$and": [
                                {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                {"$not": [{"$in": ["$_weekday", [1, 7]]}]},
                            ]},
                            {"$cond": ["$half_day", 0.5, 1]}, 0,
                        ]}},
                        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                    }},
                ],
                "as": "_stats",
            }
        },
        {"$addFields": {"_working_start": {"$cond": [
            {"$gt": ["$joined_on", start_s]},
            {"$dateFromString": {"dateString": "$joined_on"}},
            {"$dateFromString": {"dateString": start_s}},
        ]}}},
        {"$addFields": {"_working_days": {"$size": {"$filter": {
            "input": {"$map": {"input": {"$range": [0, {"$dateDiff": {
                "startDate": "$_working_start",
                "endDate": {"$dateFromString": {"dateString": following_s}},
                "unit": "day",
            }}]}, "as": "offset", "in": {"$dateAdd": {
                "startDate": "$_working_start", "unit": "day", "amount": "$$offset",
            }}}},
            "as": "day", "cond": {"$not": [{"$in": [{"$dayOfWeek": "$$day"}, [1, 7]]}]},
        }}}}},
        {"$project": {
            "_id": 0, "emp_code": 1, "working_days": "$_working_days",
            "present_days": {"$ifNull": [{"$arrayElemAt": ["$_stats.present_days", 0]}, 0]},
            "leave_days": {"$ifNull": [{"$arrayElemAt": ["$_stats.leave_days", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$_stats.late_count", 0]}, 0]},
            "total_late_minutes": {"$ifNull": [{"$arrayElemAt": ["$_stats.total_late_minutes", 0]}, 0]},
            "total_overtime_minutes": {"$ifNull": [{"$arrayElemAt": ["$_stats.total_overtime_minutes", 0]}, 0]},
        }},
    ]


def _department_summary_pipeline(month: str, department: Optional[str]) -> list[dict]:
    start, following = _validate_month(month)
    match = {"joined_on": {"$lte": (following - timedelta(days=1)).isoformat()}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", start.isoformat()]},
                    {"$lt": ["$date", following.isoformat()]},
                ]}}},
                {"$addFields": {"_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
            ], "as": "_logs",
        }},
        {"$addFields": {"_present_days": {"$sum": {"$map": {
            "input": {"$filter": {"input": "$_logs", "as": "log", "cond": {"$in": ["$$log.status", list(PRESENCE_STATUSES)]}}},
            "as": "log", "in": {"$cond": [
                {"$not": [{"$in": ["$$log._weekday", [1, 7]]}]},
                {"$cond": ["$$log.half_day", 0.5, 1]}, 0,
            ]},
        }}}}},
        {"$addFields": {
            "_worked_logs": {"$filter": {"input": "$_logs", "as": "log", "cond": {"$and": [
                {"$in": ["$$log.status", list(PRESENCE_STATUSES)]}, {"$ne": ["$$log.work_hours", None]},
            ]}}},
            "_late_logs": {"$filter": {"input": "$_logs", "as": "log", "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}}},
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$_present_days"},
            "_work_hours_sum": {"$sum": {"$sum": {"$map": {
                "input": "$_worked_logs", "as": "log", "in": "$$log.work_hours"
            }}}},
            "_work_hours_count": {"$sum": {"$size": "$_worked_logs"}},
            "late_count": {"$sum": {"$size": "$_late_logs"}},
            "total_late_minutes": {"$sum": {"$sum": {"$map": {
                "input": "$_logs", "as": "log", "in": {"$ifNull": ["$$log.late_minutes", 0]}
            }}}},
            "leave_count": {"$sum": {"$size": {"$filter": {
                "input": "$_logs", "as": "log", "cond": {"$eq": ["$$log.status", "LEAVE"]}
            }}}},
            "on_duty_count": {"$sum": {"$size": {"$filter": {
                "input": "$_logs", "as": "log", "cond": {"$eq": ["$$log.status", "ON_DUTY"]}
            }}}},
        }},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1,
            "present_days": 1, "late_count": 1, "total_late_minutes": 1,
            "leave_count": 1, "on_duty_count": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": ["$_work_hours_count", 0]},
                {"$divide": ["$_work_hours_sum", "$_work_hours_count"]}, None,
            ]},
        }},
        {"$sort": {"department": 1}},
    ]


def _leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list[dict]:
    start, following = _validate_month(month)
    lookup_match = []
    if department is not None:
        lookup_match.append({"$match": {"$expr": {"$eq": ["$department", department]}}})
    return [
        {"$match": {"date": {"$gte": start.isoformat(), "$lt": following.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$emp_code", "$$code"]}}}, *lookup_match,
            {"$project": {"_id": 0, "name": 1, "department": 1}},
        ], "as": "_employee"}},
        {"$unwind": "$_employee"},
        {"$group": {"_id": "$emp_code", "name": {"$first": "$_employee.name"},
            "department": {"$first": "$_employee.department"},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": 1}}},
        {"$sort": {"total_late_minutes": -1}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": 1, "department": 1,
            "total_late_minutes": 1, "late_count": 1}},
    ]


def _department_trend_pipeline(department: str, start: date, end: date) -> list[dict]:
    start_dt = datetime.combine(start, time.min, UTC)
    end_exclusive = datetime.combine(end + timedelta(days=1), time.min, UTC)
    start_s, end_s = start.isoformat(), end.isoformat()
    return [
        {"$match": {"department": department}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lte": ["$date", end_s]},
            ]}}},
        ], "as": "_logs"}},
        {"$addFields": {"_days": {"$map": {"input": {"$range": [0, (end - start).days + 1]}, "as": "offset", "in": {
            "$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$offset"}
        }}}}},
        {"$unwind": "$_days"},
        {"$addFields": {"_date_s": {"$dateToString": {"date": "$_days", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        {"$match": {"$expr": {"$lte": ["$joined_on", "$_date_s"]}}},
        {"$addFields": {"_day_logs": {"$filter": {"input": "$_logs", "as": "log", "cond": {"$eq": ["$$log.date", "$_date_s"]}}}}},
        {"$project": {"date": "$_days", "headcount": {"$literal": 1},
            "present_count": {"$sum": {"$map": {
                "input": {"$filter": {"input": "$_day_logs", "as": "log", "cond": {"$in": ["$$log.status", list(PRESENCE_STATUSES)]}}},
                "as": "log", "in": {"$cond": ["$$log.half_day", 0.5, 1]},
            }}},
            "late_count": {"$size": {"$filter": {"input": "$_day_logs", "as": "log", "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}}}},
        }},
        {"$unionWith": {"pipeline": [
            {"$documents": [{}]},
            {"$project": {"_days": {"$map": {"input": {"$range": [0, (end - start).days + 1]}, "as": "offset", "in": {
                "$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$offset"}
            }}}, "headcount": {"$literal": 0}, "present_count": {"$literal": 0}, "late_count": {"$literal": 0}}},
            {"$unwind": "$_days"}, {"$project": {"date": "$_days", "headcount": 1, "present_count": 1, "late_count": 1}},
        ]}},
        {"$group": {"_id": "$date", "headcount": {"$sum": "$headcount"},
            "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$project": {"_id": 0, "date": "$_id", "headcount": 1, "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_exclusive]}}},
        {"$addFields": {"headcount": {"$ifNull": ["$headcount", 0]},
            "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$not": [{"$in": [{"$dayOfWeek": "$date"}, [1, 7]]}]}}},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None,
        ]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}
        }}},
        {"$sort": {"date": 1}},
        {"$project": {"_id": 0, "date": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d", "timezone": "UTC"}},
            "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_join_date(cls, value: str) -> str:
        _parse_date(value, "joined_on")
        return value

    @model_validator(mode="after")
    def distinct_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]]=None
    punch_in: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("reason", "regularized_by")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def supplied_times_must_be_instants(self):
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status must be a supported attendance status")
        if "punch_in" in self.model_fields_set and self.punch_in is None:
            raise ValueError("punch_in must be epoch milliseconds")
        if "punch_out" in self.model_fields_set and self.punch_out is None:
            raise ValueError("punch_out must be epoch milliseconds")
        return self


@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    now = datetime.now(UTC)
    doc["created_at"] = now.replace(microsecond=(now.microsecond // 1_000) * 1_000)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = _epoch_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    items = list(db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    for employee in items:
        employee["created_at"] = _epoch_ms(employee.get("created_at"))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    timestamp = _from_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    attendance_day = _attendance_day(timestamp, employee)
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "status": body.status,
        "punch_in": timestamp,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(timestamp, attendance_day, employee),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return _serialize_attendance(doc)


def _attendance_filter(emp_code, date_from, date_to, status) -> dict:
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from is not None:
        _parse_date(date_from, "date_from")
    if date_to is not None:
        _parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    query = _attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    docs = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [_serialize_attendance(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    timestamp = _from_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": timestamp}},
        sort=[("punch_in", DESCENDING), ("date", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")
    punch_in_at = _utc_datetime(record["punch_in"])
    duration = (timestamp - punch_in_at).total_seconds()
    if duration <= 0 or duration > 24 * 60 * 60:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    work_hours = compute_work_hours(punch_in_at, timestamp)
    overtime = compute_overtime(timestamp, record["date"], employee)
    changes = {"punch_out": timestamp, "work_hours": work_hours, "overtime_minutes": overtime, "half_day": work_hours < 4.50}
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None}, {"$set": changes}
    )
    if result.modified_count != 1:
        raise HTTPException(409, "record is already punched out")
    record.update(changes)
    return _serialize_attendance(record)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    attendance_date = date
    _parse_date(attendance_date)
    employee = db.employees.find_one({"emp_code": emp_code})
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": attendance_date})
    if employee is None or record is None:
        raise HTTPException(404, "employee or attendance record not found")
    supplied = body.model_fields_set
    if body.status in ("ABSENT", "LEAVE") and ("punch_in" in supplied or "punch_out" in supplied):
        raise HTTPException(422, "ABSENT and LEAVE cannot include punch times")
    old = {
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    new = dict(old)
    if "status" in supplied:
        new["status"] = body.status
    if new["status"] in ("ABSENT", "LEAVE"):
        new.update(punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
    else:
        if "punch_in" in supplied:
            new["punch_in"] = _from_epoch_ms(body.punch_in) if body.punch_in is not None else None
        if "punch_out" in supplied:
            new["punch_out"] = _from_epoch_ms(body.punch_out) if body.punch_out is not None else None
        if new["punch_in"] is None:
            raise HTTPException(422, "presence status requires punch_in")
        if _attendance_day(new["punch_in"], employee) != attendance_date:
            raise HTTPException(422, "punch_in must belong to the attendance date")
        if new["punch_out"] is not None:
            duration = (new["punch_out"] - new["punch_in"]).total_seconds()
            if duration <= 0 or duration > 24 * 60 * 60:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
            new["work_hours"] = compute_work_hours(new["punch_in"], new["punch_out"])
            new["overtime_minutes"] = compute_overtime(new["punch_out"], attendance_date, employee)
            new["half_day"] = new["work_hours"] < 4.50
        else:
            new["work_hours"] = None
            new["overtime_minutes"] = 0
            new["half_day"] = False
        new["late_minutes"] = compute_late_minutes(new["punch_in"], attendance_date, employee)

    changed_fields = [field for field in new if old[field] != new[field]]
    if not changed_fields:
        raise HTTPException(422, "correction does not change the record")
    changes = {field: {"from": old[field], "to": new[field]} for field in changed_fields}
    entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by,
        "reason": body.reason, "changes": changes}
    query = {"_id": record["_id"]}
    for field in ("status", "punch_in", "punch_out"):
        if field in record:
            query[field] = record[field]
        else:
            query[field] = {"$exists": False}
    result = db.attendance_logs.update_one(query, {"$set": new, "$push": {"history": entry}})
    if result.modified_count != 1:
        raise HTTPException(409, "record changed during correction; retry")
    record.update(new)
    record.setdefault("history", []).append(entry)
    return _serialize_attendance(record)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    _validate_month(month)
    if db.employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
        raise HTTPException(404, "employee not found")
    result = next(db.employees.aggregate(_employee_monthly_pipeline(emp_code, month)), None)
    working_days = result["working_days"]
    attendance_pct = _round_number(result["present_days"] / working_days * 100, 2) if working_days else None
    result["month"] = month
    result["present_days"] = _round_number(result["present_days"])
    result["attendance_pct"] = attendance_pct
    return result


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None
):
    _validate_month(month)
    items = list(db.employees.aggregate(_department_summary_pipeline(month, department)))
    for item in items:
        item["present_days"] = _round_number(item["present_days"])
        item["avg_work_hours"] = _round_number(item["avg_work_hours"])
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    _validate_month(month)
    items = list(db.attendance_logs.aggregate(_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(alias="from"), to_date: str = Query(alias="to")):
    start, end = _parse_date(from_date, "from"), _parse_date(to_date, "to")
    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(422, "trend range must be ordered and no longer than 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    pipeline = _department_trend_pipeline(department, start, end)
    items = list(db.employees.aggregate(pipeline, allowDiskUse=True))
    for item in items:
        item["attendance_rate"] = _round_number(item["attendance_rate"], 4)
        item["moving_avg_7d"] = _round_number(item["moving_avg_7d"], 4)
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from:
            _parse_date(date_from, "date_from")
        if date_to:
            _parse_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
        command = {"find": "attendance_logs", "filter": _attendance_filter(emp_code, date_from, date_to, status),
            "sort": {"date": DESCENDING, "emp_code": ASCENDING}, "skip": (page - 1) * page_size, "limit": page_size}
        collection = "attendance_logs"
        explain = db.command("explain", command, verbosity="executionStats")
    elif endpoint == "employee_monthly":
        if not emp_code or month is None:
            raise HTTPException(422, "emp_code and month are required")
        _validate_month(month)
        collection = "employees"
        explain = db.command("explain", {"aggregate": "employees", "pipeline": _employee_monthly_pipeline(emp_code, month), "cursor": {}}, verbosity="executionStats")
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(422, "month is required")
        _validate_month(month)
        collection = "employees"
        explain = db.command("explain", {"aggregate": "employees", "pipeline": _department_summary_pipeline(month, department), "cursor": {}}, verbosity="executionStats")
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(422, "month is required")
        _validate_month(month)
        collection = "attendance_logs"
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": _leaderboard_pipeline(month, limit, department), "cursor": {}}, verbosity="executionStats")
    else:
        if not department or from_date is None or to_date is None:
            raise HTTPException(422, "department, from and to are required")
        start, end = _parse_date(from_date, "from"), _parse_date(to_date, "to")
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, "trend range must be ordered and no longer than 92 days")
        collection = "employees"
        explain = db.command("explain", {"aggregate": "employees", "pipeline": _department_trend_pipeline(department, start, end), "cursor": {}}, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": collection, "explain": json_util.loads(json_util.dumps(explain))}