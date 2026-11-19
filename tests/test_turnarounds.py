"""过站结果端到端测试。"""

from __future__ import annotations

from tests.conftest import csv_bytes, upload

D = "2026-11-18"
D1 = "2026-11-19"
D2 = "2026-11-20"

PLAN_D = """航班号,目的地,计划到达UTC分钟,状态
CA101,PEK,1400,正常
CA102,PVG,660,正常
CA103,CAN,1000,正常
CA104,SZX,780,正常
CA105,CTU,840,取消
CA106,HGH,900,正常
"""

# D 日落地的前序：CA101 正点到达；CA102 备降 SZX；CA104 到达 SZX。
LAND_D = """计划起飞日,航班号,实际机场,实际到达UTC分钟
2026-11-18,CA101,PEK,1430
2026-11-18,CA102,SZX,730
2026-11-18,CA104,SZX,800
"""

# CA103 跨午夜 00:10 落地，属 D+1；CA201 计划日 D+1 当天 21:40 落地。
LAND_D1 = """计划起飞日,航班号,实际机场,实际到达UTC分钟
2026-11-18,CA103,SZX,1450
2026-11-19,CA201,CAN,1300
"""

PLAN_D1 = """航班号,目的地,计划到达UTC分钟,状态
CA201,CAN,500,正常
"""

EMPTY_LAND = "计划起飞日,航班号,实际机场,实际到达UTC分钟\n"

HEADER = (
    "机号,前序计划起飞日,前序航班号,后序计划起飞日,后序航班号,"
    "后序起飞机场,后序实际起飞UTC分钟\n"
)


def _seed(client):
    assert upload(client, "plans", D, 1, csv_bytes(PLAN_D)).status_code == 200
    assert upload(client, "landings", D, 1, csv_bytes(LAND_D)).status_code == 200
    assert upload(client, "landings", D1, 1, csv_bytes(LAND_D1)).status_code == 200
    assert upload(client, "plans", D1, 1, csv_bytes(PLAN_D1)).status_code == 200
    assert upload(client, "landings", D2, 1, csv_bytes(EMPTY_LAND)).status_code == 200


def _by_next(dep_date, client):
    rep = client.get(f"/api/turnarounds/{dep_date}").json()
    return rep, {f["next_flight_no"]: f for f in rep["facts"]}


def test_confirmed_turnaround_uses_actual_times(client):
    _seed(client)
    # CA101 前序 D 23:50 落地 PEK；后序 NB101 在 D+1 01:00 起飞 PEK。
    link = HEADER + f"B-1001,{D},CA101,{D1},NB101,PEK,60\n"
    resp = upload(client, "turnarounds", D1, 1, csv_bytes(link))
    assert resp.status_code == 200, resp.text
    assert resp.json()["recomputed_dep_dates"] == [D1]

    rep, facts = _by_next(D1, client)
    assert rep["status"] == "complete"
    f = facts["NB101"]
    assert f["status"] == "confirmed"
    assert f["aircraft"] == "B-1001"
    assert f["prev_arr_min"] == 1430
    # 前序 23:50 -> 次日 01:00 为 70 分钟。
    assert f["turnaround_min"] == 70
    assert f["review_reasons"] is None
    assert f["missing_sources"] is None
    assert f["source_versions"] == {
        "prev_plan": 1,
        "prev_landing_d0": 1,
        "prev_landing_d1": 1,
        "link": 1,
    }


def test_diverted_prev_with_matching_airport_is_confirmed(client):
    _seed(client)
    # CA102 备降 SZX，后序从 SZX 起飞。
    link = HEADER + f"B-2002,{D},CA102,{D1},NB102,SZX,800\n"
    assert upload(client, "turnarounds", D1, 1, csv_bytes(link)).status_code == 200
    _, facts = _by_next(D1, client)
    f = facts["NB102"]
    assert f["status"] == "confirmed"
    # 730 落地 D 日 -> 相对 D1 零点为 730-1440；800 起飞，相差 1510 分钟。
    assert f["turnaround_min"] == 800 - (730 - 1440)


