"""业务失败测乙（乙路径反证）：

若一笔罚没出现「余额降而冻结不动」，即冻结未齐扣，则罚没正式册必须零增，
不得只动余额单列。修复后系统应当场检测漂移并整笔回滚、落偏航记拒写，
所以这条测试直接盯终态：事故态下再罚没，余额降、冻结不动、罚没册零增
三者不得并存——若并存就是埋雷仍在。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient) -> dict:
    tok = c.post(
        "/api/login", json={"username": "admin", "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def test_path_b_balance_drops_without_frozen_must_not_enter_ledger():
    c = TestClient(app)
    h = _auth(c)
    party = "P-B"
    amount = 30000.0
    c.post(
        "/api/bonds",
        headers=h,
        json={"party_key": party, "party_name": "乙户口径", "initial_deposit": 100000},
    )
    cage = c.post(
        "/api/cages/freeze",
        headers=h,
        json={
            "party_key": party,
            "contract_no": "HT-B-1",
            "amount": amount,
            "beneficiary": "受益方乙",
            "cage_code": "CG-B-001",
        },
    ).json()
    assert (
        c.post(
            "/api/attestations",
            headers=h,
            json={"cage_id": cage["id"], "outcome": "违约", "evidence": "逾期"},
        ).status_code
        == 200
    )

    # 埋雷同型漂移：冻结列被掏空（只少扣一角的极端：一分未扣侧）
    with connect() as conn:
        conn.execute(
            "UPDATE bond_accounts SET frozen=0 WHERE party_key=?", (party,)
        )
        conn.commit()
    with connect() as conn:
        b0 = conn.execute(
            "SELECT balance,frozen FROM bond_accounts WHERE party_key=?", (party,)
        ).fetchone()
        bal0, fro0 = float(b0["balance"]), float(b0["frozen"])
        assert fro0 == 0.0

    r = c.post("/api/forfeits", headers=h, json={"cage_id": cage["id"]})
    assert r.status_code == 409

    with connect() as conn:
        b1 = conn.execute(
            "SELECT balance,frozen FROM bond_accounts WHERE party_key=?", (party,)
        ).fetchone()
        bal1, fro1 = float(b1["balance"]), float(b1["frozen"])
        forfeit_cnt = conn.execute(
            "SELECT COUNT(*) c FROM forfeits WHERE cage_id=?", (cage["id"],)
        ).fetchone()["c"]
        yaw = conn.execute(
            "SELECT * FROM forfeit_yaw_log WHERE cage_id=?", (cage["id"],)
        ).fetchone()
        locks = conn.execute(
            "SELECT COUNT(*) c FROM cage_locks WHERE lock_key=? AND released_at IS NULL",
            (f"cage:{cage['id']}",),
        ).fetchone()["c"]

    # 禁止「余额降、冻结不动、罚没册零增/有增」并存的脏写：
    # 修复口径下罚没被拒，余额冻结全不动，正式册零增
    drifts_coexist = (bal0 != bal1) and (fro0 == fro1)
    assert not drifts_coexist, "乙路径破防：余额降而冻结不动仍被写入"
    assert (bal1, fro1) == (bal0, fro0)
    assert forfeit_cnt == 0, "冻结未齐扣时罚没正式册必须零增"
    assert yaw is not None, "偏航必须留痕（罚没双扣偏航）"
    assert locks == 0, "拒写后笼锁释放，不影响后续合法操作"

    # 漂移修正后同笼可按甲路径正常罚没，证明不是把笼写死
    with connect() as conn:
        conn.execute(
            "UPDATE bond_accounts SET frozen=? WHERE party_key=?",
            (amount, party),
        )
        conn.commit()
    r2 = c.post("/api/forfeits", headers=h, json={"cage_id": cage["id"]})
    assert r2.status_code == 200
    with connect() as conn:
        b2 = conn.execute(
            "SELECT balance,frozen FROM bond_accounts WHERE party_key=?", (party,)
        ).fetchone()
        assert round(float(b2["balance"]), 6) == round(bal0 - amount, 6)
        assert float(b2["frozen"]) == 0.0
        assert (
            conn.execute(
                "SELECT COUNT(*) c FROM forfeits WHERE cage_id=? AND status='已罚没'",
                (cage["id"],),
            ).fetchone()["c"]
            == 1
        )
