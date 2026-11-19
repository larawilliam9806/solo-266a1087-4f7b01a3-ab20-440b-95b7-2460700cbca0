"""核心业务：按版号替换源文件、重算到达事实与过站结果、查询与启动自愈。

待核原因（review_reasons）代码（到达日报）：
- CANCELLED_BUT_LANDED   取消却落地
- NORMAL_MISSING_LANDING 正常却缺落地
- ORPHAN_LANDING         有落地清单但计划清单中无对应航班
- MULTIPLE_LANDINGS      同航班多次落地

待核原因（过站）：
- PREV_REPORT_INCOMPLETE 前序日报缺清单（缺失项另在 missing_sources 给出）
- PREV_NOT_ARRIVED       前序日报未判定为到达或备降
- AIRPORT_MISMATCH       前序实际落地机场与后序起飞机场不一致
- LANDING_AFTER_DEPARTURE 前序落地时刻晚于后序起飞时刻
"""

from __future__ import annotations

import datetime as _dt
import json
import threading
from typing import Any

from . import csv_parser, db
from .csv_parser import LandingRow, LinkRow, PlanRow, add_days

# 进程内串行化所有写操作；RLock 允许启动修复与上传路径复用同一把锁。
_WRITE_LOCK = threading.RLock()

REASON_CANCELLED_BUT_LANDED = "CANCELLED_BUT_LANDED"
REASON_NORMAL_MISSING = "NORMAL_MISSING_LANDING"
REASON_ORPHAN = "ORPHAN_LANDING"
REASON_MULTIPLE = "MULTIPLE_LANDINGS"

REASON_PREV_INCOMPLETE = "PREV_REPORT_INCOMPLETE"
REASON_PREV_NOT_ARRIVED = "PREV_NOT_ARRIVED"
REASON_AIRPORT_MISMATCH = "AIRPORT_MISMATCH"
REASON_LANDING_AFTER_DEPARTURE = "LANDING_AFTER_DEPARTURE"

REASON_TEXT = {
    REASON_CANCELLED_BUT_LANDED: "取消却落地",
    REASON_NORMAL_MISSING: "正常却缺落地",
    REASON_ORPHAN: "落地清单中存在但计划清单无对应航班",
    REASON_MULTIPLE: "同航班多次落地",
    REASON_PREV_INCOMPLETE: "前序日报缺清单",
    REASON_PREV_NOT_ARRIVED: "前序日报未判定到达或备降",
    REASON_AIRPORT_MISMATCH: "前序实际落地机场与后序起飞机场不一致",
    REASON_LANDING_AFTER_DEPARTURE: "前序落地时刻晚于后序起飞时刻",
}


class VersionConflictError(Exception):
    """新版号不大于已存版号。"""

    def __init__(self, kind: str, file_date: str, old: int, new: int):
        self.kind = kind
        self.file_date = file_date
        self.old = old
        self.new = new
        super().__init__(
            f"{kind} 文件日期 {file_date} 已存在版号 {old}，版号 {new} 不更新（须严格递增）"
        )


class LinkConflictError(Exception):
    """衔接清单存在航班复用或机号不一致，整批拒绝。"""

    def __init__(self, file_date: str, conflicts: list[str]):
        self.file_date = file_date
        self.conflicts = conflicts
        detail = "；".join(conflicts)
        super().__init__(f"衔接清单 {file_date} 存在冲突，整批拒绝：{detail}")


