# Employee Attendance & Analytics API

FastAPI service for employee records, attendance punch-in/out, manual corrections, and monthly/department analytics. MongoDB stores employee and attendance documents; report calculations run in MongoDB aggregation pipelines. The implementation follows `openapi.yaml` and the storage rules in `DATA_MODEL.md`.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer (MongoDB 7.0 is used by the assessment)

## Setup

Create a virtual environment and install runtime and test dependencies.

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` if MongoDB is not available at the local default:

```dotenv
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

You can run MongoDB locally or use an existing MongoDB 6.0+ deployment. The application reads these variables at startup. Do not commit `.env` or put credentials in source control.

## Seed Sample Data

With the virtual environment active and MongoDB reachable:

```bash
python sample_seed.py
```

The script replaces documents in the `employees` and `attendance_logs` collections of the configured database. Use a disposable database if it contains data you need to keep.

## Run

```bash
python -m uvicorn app.main:app --port 8000 --reload
```

Interactive API documentation is available at <http://localhost:8000/docs>. `GET /health` pings MongoDB and returns 503 when it is unavailable. The application creates its indexes idempotently during startup.

## Endpoints

- `GET /health`
- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

Instants use epoch milliseconds at the HTTP boundary and UTC BSON datetimes in MongoDB. Shift calculations use IST. Analytics use the stored derived attendance fields and MongoDB aggregations; pagination is applied by MongoDB before returning results.

## Tests

Run the default suite without MongoDB credentials:

```bash
python -m pytest -q
```

The API tests use mocked collections. MongoDB integration tests are skipped unless `MONGO_TEST_URI` is explicitly set; they create uniquely named temporary databases and drop only those databases afterward.

PowerShell:

```powershell
$env:MONGO_TEST_URI = "mongodb://localhost:27017"
python -m pytest tests/test_mongo_integration.py -q
```

macOS/Linux:

```bash
MONGO_TEST_URI=mongodb://localhost:27017 python -m pytest tests/test_mongo_integration.py -q
```

The integration suite verifies required indexes, concurrent employee creation and punch-in/punch-out conflicts, monthly metrics across join dates/weekends/empty logs, department headcount, tied ranks, trend gaps/moving averages, and actual indexed explain plans.

## Scope and Limitations

There is no authentication layer or holiday calendar in this assignment. Working days are Monday through Friday, shifts use fixed IST, trend requests are limited to 92 calendar days, and explain responses expose MongoDB execution statistics without connection credentials.