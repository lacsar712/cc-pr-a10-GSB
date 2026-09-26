import os
import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from psycopg.rows import dict_row

DSN = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:54394/printreg")
SECRET = os.environ.get("JWT_SECRET", "print-register-dev-secret")
DEFAULT_CLAIM_TIMEOUT_SECONDS = 30
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)
USERS = {
    "printer": {"role": "writer", "password_hash": pwd.hash("print123456")},
    "checker": {"role": "reader", "password_hash": pwd.hash("check123456")},
}


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
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS claim_seq integer NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS claim_timeout_seconds integer;
CREATE TABLE IF NOT EXISTS settings (
    key text PRIMARY KEY,
    value text NOT NULL
);
CREATE TABLE IF NOT EXISTS reclaim_events (
    id serial PRIMARY KEY,
    job_id integer NOT NULL,
    sheet text NOT NULL,
    claim_seq integer NOT NULL,
    claimed_at timestamptz NOT NULL,
    reclaimed_at timestamptz NOT NULL,
    timeout_seconds integer NOT NULL
);
INSERT INTO settings (key, value) VALUES ('claim_timeout_seconds', '30')
ON CONFLICT (key) DO NOTHING;
"""

# 领取超时回收：落在领取态(running)且超过领取时快照秒数仍未出结论的任务，
# 退回待处理供再次领取，并写入回收流水。秒数以领取那一刻的快照为准，
# 之后改秒数不影响已在领取中的任务。
RECLAIM_SQL = """
WITH expired AS (
    SELECT id, sheet, claim_seq, claimed_at, claim_timeout_seconds
    FROM jobs
    WHERE status = 'running'
      AND claimed_at IS NOT NULL
      AND claim_timeout_seconds IS NOT NULL
      AND now() - claimed_at > make_interval(secs => claim_timeout_seconds)
    FOR UPDATE
),
released AS (
    UPDATE jobs j
    SET status = 'pending', claimed_at = NULL, claim_timeout_seconds = NULL
    FROM expired e
    WHERE j.id = e.id
    RETURNING j.id
)
INSERT INTO reclaim_events (job_id, sheet, claim_seq, claimed_at, reclaimed_at, timeout_seconds)
SELECT id, sheet, claim_seq, claimed_at, now(), claim_timeout_seconds
FROM expired
RETURNING id
"""


def reclaim_expired(conn) -> int:
    return len(conn.execute(RECLAIM_SQL).fetchall())


def get_timeout_seconds(conn) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key = 'claim_timeout_seconds'").fetchone()
    return int(row["value"]) if row else DEFAULT_CLAIM_TIMEOUT_SECONDS


def sweeper():
    while True:
        try:
            with connect() as conn:
                reclaim_expired(conn)
                conn.commit()
        except Exception as exc:  # 数据库短暂不可用时下一轮再试
            print(f"回收扫描失败: {exc}", flush=True)
        time.sleep(0.5)


class LoginIn(BaseModel):
    username: str
    password: str


class JobIn(BaseModel):
    sheet: str
    cyan_mm: float
    magenta_mm: float


class TimeoutIn(BaseModel):
    seconds: int


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
        raise HTTPException(status_code=403, detail="仅印刷员可写")
    return user


app = FastAPI(title="印刷套准复核台")


@app.on_event("startup")
def startup():
    with connect() as conn:
        conn.execute(SCHEMA)
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
    threading.Thread(target=sweeper, daemon=True).start()


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
        reclaim_expired(conn)
        conn.commit()
        return conn.execute(
            "SELECT id, sheet, cyan_mm, magenta_mm, status, verdict, reason, created_by FROM jobs ORDER BY id DESC"
        ).fetchall()


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
def read_claim_timeout(_user: dict = Depends(current_user)):
    with connect() as conn:
        return {"seconds": get_timeout_seconds(conn)}


@app.put("/api/claim-timeout")
def update_claim_timeout(body: TimeoutIn, _user: dict = Depends(require_writer)):
    if not 1 <= body.seconds <= 86400:
        raise HTTPException(status_code=400, detail="秒数需为 1 到 86400 的整数")
    with connect() as conn:
        conn.execute(
            """INSERT INTO settings (key, value) VALUES ('claim_timeout_seconds', %s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
            (str(body.seconds),),
        )
        conn.commit()
    return {"seconds": body.seconds}


@app.get("/api/claiming")
def list_claiming(_user: dict = Depends(current_user)):
    with connect() as conn:
        reclaim_expired(conn)
        conn.commit()
        return conn.execute(
            """SELECT id, sheet, cyan_mm, magenta_mm, claim_seq, claimed_at, claim_timeout_seconds,
                      EXTRACT(EPOCH FROM (now() - claimed_at))::int AS elapsed_seconds
               FROM jobs
               WHERE status = 'running'
               ORDER BY id"""
        ).fetchall()


@app.get("/api/reclaims")
def list_reclaims(_user: dict = Depends(current_user)):
    with connect() as conn:
        reclaim_expired(conn)
        conn.commit()
        return conn.execute(
            """SELECT id, job_id, sheet, claim_seq, claimed_at, reclaimed_at, timeout_seconds
               FROM reclaim_events
               ORDER BY id DESC
               LIMIT 100"""
        ).fetchall()