def upload_plan(
    plan_date: str,
    version: int,
    rows: list[PlanRow],
    db_file: str | None = None,
) -> dict[str, Any]:
    """整批替换某计划起飞日的计划清单，并重算该日到达结论。

    计划与落地报文都会影响以该日航班为前序或后序的过站结果，因此受影响
    起飞日在同一事务内一并重算。
    """
    with _WRITE_LOCK:
        conn = db.connect(db_file)
        try:
            with db.immediate_tx(conn):
                old = db.get_version(conn, "plan", plan_date)
                if old is not None and version <= old:
                    raise VersionConflictError("plan", plan_date, old, version)

                affected_deps = _dep_dates_for_plan_date(conn, plan_date)
                conn.execute(
                    "DELETE FROM plan_entries WHERE plan_date=?", (plan_date,)
                )
                db.insert_plan_rows(conn, plan_date, version, csv_parser.rows_to_dicts(rows))
                db.replace_source_version(conn, "plan", plan_date, version)
                # 计划只服务于本计划日。
                recompute_facts(conn, plan_date)
                for dep_date in affected_deps:
                    recompute_turnarounds(conn, dep_date)
            return {
                "kind": "plan",
                "file_date": plan_date,
                "version": version,
                "replaced": old is not None,
                "row_count": len(rows),
                "recomputed_dates": [plan_date],
                "recomputed_dep_dates": sorted(affected_deps),
            }
        finally:
            conn.close()


def upload_landing(
    landing_date: str,
    version: int,
    rows: list[LandingRow],
    db_file: str | None = None,
) -> dict[str, Any]:
    """整批替换某落地日的落地清单。

    落地日 L 的报文可同时包含计划日 L 与 L-1 的航班，因此受影响的两个
    计划日结论必须在同一事务内一起重算；以这些计划日航班为前序的过站结果
    也随源数据一起重算。
    """
    with _WRITE_LOCK:
        conn = db.connect(db_file)
        try:
            with db.immediate_tx(conn):
                old = db.get_version(conn, "landing", landing_date)
                if old is not None and version <= old:
                    raise VersionConflictError("landing", landing_date, old, version)

                affected_prev_dates = {add_days(landing_date, -1), landing_date}
                affected_deps = _dep_dates_for_prev_dates(conn, affected_prev_dates)

                conn.execute(
                    "DELETE FROM landing_entries WHERE landing_date=?", (landing_date,)
                )
                db.insert_landing_rows(conn, version, rows)
                db.replace_source_version(conn, "landing", landing_date, version)

                affected = sorted(affected_prev_dates)
                for plan_date in affected:
                    recompute_facts(conn, plan_date)
                for dep_date in affected_deps:
                    recompute_turnarounds(conn, dep_date)
            return {
                "kind": "landing",
                "file_date": landing_date,
                "version": version,
                "replaced": old is not None,
                "row_count": len(rows),
                "recomputed_dates": affected,
                "recomputed_dep_dates": sorted(affected_deps),
            }
        finally:
            conn.close()