def test_cross_midnight_landing_on_file_date(client):
    _seed(client)
    # CA103 次日 00:10 落地 SZX；后序 D+1 10:00 起飞。
    link = HEADER + f"B-3003,{D},CA103,{D1},NB103,SZX,600\n"
    assert upload(client, "turnarounds", D1, 1, csv_bytes(link)).status_code == 200
    _, facts = _by_next(D1, client)
    f = facts["NB103"]
    assert f["status"] == "confirmed"
    assert f["turnaround_min"] == 590


def test_review_reasons_keep_link(client):
    _seed(client)
    # NB201 机场不一致；NB202 前序取消；NB203 前序缺落地；NB204 落地晚于起飞。
    link = HEADER + (
        f"B-201,{D},CA101,{D1},NB201,CAN,100\n"
        f"B-202,{D},CA105,{D1},NB202,CTU,100\n"
        f"B-203,{D},CA106,{D1},NB203,HGH,100\n"
        f"B-204,{D1},CA201,{D1},NB204,CAN,600\n"
    )
    assert upload(client, "turnarounds", D1, 1, csv_bytes(link)).status_code == 200
    _, facts = _by_next(D1, client)

    assert facts["NB201"]["status"] == "review"
    assert facts["NB201"]["review_reasons"] == ["AIRPORT_MISMATCH"]
    assert facts["NB201"]["review_text"] == ["前序实际落地机场与后序起飞机场不一致"]
    assert facts["NB201"]["turnaround_min"] is None

    assert facts["NB202"]["review_reasons"] == ["PREV_NOT_ARRIVED"]
    assert facts["NB203"]["review_reasons"] == ["PREV_NOT_ARRIVED"]

    # CA201 前序计划日 D1、当天 21:40 落地，但后序 10:00 已起飞。
    assert facts["NB204"]["review_reasons"] == ["LANDING_AFTER_DEPARTURE"]
    # 所有待核衔接都保留。
    assert len(facts) == 4


def test_missing_sources_are_flagged_separately(client):
    # 只上传衔接清单，前序计划与落地清单都缺。
    link = HEADER + f"B-900,{D1},CA999,{D1},NB900,PEK,600\n"
    assert upload(client, "turnarounds", D1, 1, csv_bytes(link)).status_code == 200
    rep, facts = _by_next(D1, client)
    f = facts["NB900"]
    assert f["status"] == "review"
    assert f["review_reasons"] == ["PREV_REPORT_INCOMPLETE"]
    assert f["missing_sources"] == [
        f"plan:{D1}",
        f"landing:{D1}",
        f"landing:{D2}",
    ]
    assert rep["status"] == "incomplete"
    assert rep["missing_sources"] == [
        f"plan:{D1}",
        f"landing:{D1}",
        f"landing:{D2}",
    ]


def test_query_without_link_file_is_incomplete(client):
    rep = client.get(f"/api/turnarounds/{D1}").json()
    assert rep["status"] == "incomplete"
    assert rep["missing_sources"] == [f"link:{D1}"]
    assert rep["facts"] == []


def test_duplicate_flight_in_batch_rejected(client):
    _seed(client)
    link = HEADER + (
        f"B-1,{D},CA101,{D1},NB101,PEK,100\n"
        f"B-2,{D},CA101,{D1},NB102,PEK,200\n"
    )
    resp = upload(client, "turnarounds", D1, 1, csv_bytes(link))
    assert resp.status_code == 409
    assert "前序" in resp.json()["detail"] and "重复" in resp.json()["detail"]
    # 整批拒绝，没有任何结果入库。
    rep = client.get(f"/api/turnarounds/{D1}").json()
    assert rep["facts"] == []
    assert rep["link_version"] is None


