import os
import time

import psycopg
from psycopg.rows import dict_row

from rules import judge

DSN = os.environ["DATABASE_URL"]
# 人为拖住领取的秒数：领取后先睡这么久再写结论，用于演示领取超时回收。
HOLD_SECONDS = float(os.environ.get("WORKER_HOLD_SECONDS", "0"))
DEFAULT_CLAIM_TIMEOUT_SECONDS = 30


def connect():
    last = None
    for _ in range(40):
        try:
            return psycopg.connect(DSN, row_factory=dict_row)
        except psycopg.OperationalError as exc:
            last = exc
            time.sleep(1)
    raise last


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


def ensure():
    with connect() as conn:
        conn.execute(SCHEMA)
        conn.commit()


def claim_once(conn):
    # 领取即提交：状态、领取时刻、领取序号与当时的超时秒数快照一起落库，
    # 此后改秒数只约束新落入领取态的任务。
    return conn.execute(
        """WITH picked AS (
             SELECT id FROM jobs
             WHERE status = 'pending'
             ORDER BY id
             FOR UPDATE SKIP LOCKED
             LIMIT 1
           )
           UPDATE jobs
           SET status = 'running',
               claimed_at = now(),
               claim_seq = claim_seq + 1,
               claim_timeout_seconds = COALESCE(
                   (SELECT value::int FROM settings WHERE key = 'claim_timeout_seconds'),
                   %s)
           FROM picked
           WHERE jobs.id = picked.id
           RETURNING jobs.id, jobs.cyan_mm, jobs.magenta_mm, jobs.claim_seq""",
        (DEFAULT_CLAIM_TIMEOUT_SECONDS,),
    ).fetchone()


def finish(conn, row, verdict, reason):
    # 只认自己这次领取：若任务已被回收退回待处理（甚至被别人重新领走），
    # claim_seq 或状态已对不上，结论直接丢弃。
    cur = conn.execute(
        """UPDATE jobs
           SET status = 'done', verdict = %s, reason = %s
           WHERE id = %s AND status = 'running' AND claim_seq = %s""",
        (verdict, reason, row["id"], row["claim_seq"]),
    )
    return cur.rowcount


def main():
    ensure()
    while True:
        with connect() as conn:
            row = claim_once(conn)
            conn.commit()
        if row is None:
            time.sleep(0.4)
            continue
        if HOLD_SECONDS > 0:
            print(f"任务 {row['id']} 第 {row['claim_seq']} 次领取，人为拖住 {HOLD_SECONDS} 秒", flush=True)
            time.sleep(HOLD_SECONDS)
        verdict, reason = judge(row["cyan_mm"], row["magenta_mm"])
        with connect() as conn:
            written = finish(conn, row, verdict, reason)
            conn.commit()
        if written == 0:
            print(f"任务 {row['id']} 第 {row['claim_seq']} 次领取已被回收，结论丢弃", flush=True)


if __name__ == "__main__":
    main()