def recompute_facts(conn, plan_date: str) -> dict[str, Any]:
    """按当前库内三清单重算一个计划起飞日的全部结论。

    清单不齐全（计划日清单、当日落地清单、次日落地清单缺一）时删除旧结论
    且不产出新结论——缺清单时绝不判定取消或正常到达。
    """
    plan_ver = db.get_version(conn, "plan", plan_date)
    d0_ver = db.get_version(conn, "landing", plan_date)
    d1_date = add_days(plan_date, 1)
    d1_ver = db.get_version(conn, "landing", d1_date)

    conn.execute("DELETE FROM arrival_facts WHERE plan_date=?", (plan_date,))
    if plan_ver is None or d0_ver is None or d1_ver is None:
        return {"plan_date": plan_date, "complete": False, "facts": 0}

    plan_rows = conn.execute(
        "SELECT flight_no, destination, sched_arr_min, status "
        "FROM plan_entries WHERE plan_date=? ORDER BY flight_no",
        (plan_date,),
    ).fetchall()

    landing_rows = conn.execute(
        "SELECT flight_no, actual_airport, actual_arr_min, plan_date, landing_date "
        "FROM landing_entries WHERE plan_date=? ORDER BY flight_no, actual_arr_min",
        (plan_date,),
    ).fetchall()

    landings: dict[str, list[Any]] = {}
    for lr in landing_rows:
        landings.setdefault(lr["flight_no"], []).append(lr)

    planned = {r["flight_no"]: r for r in plan_rows}
    facts: list[dict[str, Any]] = []

    for flight_no, plan in planned.items():
        hits = landings.get(flight_no, [])
        base = {
            "plan_date": plan_date,
            "flight_no": flight_no,
            "sched_airport": plan["destination"],
            "plan_version": plan_ver,
            "landing_d0_ver": d0_ver,
            "landing_d1_ver": d1_ver,
        }
        if not hits:
            if plan["status"] == "cancelled":
                facts.append(
                    {
                        **base,
                        **_fact(
                            "cancelled",
                            actual_airport=None,
                            delay_min=None,
                            reasons=None,
                            landing_count=0,
                        ),
                    }
                )
            else:
                facts.append(
                    {
                        **base,
                        **_fact(
                            "review",
                            actual_airport=None,
                            delay_min=None,
                            reasons=[REASON_NORMAL_MISSING],
                            landing_count=0,
                        ),
                    }
                )
            continue

        if len(hits) > 1:
            facts.append(
                {
                    **base,
                    **_fact(
                        "review",
                        actual_airport=hits[0]["actual_airport"],
                        delay_min=None,
                        reasons=_multi_reasons(plan["status"]),
                        landing_count=len(hits),
                    ),
                }
            )
            continue

        landing = hits[0]
        if plan["status"] == "cancelled":
            facts.append(
                {
                    **base,
                    **_fact(
                        "review",
                        actual_airport=landing["actual_airport"],
                        delay_min=None,
                        reasons=[REASON_CANCELLED_BUT_LANDED],
                        landing_count=1,
                    ),
                }
            )
            continue

        if landing["actual_airport"] != plan["destination"]:
            # 备降：记录实际机场，不计算原目的地延误。
            facts.append(
                {
                    **base,
                    **_fact(
                        "diverted",
                        actual_airport=landing["actual_airport"],
                        delay_min=None,
                        reasons=None,
                        landing_count=1,
                    ),
                }
            )
            continue

        delay = max(0, landing["actual_arr_min"] - plan["sched_arr_min"])
        facts.append(
            {
                **base,
                **_fact(
                    "arrived",
                    actual_airport=landing["actual_airport"],
                    delay_min=delay,
                    reasons=None,
                    landing_count=1,
                ),
            }
        )

    # 孤儿落地：落地清单里有、计划清单里没有的航班。
    for flight_no, hits in landings.items():
        if flight_no in planned:
            continue
        sample = hits[0]
        facts.append(
            {
                "plan_date": plan_date,
                "flight_no": flight_no,
                "sched_airport": None,
                "plan_version": plan_ver,
                "landing_d0_ver": d0_ver,
                "landing_d1_ver": d1_ver,
                **_fact(
                    "review",
                    actual_airport=sample["actual_airport"],
                    delay_min=None,
                    reasons=[REASON_ORPHAN],
                    landing_count=len(hits),
                ),
            }
        )

    for fact in facts:
        db.upsert_fact(conn, fact)
    return {"plan_date": plan_date, "complete": True, "facts": len(facts)}


# ---------------------------------------------------------------------------
# 过站（turnaround）
# ---------------------------------------------------------------------------


def upload_link(
    dep_date: str,
    version: int,
    rows: list[LinkRow],
    db_file: str | None = None,
) -> dict[str, Any]:
    """按后序实际起飞 UTC 日整批替换衔接清单，并重算该日起飞日的过站结果。

    冲突检查整批进行：同一航班至多作一次前序、一次后序，两处机号必须一致；
    冲突时抛 ``LinkConflictError``，不触碰任何数据。跨文件冲突（本次清单与
    其它日期的衔接清单）同样整批拒绝。
    """
    with _WRITE_LOCK:
        conn = db.connect(db_file)
        try:
            with db.immediate_tx(conn):
                old = db.get_version(conn, "link", dep_date)
                if old is not None and version <= old:
                    raise VersionConflictError("link", dep_date, old, version)

                conflicts = _find_link_conflicts(conn, dep_date, rows)
                if conflicts:
                    raise LinkConflictError(dep_date, conflicts)

                # 只有本起飞日的结果依赖这份清单；前序复用的航班若跨文件
                # 出现会先被冲突检查整批拒绝。
                affected_deps = {dep_date}

                conn.execute("DELETE FROM link_entries WHERE file_date=?", (dep_date,))
                db.insert_link_rows(conn, dep_date, version, rows)
                db.replace_source_version(conn, "link", dep_date, version)
                for affected in sorted(affected_deps):
                    recompute_turnarounds(conn, affected)
            return {
                "kind": "link",
                "file_date": dep_date,
                "version": version,
                "replaced": old is not None,
                "row_count": len(rows),
                "recomputed_dates": [],
                "recomputed_dep_dates": sorted(affected_deps),
            }
        finally:
            conn.close()