def test_duplicate_next_flight_rejected(client):
    _seed(client)
    link = HEADER + (
        f"B-1,{D},CA101,{D1},NB101,PEK,100\n"
        f"B-2,{D},CA102,{D1},NB101,SZX,200\n"
    )
    resp = upload(client, "turnarounds", D1, 1, csv_bytes(link))
    assert resp.status_code == 409
    assert "后序" in resp.json()["detail"] and "重复" in resp.json()["detail"]


def test_cross_file_conflict_rejected(client):
    _seed(client)
    assert upload(
        client,
        "turnarounds",
        D1,
        1,
        csv_bytes(HEADER + f"B-1,{D},CA101,{D1},NB101,PEK,100\n"),
    ).status_code == 200
    # 另一起飞日的清单再次把 NB101 作为后序 -> 跨文件冲突，整批拒绝。
    resp = upload(
        client,
        "turnarounds",
        D2,
        1,
        csv_bytes(HEADER + f"B-1,{D1},CA201,{D1},NB101,CAN,100\n"),
    )
    assert resp.status_code == 409
    assert "其它日期" in resp.json()["detail"]
    assert client.get(f"/api/turnarounds/{D2}").json()["facts"] == []


def test_same_flight_prev_and_next_allowed_if_aircraft_matches(client):
    _seed(client)
    # NB101 既作首条后序，又作次条前序；机号一致则合法。
    link = HEADER + (
        f"B-1,{D},CA101,{D1},NB101,PEK,100\n"
        f"B-1,{D1},NB101,{D1},NB102,PEK,200\n"
    )
    resp = upload(client, "turnarounds", D1, 1, csv_bytes(link))
    assert resp.status_code == 200, resp.text
    _, facts = _by_next(D1, client)
    assert facts["NB101"]["aircraft"] == "B-1"
    assert facts["NB102"]["aircraft"] == "B-1"


def test_aircraft_inconsistency_within_row_rejected(client):
    _seed(client)
    link = (
        "机号,前序机号,后序机号,前序计划起飞日,前序航班号,后序计划起飞日,"
        "后序航班号,后序起飞机场,后序实际起飞UTC分钟\n"
        f"B-1,B-1,B-2,{D},CA101,{D1},NB101,PEK,100\n"
    )
    resp = upload(client, "turnarounds", D1, 1, csv_bytes(link))
    assert resp.status_code == 422
    assert "机号不一致" in resp.json()["detail"]


def test_plan_dates_limited_to_file_day_or_previous(client):
    _seed(client)
    for prev_date, next_date in ((D2, D1), (D, D2)):
        link = HEADER + f"B-1,{prev_date},CA101,{next_date},NB101,PEK,100\n"
        resp = upload(client, "turnarounds", D1, 1, csv_bytes(link))
        assert resp.status_code == 422
        assert "文件日" in resp.json()["detail"]


def test_landing_revision_recomputes_turnaround(client):
    _seed(client)
    link = HEADER + f"B-1001,{D},CA101,{D1},NB101,PEK,60\n"
    assert upload(client, "turnarounds", D1, 1, csv_bytes(link)).status_code == 200
    assert _by_next(D1, client)[1]["NB101"]["turnaround_min"] == 70

    # 修订 D 日落地报文：CA101 改为 22:00 落地，过站随之变为 180 分钟。
    new_land = """计划起飞日,航班号,实际机场,实际到达UTC分钟
2026-11-18,CA101,PEK,1320
2026-11-18,CA102,SZX,730
2026-11-18,CA104,SZX,800
"""
    resp = upload(client, "landings", D, 2, csv_bytes(new_land))
    assert resp.status_code == 200
    assert D1 in resp.json()["recomputed_dep_dates"]
    f = _by_next(D1, client)[1]["NB101"]
    assert f["turnaround_min"] == 180
    assert f["source_versions"]["prev_landing_d0"] == 2


