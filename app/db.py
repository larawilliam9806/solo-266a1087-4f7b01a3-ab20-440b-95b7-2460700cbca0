"""SQLite 连接、建表与启动自愈。

并发模型：
- 写操作由 services 层的进程内 RLock 串行化；
- SQLite 开启 WAL + busy_timeout，配合 ``BEGIN IMMEDIATE`` 事务；
- 每次上传在同一个事务内完成“替换源数据 + 重算结论”，任何步骤失败整体
  ROLLBACK，旧数据与旧结论原样保留，绝不会出现源数据已换而结论仍旧的混版。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import config

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 每个 (kind, file_date) 保留所见的最新版号，文件按版号整体替换。
CREATE TABLE IF NOT EXISTS source_versions (
    kind        TEXT NOT NULL CHECK (kind IN ('plan', 'landing', 'link')),
    file_date   TEXT NOT NULL,
    version     INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (kind, file_date)
);

CREATE TABLE IF NOT EXISTS plan_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_date       TEXT NOT NULL,
    version         INTEGER NOT NULL,
    flight_no       TEXT NOT NULL,
    destination     TEXT NOT NULL,
    sched_arr_min   INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('normal', 'cancelled')),
    UNIQUE (plan_date, version, flight_no)
);

CREATE TABLE IF NOT EXISTS landing_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    landing_date    TEXT NOT NULL,
    plan_date       TEXT NOT NULL,
    version         INTEGER NOT NULL,
    flight_no       TEXT NOT NULL,
    actual_airport  TEXT NOT NULL,
    actual_arr_min  INTEGER NOT NULL
);

-- 按后序实际起飞 UTC 日整批上传的衔接清单，每条是一个机号的两段航班。
CREATE TABLE IF NOT EXISTS link_entries (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    file_date        TEXT NOT NULL,   -- 后序实际起飞 UTC 日（上传日）
    version          INTEGER NOT NULL,
    aircraft         TEXT NOT NULL,
    prev_plan_date   TEXT NOT NULL,
    prev_flight_no   TEXT NOT NULL,
    next_plan_date   TEXT NOT NULL,
    next_flight_no   TEXT NOT NULL,
    next_dep_airport TEXT NOT NULL,
    next_dep_min     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plan_lookup
    ON plan_entries (plan_date, flight_no);
CREATE INDEX IF NOT EXISTS idx_landing_plan_date
    ON landing_entries (plan_date, flight_no);
CREATE INDEX IF NOT EXISTS idx_landing_file_date
    ON landing_entries (landing_date);
CREATE INDEX IF NOT EXISTS idx_link_file_date
    ON link_entries (file_date, next_flight_no);
CREATE INDEX IF NOT EXISTS idx_link_prev
    ON link_entries (prev_plan_date, prev_flight_no);

-- 每个 (计划起飞日, 航班) 一行结论；源清单不齐全时该日没有任何结论行。
-- source_versions 记录结论所用的三个版号，杜绝混版。
CREATE TABLE IF NOT EXISTS arrival_facts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_date        TEXT NOT NULL,
    flight_no        TEXT NOT NULL,
    fact_type        TEXT NOT NULL CHECK (fact_type IN
                         ('arrived', 'cancelled', 'diverted', 'review')),
    sched_airport    TEXT,
    actual_airport   TEXT,
    delay_min        INTEGER,
    review_reasons   TEXT,          -- JSON 数组，仅 fact_type='review' 时有值
    landing_count    INTEGER NOT NULL DEFAULT 0,
    plan_version     INTEGER NOT NULL,
    landing_d0_ver   INTEGER NOT NULL,
    landing_d1_ver   INTEGER NOT NULL,
    UNIQUE (plan_date, flight_no)
);

-- 每个 (后序实际起飞日, 后序航班) 至多一条过站结果；同一航班至多作一次
-- 后序，因此文件内与跨文件都唯一。所有源清单齐全时该日有结果行，
-- 缺清单时该日不产出任何结果行。
-- 每条结果记录生成它所用的全部源版号，杜绝混版。
CREATE TABLE IF NOT EXISTS turnaround_facts (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    dep_date                TEXT NOT NULL,          -- 后序实际起飞 UTC 日
    next_flight_no          TEXT NOT NULL,
    aircraft                TEXT NOT NULL,
    prev_plan_date          TEXT NOT NULL,
    prev_flight_no          TEXT NOT NULL,
    next_plan_date          TEXT NOT NULL,
    next_dep_airport        TEXT NOT NULL,
    next_dep_min            INTEGER NOT NULL,
    prev_arr_min            INTEGER,                -- 前序实际落地分钟（相对前序计划日）
    turnaround_min          INTEGER,                -- 实际过站分钟，仅 confirmed 有值
    status                  TEXT NOT NULL CHECK (status IN ('confirmed', 'review')),
    review_reasons          TEXT,                   -- JSON 数组，仅 review 有值
    missing_sources         TEXT,                   -- JSON 数组，缺清单单独标记
    prev_plan_version       INTEGER,
    prev_landing_d0_ver     INTEGER,
    prev_landing_d1_ver     INTEGER,
    link_version            INTEGER NOT NULL,
    UNIQUE (dep_date, next_flight_no)
);
"""