def _find_link_conflicts(
    conn, dep_date: str, rows: list[LinkRow]
) -> list[str]:
    """检查本次衔接清单的航班复用与机号一致性（含跨文件）。

    规则：同一航班至多作一次前序、一次后序；该航班无论出现在前序还是
    后序位置，机号必须一致。冲突既可能是本次文件内重复，也可能是与其它
    日期的衔接清单重复。任何冲突都整批拒绝。
    """
    conflicts: list[str] = []

    # 现有（其它日期的）占位：航班键 -> 机号。
    existing_prev: dict[tuple[str, str], str] = {}
    existing_next: dict[tuple[str, str], str] = {}
    for row in conn.execute(
        "SELECT aircraft, prev_plan_date, prev_flight_no, next_plan_date, "
        "next_flight_no FROM link_entries WHERE file_date<>?",
        (dep_date,),
    ).fetchall():
        prev_key = (row["prev_plan_date"], row["prev_flight_no"])
        next_key = (row["next_plan_date"], row["next_flight_no"])
        existing_prev.setdefault(prev_key, row["aircraft"])
        existing_next.setdefault(next_key, row["aircraft"])

    # 本批文件内的占位，与现有占位合并检查。
    prev_owner: dict[tuple[str, str], str] = dict(existing_prev)
    next_owner: dict[tuple[str, str], str] = dict(existing_next)
    for row in rows:
        crosses_prev = row.prev_key in existing_prev
        crosses_next = row.next_key in existing_next
        if row.prev_key in prev_owner:
            where = "其它日期的衔接清单中" if crosses_prev else "同批清单中"
            conflicts.append(
                f"航班 {row.prev_flight_no}（计划日 {row.prev_plan_date}）"
                f"在{where}多次/重复作为前序"
            )
        else:
            prev_owner[row.prev_key] = row.aircraft
        if row.next_key in next_owner:
            where = "其它日期的衔接清单中" if crosses_next else "同批清单中"
            conflicts.append(
                f"航班 {row.next_flight_no}（计划日 {row.next_plan_date}）"
                f"在{where}多次/重复作为后序"
            )
        else:
            next_owner[row.next_key] = row.aircraft

    # 同一航班作为前序和后序时，机号必须一致（含跨文件）。
    for flight_key, aircraft in prev_owner.items():
        other = next_owner.get(flight_key)
        if other is not None and other != aircraft:
            conflicts.append(
                f"航班 {flight_key[1]}（计划日 {flight_key[0]}）作为前序的机号 "
                f"{aircraft} 与作为后序的机号 {other} 不一致"
            )
    # 去重，保持顺序。
    return list(dict.fromkeys(conflicts))


def _dep_dates_for_plan_date(conn, plan_date: str) -> set[str]:
    """某计划日起飞的所有航班作为前序或后序时，受影响的衔接文件起飞日。"""
    rows = conn.execute(
        "SELECT DISTINCT file_date FROM link_entries "
        "WHERE prev_plan_date=? OR next_plan_date=?",
        (plan_date, plan_date),
    ).fetchall()
    return {r["file_date"] for r in rows}


def _dep_dates_for_prev_dates(conn, plan_dates: set[str]) -> set[str]:
    """给定若干计划日，找出以其航班为前序的衔接文件起飞日。"""
    dates = sorted(plan_dates)
    if not dates:
        return set()
    placeholders = ",".join("?" for _ in dates)
    rows = conn.execute(
        f"SELECT DISTINCT file_date FROM link_entries "
        f"WHERE prev_plan_date IN ({placeholders})",
        dates,
    ).fetchall()
    return {r["file_date"] for r in rows}