def test_plan_revision_recomputes_turnaround(client):
    _seed(client)
    link = HEADER + f"B-1001,{D},CA101,{D1},NB101,PEK,60\n"
    assert upload(client, "turnarounds", D1, 1, csv_bytes(link)).status_code == 200
    assert _by_next(D1, client)[1]["NB101"]["status"] == "confirmed"

    # CA101 改为取消：前序不再判定到达，过站转为待核。
    new_plan = """航班号,目的地,计划到达UTC分钟,状态
CA101,PEK,1400,取消
CA102,PVG,660,正常
CA103,CAN,1000,正常
CA104,SZX,780,正常
CA105,CTU,840,取消
CA106,HGH,900,正常
"""
    resp = upload(client, "plans", D, 2, csv_bytes(new_plan))
    assert resp.status_code == 200
    assert D1 in resp.json()["recomputed_dep_dates"]
    f = _by_next(D1, client)[1]["NB101"]
    assert f["status"] == "review"
    # 取消却落地 -> CANCELLED_BUT_LANDED，不是到达/备降。
    assert f["review_reasons"] == ["PREV_NOT_ARRIVED"]
    assert f["source_versions"]["prev_plan"] == 2


def test_link_file_replacement_drops_old_results(client):
    _seed(client)
    link = HEADER + (
        f"B-1,{D},CA101,{D1},NB101,PEK,60\n"
        f"B-2,{D},CA102,{D1},NB102,SZX,800\n"
    )
    assert upload(client, "turnarounds", D1, 1, csv_bytes(link)).status_code == 200
    assert set(_by_next(D1, client)[1]) == {"NB101", "NB102"}

    # 替换为只含一条衔接的清单：被删掉的航班结果同步消失。
    assert upload(
        client,
        "turnarounds",
        D1,
        2,
        csv_bytes(HEADER + f"B-1,{D},CA101,{D1},NB101,PEK,60\n"),
    ).status_code == 200
    rep, facts = _by_next(D1, client)
    assert set(facts) == {"NB101"}
    assert rep["link_version"] == 2


def test_failed_write_keeps_old_results(tmp_path, monkeypatch):
    """过站重算中途失败时，旧衔接与旧结果原样保留。"""
    monkeypatch.setenv("AIRPORT_DB_PATH", str(tmp_path / "turn_fail.db"))
    from app import db as db_mod
    from app import services
    from app.csv_parser import parse_link_csv

    db_mod.init_db()
    services.upload_plan(D, 1, _parse_plan(PLAN_D))
    services.upload_landing(D, 1, _parse_land(LAND_D, D))
    services.upload_landing(D1, 1, _parse_land(LAND_D1, D1))
    r = services.upload_link(
        D1, 1, parse_link_csv(csv_bytes(HEADER + f"B-1,{D},CA101,{D1},NB101,PEK,60\n"), D1)
    )
    assert r["row_count"] == 1
    before = services.get_turnaround_report(D1)
    assert before["facts"][0]["turnaround_min"] == 70

    original = services.db.upsert_turnaround_fact

    def boom(*_a, **_kw):
        raise RuntimeError("simulated failure")

    services.db.upsert_turnaround_fact = boom
    try:
        try:
            services.upload_link(
                D1,
                2,
                parse_link_csv(
                    csv_bytes(HEADER + f"B-9,{D},CA102,{D1},NB109,SZX,900\n"), D1
                ),
            )
            raised = False
        except RuntimeError:
            raised = True
    finally:
        services.db.upsert_turnaround_fact = original
    assert raised

    after = services.get_turnaround_report(D1)
    assert after["link_version"] == 1
    assert [f["next_flight_no"] for f in after["facts"]] == ["NB101"]
    assert after["facts"][0]["turnaround_min"] == 70