def connect(db_file: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_file) if db_file is not None else config.db_path()
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_file: Path | str | None = None) -> None:
    conn = connect(db_file)
    try:
        conn.executescript(SCHEMA_SQL)
        _migrate_source_kinds(conn)
    finally:
        conn.close()


def _migrate_source_kinds(conn: sqlite3.Connection) -> None:
    """旧库的 source_versions CHECK 只允许 plan/landing，补上 link。

    SQLite 无法直接改 CHECK 约束，按官方推荐的“建新表 → 拷数据 → 换名”方式重建。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='source_versions'"
    ).fetchone()
    if row is None or "'link'" in (row["sql"] or ""):
        return
    with immediate_tx(conn):
        conn.execute(
            """
            CREATE TABLE source_versions_new (
                kind        TEXT NOT NULL CHECK (kind IN ('plan', 'landing', 'link')),
                file_date   TEXT NOT NULL,
                version     INTEGER NOT NULL,
                uploaded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                PRIMARY KEY (kind, file_date)
            )
            """
        )
        conn.execute(
            "INSERT INTO source_versions_new (kind, file_date, version, uploaded_at) "
            "SELECT kind, file_date, version, uploaded_at FROM source_versions"
        )
        conn.execute("DROP TABLE source_versions")
        conn.execute("ALTER TABLE source_versions_new RENAME TO source_versions")


@contextmanager
def immediate_tx(conn: sqlite3.Connection):
    """立即拿写锁的事务，失败自动回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


@contextmanager
def read_snapshot(conn: sqlite3.Connection):
    """只读事务：多次查询看到同一快照。

    WAL 下读不阻塞写；更新提交前查询只看到完整的旧结果，提交后只看到
    完整的新结果。
    """
    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.execute("COMMIT")


def get_version(conn: sqlite3.Connection, kind: str, file_date: str) -> int | None:
    row = conn.execute(
        "SELECT version FROM source_versions WHERE kind=? AND file_date=?",
        (kind, file_date),
    ).fetchone()
    return None if row is None else row["version"]


def replace_source_version(
    conn: sqlite3.Connection,
    kind: str,
    file_date: str,
    version: int,
) -> None:
    conn.execute(
        """
        INSERT INTO source_versions (kind, file_date, version)
        VALUES (?, ?, ?)
        ON CONFLICT (kind, file_date) DO UPDATE SET
            version=excluded.version,
            uploaded_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        """,
        (kind, file_date, version),
    )


def insert_plan_rows(
    conn: sqlite3.Connection,
    plan_date: str,
    version: int,
    rows: Iterable[dict[str, Any]],
) -> None:
    conn.executemany(
        """
        INSERT INTO plan_entries
            (plan_date, version, flight_no, destination, sched_arr_min, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                plan_date,
                version,
                r["flight_no"],
                r["destination"],
                r["sched_arr_min"],
                r["status"],
            )
            for r in rows
        ],
    )


def insert_landing_rows(
    conn: sqlite3.Connection,
    version: int,
    rows: Iterable[Any],
) -> None:
    conn.executemany(
        """
        INSERT INTO landing_entries
            (landing_date, plan_date, version, flight_no,
             actual_airport, actual_arr_min)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                r.landing_date,
                r.plan_date,
                version,
                r.flight_no,
                r.actual_airport,
                r.actual_arr_min,
            )
            for r in rows
        ],
    )