def recompute_turnarounds(conn, dep_date: str) -> dict[str, Any]:
    """按当前库内衔接清单与相关源清单重算某起飞日的全部过站结果。

    每个后序航班的结果都有明确状态：
    - ``confirmed``：前序日报已判定到达或备降、实际落地机场与后序起飞机场
      相同且落地不晚于起飞，按实际时刻计算过站分钟；
    - ``review``：其余衔接保留并标明待核原因。
    缺清单时同样保留衔接，单独在 ``missing_sources`` 标记缺失项。
    """
    link_ver = db.get_version(conn, "link", dep_date)
    conn.execute("DELETE FROM turnaround_facts WHERE dep_date=?", (dep_date,))
    if link_ver is None:
        return {"dep_date": dep_date, "complete": False, "facts": 0}

    links = conn.execute(
        "SELECT aircraft, prev_plan_date, prev_flight_no, next_plan_date, "
        "next_flight_no, next_dep_airport, next_dep_min "
        "FROM link_entries WHERE file_date=? ORDER BY next_flight_no",
        (dep_date,),
    ).fetchall()

    facts = [_turnaround_fact(conn, dep_date, link_ver, link) for link in links]
    for fact in facts:
        db.upsert_turnaround_fact(conn, fact)
    complete = all(not f["missing_sources"] for f in facts) if facts else True
    return {"dep_date": dep_date, "complete": complete, "facts": len(facts)}


def _turnaround_fact(conn, dep_date: str, link_ver: int, link) -> dict[str, Any]:
    fact: dict[str, Any] = {
        "dep_date": dep_date,
        "next_flight_no": link["next_flight_no"],
        "aircraft": link["aircraft"],
        "prev_plan_date": link["prev_plan_date"],
        "prev_flight_no": link["prev_flight_no"],
        "next_plan_date": link["next_plan_date"],
        "next_dep_airport": link["next_dep_airport"],
        "next_dep_min": link["next_dep_min"],
        "prev_arr_min": None,
        "turnaround_min": None,
        "status": "review",
        "review_reasons": None,
        "missing_sources": None,
        "prev_plan_version": None,
        "prev_landing_d0_ver": None,
        "prev_landing_d1_ver": None,
        "link_version": link_ver,
    }

    prev_plan_date = link["prev_plan_date"]
    prev_plan_ver = db.get_version(conn, "plan", prev_plan_date)
    prev_d0_date = prev_plan_date
    prev_d1_date = add_days(prev_plan_date, 1)
    prev_d0_ver = db.get_version(conn, "landing", prev_d0_date)
    prev_d1_ver = db.get_version(conn, "landing", prev_d1_date)
    fact.update(
        prev_plan_version=prev_plan_ver,
        prev_landing_d0_ver=prev_d0_ver,
        prev_landing_d1_ver=prev_d1_ver,
    )

    missing: list[str] = []
    if prev_plan_ver is None:
        missing.append(f"plan:{prev_plan_date}")
    if prev_d0_ver is None:
        missing.append(f"landing:{prev_d0_date}")
    if prev_d1_ver is None:
        missing.append(f"landing:{prev_d1_date}")
    if missing:
        fact["missing_sources"] = missing
        fact["review_reasons"] = [REASON_PREV_INCOMPLETE]
        return fact

    # 前序日报已判定到达或备降：读取该航班在到达事实中的结论。
    arrival = conn.execute(
        "SELECT fact_type, actual_airport FROM arrival_facts "
        "WHERE plan_date=? AND flight_no=?",
        (prev_plan_date, link["prev_flight_no"]),
    ).fetchone()
    if arrival is None:
        fact["review_reasons"] = [REASON_PREV_NOT_ARRIVED]
        return fact

    if arrival["fact_type"] not in ("arrived", "diverted"):
        fact["review_reasons"] = [REASON_PREV_NOT_ARRIVED]
        return fact

    if arrival["actual_airport"] != link["next_dep_airport"]:
        fact["review_reasons"] = [REASON_AIRPORT_MISMATCH]
        return fact

    # 前序落地时刻：取该航班在前序计划日落地清单中的实际到达分钟。
    landing = conn.execute(
        "SELECT actual_arr_min FROM landing_entries "
        "WHERE plan_date=? AND flight_no=? ORDER BY actual_arr_min LIMIT 1",
        (prev_plan_date, link["prev_flight_no"]),
    ).fetchone()
    if landing is None:
        fact["review_reasons"] = [REASON_PREV_NOT_ARRIVED]
        return fact

    prev_arr_min = landing["actual_arr_min"]
    fact["prev_arr_min"] = prev_arr_min
    # 后序起飞分钟已自文件日（即后序实际起飞日）UTC 零点起算，直接使用；
    # 前序落地分钟相对前序计划日零点，换算到文件日零点再比较。
    dep_abs = link["next_dep_min"]
    prev_date = _dt.date.fromisoformat(prev_plan_date)
    dep_abs_date = _dt.date.fromisoformat(dep_date)
    prev_abs = prev_arr_min + (prev_date - dep_abs_date).days * 24 * 60
    if prev_abs > dep_abs:
        fact["review_reasons"] = [REASON_LANDING_AFTER_DEPARTURE]
        return fact

    fact["status"] = "confirmed"
    fact["turnaround_min"] = dep_abs - prev_abs
    return fact


