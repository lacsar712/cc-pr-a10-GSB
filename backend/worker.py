import os
import time
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from rules import judge

DSN = os.environ["DATABASE_URL"]
DEFAULT_TIMEOUT_SECONDS = 60
REAP_INTERVAL_SECONDS = 1.0


def connect():
    last = None
    for _ in range(40):
        try:
            return psycopg.connect(DSN, row_factory=dict_row)
        except psycopg.OperationalError as exc:
            last = exc
            time.sleep(1)
    raise last


def ensure():
    with connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                id serial PRIMARY KEY,
                sheet text NOT NULL,
                cyan_mm double precision NOT NULL,
                magenta_mm double precision NOT NULL,
                status text NOT NULL,
                verdict text NOT NULL DEFAULT '',
                reason text NOT NULL DEFAULT '',
                created_by text NOT NULL,
                created_at timestamptz NOT NULL
            )"""
        )
        conn.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS claimed_at timestamptz")
        conn.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS timeout_seconds integer")
        conn.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS reclaim_count integer NOT NULL DEFAULT 0")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                key text PRIMARY KEY,
                value integer NOT NULL,
                updated_by text NOT NULL DEFAULT '',
                updated_at timestamptz NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS reclaim_logs (
                id serial PRIMARY KEY,
                job_id integer NOT NULL,
                sheet text NOT NULL,
                claimed_at timestamptz NOT NULL,
                reclaimed_at timestamptz NOT NULL,
                timeout_seconds integer NOT NULL,
                reclaim_no integer NOT NULL
            )"""
        )
        conn.execute(
            """INSERT INTO settings (key, value, updated_by, updated_at)
               VALUES ('claim_timeout_seconds', %s, 'system', %s)
               ON CONFLICT (key) DO NOTHING""",
            (DEFAULT_TIMEOUT_SECONDS, datetime.now(timezone.utc)),
        )
        conn.commit()


def get_timeout(conn) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key = 'claim_timeout_seconds'").fetchone()
    return row["value"] if row else DEFAULT_TIMEOUT_SECONDS


def claim_once(conn):
    # 领取时把当前超时秒数快照到任务上；之后改设置不影响已在领取态的任务
    row = conn.execute(
        """WITH picked AS (
             SELECT id FROM jobs
             WHERE status = 'pending'
             ORDER BY id
             FOR UPDATE SKIP LOCKED
             LIMIT 1
           )
           UPDATE jobs SET status = 'running', claimed_at = %s, timeout_seconds = %s
           FROM picked
           WHERE jobs.id = picked.id
           RETURNING jobs.id, jobs.cyan_mm, jobs.magenta_mm""",
        (datetime.now(timezone.utc), get_timeout(conn)),
    ).fetchone()
    return row


def reclaim_expired(conn) -> int:
    """行锁挑出领取超时的任务，退回待处理并记一条回收流水，返回回收条数。"""
    now = datetime.now(timezone.utc)
    expired = conn.execute(
        """SELECT id, sheet, claimed_at, timeout_seconds, reclaim_count
           FROM jobs
           WHERE status = 'running'
             AND claimed_at IS NOT NULL
             AND claimed_at + make_interval(secs => timeout_seconds) <= %s
           ORDER BY id
           FOR UPDATE SKIP LOCKED""",
        (now,),
    ).fetchall()
    for job in expired:
        reclaim_no = job["reclaim_count"] + 1
        conn.execute(
            "UPDATE jobs SET status = 'pending', reclaim_count = %s WHERE id = %s",
            (reclaim_no, job["id"]),
        )
        conn.execute(
            """INSERT INTO reclaim_logs
               (job_id, sheet, claimed_at, reclaimed_at, timeout_seconds, reclaim_no)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (job["id"], job["sheet"], job["claimed_at"], now, job["timeout_seconds"], reclaim_no),
        )
    return len(expired)


def main():
    ensure()
    next_reap = time.monotonic()
    while True:
        with connect() as conn:
            row = claim_once(conn)
            if row is None:
                conn.commit()
            else:
                verdict, reason = judge(row["cyan_mm"], row["magenta_mm"])
                # 写结论前仍持行锁；若此刻已被回收（退回 pending），不得覆盖状态
                done = conn.execute(
                    "UPDATE jobs SET status = 'done', verdict = %s, reason = %s WHERE id = %s AND status = 'running'",
                    (verdict, reason, row["id"]),
                )
                if done.rowcount == 0:
                    # 已超时退回待处理，本次结论作废，任务等待重新领取
                    conn.rollback()
                else:
                    conn.commit()

            if time.monotonic() >= next_reap:
                reclaimed = reclaim_expired(conn)
                conn.commit()
                if reclaimed:
                    print(f"reclaimed {reclaimed} timed-out job(s)", flush=True)
                next_reap = time.monotonic() + REAP_INTERVAL_SECONDS

        if row is None:
            time.sleep(0.4)


if __name__ == "__main__":
    main()