def test_restart_repairs_turnaround_results(tmp_path, monkeypatch):
    monkeypatch.setenv("AIRPORT_DB_PATH", str(tmp_path / "turn_restart.db"))
    from app import db as db_mod
    from app import services
    from app.csv_parser import parse_link_csv

    db_mod.init_db()
    services.upload_plan(D, 1, _parse_plan(PLAN_D))
    services.upload_landing(D, 1, _parse_land(LAND_D, D))
    services.upload_landing(D1, 1, _parse_land(LAND_D1, D1))
    services.upload_link(
        D1, 1, parse_link_csv(csv_bytes(HEADER + f"B-1,{D},CA101,{D1},NB101,PEK,60\n"), D1)
    )

    import sqlite3

    conn = sqlite3.connect(_env_db())
    conn.execute("UPDATE turnaround_facts SET turnaround_min=9999")
    conn.commit()
    conn.close()

    result = services.repair_on_startup(_env_db())
    assert D1 in result["checked_dep_dates"]
    assert services.get_turnaround_report(D1)["facts"][0]["turnaround_min"] == 70


def test_sources_lists_link_versions(client):
    _seed(client)
    upload(client, "turnarounds", D1, 1, csv_bytes(HEADER))
    sources = client.get("/api/sources").json()
    assert sources["link"] == [
        {
            "kind": "link",
            "file_date": D1,
            "version": 1,
            "uploaded_at": sources["link"][0]["uploaded_at"],
        }
    ]


def test_chain_across_files_allowed_when_aircraft_matches(client):
    _seed(client)
    # D1 文件：CA101 -> NB101；D2 文件：NB101 -> NB102，形成链式衔接。
    assert upload(
        client,
        "turnarounds",
        D1,
        1,
        csv_bytes(HEADER + f"B-1,{D},CA101,{D1},NB101,PEK,60\n"),
    ).status_code == 200
    resp = upload(
        client,
        "turnarounds",
        D2,
        1,
        csv_bytes(HEADER + f"B-1,{D1},NB101,{D2},NB102,PEK,100\n"),
    )
    assert resp.status_code == 200, resp.text
    _, facts = _by_next(D2, client)
    assert facts["NB102"]["aircraft"] == "B-1"


def test_cross_file_aircraft_mismatch_rejected(client):
    _seed(client)
    assert upload(
        client,
        "turnarounds",
        D1,
        1,
        csv_bytes(HEADER + f"B-1,{D},CA101,{D1},NB101,PEK,60\n"),
    ).status_code == 200
    resp = upload(
        client,
        "turnarounds",
        D2,
        1,
        csv_bytes(HEADER + f"B-2,{D1},NB101,{D2},NB102,PEK,100\n"),
    )
    assert resp.status_code == 409
    assert "不一致" in resp.json()["detail"]


def test_landing_d1_revision_recomputes_turnaround(client):
    _seed(client)
    # 前序 CA101 落在 D 日 23:50；后序 D+1 01:00 起飞。
    assert upload(
        client,
        "turnarounds",
        D1,
        1,
        csv_bytes(HEADER + f"B-1,{D},CA101,{D1},NB101,PEK,60\n"),
    ).status_code == 200
    assert _by_next(D1, client)[1]["NB101"]["turnaround_min"] == 70

    # 修订 D1 落地文件（作为 D 计划日的 landing_d1）本身不改变 CA101 的到达事实，
    # 但需保证过站结果所用版号与源一起切换。
    resp = upload(
        client,
        "landings",
        D1,
        2,
        csv_bytes(LAND_D1 + "2026-11-19,CA999,PEK,300\n"),
    )
    assert resp.status_code == 200
    assert D1 in resp.json()["recomputed_dep_dates"]
    f = _by_next(D1, client)[1]["NB101"]
    assert f["turnaround_min"] == 70
    assert f["source_versions"]["prev_landing_d1"] == 2


def _parse_plan(text):
    from app.csv_parser import parse_plan_csv

    return parse_plan_csv(csv_bytes(text))


def _parse_land(text, file_date):
    from app.csv_parser import parse_landing_csv

    return parse_landing_csv(csv_bytes(text), file_date)


def _env_db():
    import os

    return os.environ["AIRPORT_DB_PATH"]