def _fact(
    fact_type: str,
    *,
    actual_airport: str | None,
    delay_min: int | None,
    reasons: list[str] | None,
    landing_count: int,
) -> dict[str, Any]:
    return {
        "fact_type": fact_type,
        "actual_airport": actual_airport,
        "delay_min": delay_min,
        "review_reasons": reasons,
        "landing_count": landing_count,
    }


def _multi_reasons(plan_status: str) -> list[str]:
    reasons = [REASON_MULTIPLE]
    if plan_status == "cancelled":
        reasons.append(REASON_CANCELLED_BUT_LANDED)
    return reasons


def get_report(plan_date: str, db_file: str | None = None) -> dict[str, Any]:
    """查询某日结论；缺清单时返回缺失项，不返回事实。"""
    conn = db.connect(db_file)
    try:
        plan_ver = db.get_version(conn, "plan", plan_date)
        d0_ver = db.get_version(conn, "landing", plan_date)
        d1_date = add_days(plan_date, 1)
        d1_ver = db.get_version(conn, "landing", d1_date)

        missing: list[str] = []
        if plan_ver is None:
            missing.append(f"plan:{plan_date}")
        if d0_ver is None:
            missing.append(f"landing:{plan_date}")
        if d1_ver is None:
            missing.append(f"landing:{d1_date}")

        rows = conn.execute(
            "SELECT * FROM arrival_facts WHERE plan_date=? "
            "ORDER BY flight_no",
            (plan_date,),
        ).fetchall()
        facts = [_fact_to_output(r) for r in rows]
        return {
            "plan_date": plan_date,
            "status": "complete" if not missing else "incomplete",
            "missing_sources": missing,
            "source_versions": {
                "plan": plan_ver,
                f"landing_{plan_date}": d0_ver,
                f"landing_{d1_date}": d1_ver,
            },
            "facts": facts,
        }
    finally:
        conn.close()


def _fact_to_output(row) -> dict[str, Any]:
    reasons = json.loads(row["review_reasons"]) if row["review_reasons"] else None
    return {
        "flight_no": row["flight_no"],
        "fact_type": row["fact_type"],
        "sched_airport": row["sched_airport"],
        "actual_airport": row["actual_airport"],
        "delay_min": row["delay_min"],
        "landing_count": row["landing_count"],
        "review_reasons": reasons,
        "review_text": [REASON_TEXT[r] for r in reasons] if reasons else None,
        "source_versions": {
            "plan": row["plan_version"],
            "landing_d0": row["landing_d0_ver"],
            "landing_d1": row["landing_d1_ver"],
        },
    }