def list_plan_dates(conn: sqlite3.Connection) -> list[str]:
    return [
        r["file_date"]
        for r in conn.execute(
            "SELECT file_date FROM source_versions WHERE kind='plan' ORDER BY 1"
        )
    ]


def insert_link_rows(
    conn: sqlite3.Connection,
    file_date: str,
    version: int,
    rows: Iterable[Any],
) -> None:
    conn.executemany(
        """
        INSERT INTO link_entries
            (file_date, version, aircraft, prev_plan_date, prev_flight_no,
             next_plan_date, next_flight_no, next_dep_airport, next_dep_min)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                file_date,
                version,
                r.aircraft,
                r.prev_plan_date,
                r.prev_flight_no,
                r.next_plan_date,
                r.next_flight_no,
                r.next_dep_airport,
                r.next_dep_min,
            )
            for r in rows
        ],
    )


def list_link_dates(conn: sqlite3.Connection) -> list[str]:
    return [
        r["file_date"]
        for r in conn.execute(
            "SELECT file_date FROM source_versions WHERE kind='link' ORDER BY 1"
        )
    ]


def upsert_turnaround_fact(conn: sqlite3.Connection, fact: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO turnaround_facts (
            dep_date, next_flight_no, aircraft, prev_plan_date, prev_flight_no,
            next_plan_date, next_dep_airport, next_dep_min, prev_arr_min,
            turnaround_min, status, review_reasons, missing_sources,
            prev_plan_version, prev_landing_d0_ver, prev_landing_d1_ver,
            link_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (dep_date, next_flight_no) DO UPDATE SET
            aircraft=excluded.aircraft,
            prev_plan_date=excluded.prev_plan_date,
            prev_flight_no=excluded.prev_flight_no,
            next_plan_date=excluded.next_plan_date,
            next_dep_airport=excluded.next_dep_airport,
            next_dep_min=excluded.next_dep_min,
            prev_arr_min=excluded.prev_arr_min,
            turnaround_min=excluded.turnaround_min,
            status=excluded.status,
            review_reasons=excluded.review_reasons,
            missing_sources=excluded.missing_sources,
            prev_plan_version=excluded.prev_plan_version,
            prev_landing_d0_ver=excluded.prev_landing_d0_ver,
            prev_landing_d1_ver=excluded.prev_landing_d1_ver,
            link_version=excluded.link_version
        """,
        (
            fact["dep_date"],
            fact["next_flight_no"],
            fact["aircraft"],
            fact["prev_plan_date"],
            fact["prev_flight_no"],
            fact["next_plan_date"],
            fact["next_dep_airport"],
            fact["next_dep_min"],
            fact["prev_arr_min"],
            fact["turnaround_min"],
            fact["status"],
            _json_or_none(fact["review_reasons"]),
            _json_or_none(fact["missing_sources"]),
            fact["prev_plan_version"],
            fact["prev_landing_d0_ver"],
            fact["prev_landing_d1_ver"],
            fact["link_version"],
        ),
    )


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def upsert_fact(conn: sqlite3.Connection, fact: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO arrival_facts (
            plan_date, flight_no, fact_type, sched_airport, actual_airport,
            delay_min, review_reasons, landing_count,
            plan_version, landing_d0_ver, landing_d1_ver)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (plan_date, flight_no) DO UPDATE SET
            flight_no=excluded.flight_no,
            fact_type=excluded.fact_type,
            sched_airport=excluded.sched_airport,
            actual_airport=excluded.actual_airport,
            delay_min=excluded.delay_min,
            review_reasons=excluded.review_reasons,
            landing_count=excluded.landing_count,
            plan_version=excluded.plan_version,
            landing_d0_ver=excluded.landing_d0_ver,
            landing_d1_ver=excluded.landing_d1_ver
        """,
        (
            fact["plan_date"],
            fact["flight_no"],
            fact["fact_type"],
            fact["sched_airport"],
            fact["actual_airport"],
            fact["delay_min"],
            json.dumps(fact["review_reasons"], ensure_ascii=False)
            if fact["review_reasons"] is not None
            else None,
            fact["landing_count"],
            fact["plan_version"],
            fact["landing_d0_ver"],
            fact["landing_d1_ver"],
        ),
    )
