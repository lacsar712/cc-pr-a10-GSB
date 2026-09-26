import os
from datetime import datetime, timedelta, timezone

import psycopg
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from psycopg.rows import dict_row

DSN = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:54394/printreg")
SECRET = os.environ.get("JWT_SECRET", "print-register-dev-secret")
DEFAULT_TIMEOUT_SECONDS = 60
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)
USERS = {
    "printer": {"role": "writer", "password_hash": pwd.hash("print123456")},
    "checker": {"role": "reader", "password_hash": pwd.hash("check123456")},
}

STATUS_LABELS = {"pending": "待处理", "running": "领取中", "done": "已复核"}


def connect():
    return psycopg.connect(DSN, row_factory=dict_row)


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id serial PRIMARY KEY,
    sheet text NOT NULL,
    cyan_mm double precision NOT NULL,
    magenta_mm double precision NOT NULL,
    status text NOT NULL,
    verdict text NOT NULL DEFAULT '',
    reason text NOT NULL DEFAULT '',
    created_by text NOT NULL,
    created_at timestamptz NOT NULL
);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS claimed_at timestamptz;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS timeout_seconds integer;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS reclaim_count integer NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS settings (
    key text PRIMARY KEY,
    value integer NOT NULL,
    updated_by text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS reclaim_logs (
    id serial PRIMARY KEY,
    job_id integer NOT NULL,
    sheet text NOT NULL,
    claimed_at timestamptz NOT NULL,
    reclaimed_at timestamptz NOT NULL,
    timeout_seconds integer NOT NULL,
    reclaim_no integer NOT NULL
);
"""


class LoginIn(BaseModel):
    username: str
    password: str


class JobIn(BaseModel):
    sheet: str
    cyan_mm: float
    magenta_mm: float


class TimeoutIn(BaseModel):
    timeout_seconds: int = Field(ge=1, le=86400)


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="无效令牌") from exc
    if payload.get("sub") not in USERS:
        raise HTTPException(status_code=401, detail="无效令牌")
    return {"username": payload["sub"], "role": payload.get("role")}


def require_writer(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "writer":
        raise HTTPException(status_code=403, detail="仅印刷员可操作")
    return user


def get_timeout(conn) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key = 'claim_timeout_seconds'").fetchone()
    return row["value"] if row else DEFAULT_TIMEOUT_SECONDS


app = FastAPI(title="印刷套准复核台")


@app.on_event("startup")
def startup():
    with connect() as conn:
        conn.execute(SCHEMA)
        conn.execute(
            """INSERT INTO settings (key, value, updated_by, updated_at)
               VALUES ('claim_timeout_seconds', %s, 'system', %s)
               ON CONFLICT (key) DO NOTHING""",
            (DEFAULT_TIMEOUT_SECONDS, datetime.now(timezone.utc)),
        )
        n = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        if n == 0:
            now = datetime.now(timezone.utc)
            conn.execute(
                """INSERT INTO jobs (sheet, cyan_mm, magenta_mm, status, verdict, reason, created_by, created_at)
                   VALUES
                   ('封面-01', 0.05, -0.04, 'pending', '', '', 'printer', %s),
                   ('内页-09', 0.40, 0.02, 'pending', '', '', 'printer', %s)""",
                (now, now),
            )
        conn.commit()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "print-register-review"}


@app.post("/api/auth/login")
def login(body: LoginIn):
    user = USERS.get(body.username.strip())
    if not user or not pwd.verify(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode({"sub": body.username.strip(), "role": user["role"], "exp": exp}, SECRET, algorithm="HS256")
    return {"access_token": token, "username": body.username.strip(), "role": user["role"]}


@app.get("/api/jobs")
def list_jobs(_user: dict = Depends(current_user)):
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, sheet, cyan_mm, magenta_mm, status, verdict, reason, created_by,
                      claimed_at, timeout_seconds, reclaim_count
               FROM jobs ORDER BY id DESC"""
        ).fetchall()
    for row in rows:
        row["status_text"] = STATUS_LABELS.get(row["status"], row["status"])
    return rows


@app.post("/api/jobs", status_code=202)
def enqueue(body: JobIn, user: dict = Depends(require_writer)):
    with connect() as conn:
        row = conn.execute(
            """INSERT INTO jobs (sheet, cyan_mm, magenta_mm, status, created_by, created_at)
               VALUES (%s, %s, %s, 'pending', %s, %s)
               RETURNING id, sheet, status, verdict""",
            (body.sheet.strip(), body.cyan_mm, body.magenta_mm, user["username"], datetime.now(timezone.utc)),
        ).fetchone()
        conn.commit()
    return row


@app.get("/api/claim-timeout")
def get_claim_timeout(_user: dict = Depends(current_user)):
    with connect() as conn:
        return {"timeout_seconds": get_timeout(conn)}


@app.put("/api/claim-timeout")
def update_claim_timeout(body: TimeoutIn, user: dict = Depends(require_writer)):
    # 改秒数只约束此后新落入领取态的任务：进行中的任务仍按领取时快照的秒数计时
    with connect() as conn:
        conn.execute(
            """INSERT INTO settings (key, value, updated_by, updated_at)
               VALUES ('claim_timeout_seconds', %s, %s, %s)
               ON CONFLICT (key) DO UPDATE SET
                 value = EXCLUDED.value,
                 updated_by = EXCLUDED.updated_by,
                 updated_at = EXCLUDED.updated_at""",
            (body.timeout_seconds, user["username"], datetime.now(timezone.utc)),
        )
        conn.commit()
    return {"timeout_seconds": body.timeout_seconds}


@app.get("/api/reclaim-logs")
def list_reclaim_logs(_user: dict = Depends(current_user)):
    with connect() as conn:
        return conn.execute(
            """SELECT id, job_id, sheet, claimed_at, reclaimed_at, timeout_seconds, reclaim_no
               FROM reclaim_logs ORDER BY id DESC LIMIT 100"""
        ).fetchall()


@app.post("/api/jobs/stall", status_code=202)
def stall_job(body: JobIn, user: dict = Depends(require_writer)):
    """演示用：模拟另一进程领走任务后被拖住——直接落入领取态但永不写结论，
    直到超时回收把它退回待处理。"""
    with connect() as conn:
        timeout_seconds = get_timeout(conn)
        now = datetime.now(timezone.utc)
        row = conn.execute(
            """INSERT INTO jobs
                 (sheet, cyan_mm, magenta_mm, status, created_by, created_at, claimed_at, timeout_seconds)
               VALUES (%s, %s, %s, 'running', %s, %s, %s, %s)
               RETURNING id, sheet, status, claimed_at, timeout_seconds""",
            (body.sheet.strip(), body.cyan_mm, body.magenta_mm, user["username"], now, now, timeout_seconds),
        ).fetchone()
        conn.commit()
    return row