def get_turnaround_report(dep_date: str, db_file: str | None = None) -> dict[str, Any]:
    """查询某后序实际起飞日的过站结果。

    结果已在写入时预先入库，查询只读一日的行；更新期间查询要么看到完整的
    旧结果，要么看到完整的新结果（同一事务提交）。
    """
    conn = db.connect(db_file)
    try:
        with db.read_snapshot(conn):
            link_ver = db.get_version(conn, "link", dep_date)
            rows = conn.execute(
                "SELECT * FROM turnaround_facts WHERE dep_date=? "
                "ORDER BY next_flight_no",
                (dep_date,),
            ).fetchall()
            facts = [_turnaround_to_output(r) for r in rows]

            missing: list[str] = []
            if link_ver is None:
                missing.append(f"link:{dep_date}")
            for fact in facts:
                for source in fact["missing_sources"] or []:
                    if source not in missing:
                        missing.append(source)

            return {
                "dep_date": dep_date,
                "status": "incomplete" if missing else "complete",
                "missing_sources": missing,
                "link_version": link_ver,
                "facts": facts,
            }
    finally:
        conn.close()


def _turnaround_to_output(row) -> dict[str, Any]:
    reasons = json.loads(row["review_reasons"]) if row["review_reasons"] else None
    missing = json.loads(row["missing_sources"]) if row["missing_sources"] else None
    return {
        "next_flight_no": row["next_flight_no"],
        "aircraft": row["aircraft"],
        "prev_plan_date": row["prev_plan_date"],
        "prev_flight_no": row["prev_flight_no"],
        "next_plan_date": row["next_plan_date"],
        "next_dep_airport": row["next_dep_airport"],
        "next_dep_min": row["next_dep_min"],
        "prev_arr_min": row["prev_arr_min"],
        "turnaround_min": row["turnaround_min"],
        "status": row["status"],
        "review_reasons": reasons,
        "review_text": [REASON_TEXT[r] for r in reasons] if reasons else None,
        "missing_sources": missing,
        "source_versions": {
            "prev_plan": row["prev_plan_version"],
            "prev_landing_d0": row["prev_landing_d0_ver"],
            "prev_landing_d1": row["prev_landing_d1_ver"],
            "link": row["link_version"],
        },
    }


def list_sources(db_file: str | None = None) -> dict[str, list[dict[str, Any]]]:
    conn = db.connect(db_file)
    try:
        out: dict[str, list[dict[str, Any]]] = {"plan": [], "landing": [], "link": []}
        for kind in ("plan", "landing", "link"):
            out[kind] = [
                dict(r)
                for r in conn.execute(
                    "SELECT kind, file_date, version, uploaded_at "
                    "FROM source_versions WHERE kind=? ORDER BY file_date",
                    (kind,),
                ).fetchall()
            ]
        return out
    finally:
        conn.close()


def repair_on_startup(db_file: str | None = None) -> dict[str, Any]:
    """启动自愈：按当前源版号重算所有到达事实与过站结果。

    正常运行下结论与源在同一事务内提交，本步骤只是幂等兜底，保证重启后
    不会留下任何混版结论。写入失败时事务回滚，旧数据与旧结果保持生效。
    """
    with _WRITE_LOCK:
        conn = db.connect(db_file)
        try:
            plan_dates = db.list_plan_dates(conn)
            repaired: list[dict[str, Any]] = []
            for plan_date in plan_dates:
                with db.immediate_tx(conn):
                    repaired.append(recompute_facts(conn, plan_date))

            dep_dates = db.list_link_dates(conn)
            repaired_deps: list[dict[str, Any]] = []
            for dep_date in dep_dates:
                with db.immediate_tx(conn):
                    repaired_deps.append(recompute_turnarounds(conn, dep_date))
            return {
                "checked_dates": plan_dates,
                "results": repaired,
                "checked_dep_dates": dep_dates,
                "turnaround_results": repaired_deps,
            }
        finally:
            conn.close()
