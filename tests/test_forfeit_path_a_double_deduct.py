"""业务失败测甲（甲路径唯一口径）：

罚没名册入账，则余额与冻结必须同事务、同额扣减；
若冻结未齐扣，则该笔正式零改回滚——forfeits 零增、笼可用零改、账户两列零动，
并落「罚没双扣偏航」偏航记后拒写。
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


def _auth(c: TestClient, user: str = "admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _make_breeched_cage(c: TestClient, h: dict, party: str, code: str, amount: float = 40000.0):
    c.post(
        "/api/bonds",
        headers=h,
        json={
            "party_key": party,
            "party_name": f"甲户口径{party}",
            "initial_deposit": 100000,
        },
    )
    cage = c.post(
        "/api/cages/freeze",
        headers=h,
        json={
            "party_key": party,
            "contract_no": "HT-A-1",
            "amount": amount,
            "beneficiary": "受益方甲",
            "cage_code": code,
        },
    ).json()
    r = c.post(
        "/api/attestations",
        headers=h,
        json={"cage_id": cage["id"], "outcome": "违约", "evidence": "逾期"},
    )
    assert r.status_code == 200
    return cage


def _acct(party: str) -> tuple[float, float]:
    with connect() as conn:
        r = conn.execute(
            "SELECT balance,frozen FROM bond_accounts WHERE party_key=?", (party,)
        ).fetchone()
        return float(r["balance"]), float(r["frozen"])


def test_path_a_forfeit_deducts_balance_and_frozen_same_amount():
    c = TestClient(app)
    h = _auth(c)
    party = "P-A"
    amount = 40000.0
    cage = _make_breeched_cage(c, h, party, "CG-A-001", amount)
    bal0, fro0 = _acct(party)

    r = c.post(
        "/api/forfeits",
        headers=h,
        json={"cage_id": cage["id"], "reason": "甲路径同事务双扣"},
    )
    assert r.status_code == 200, r.text
    bal1, fro1 = _acct(party)

    # 同事务同额：余额降幅 == 冻结降幅 == 罚没额
    assert round(bal0 - bal1, 6) == amount
    assert round(fro0 - fro1, 6) == amount
    assert round((bal0 - bal1) - (fro0 - fro1), 6) == 0

    with connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) c, COALESCE(SUM(amount),0) s
               FROM forfeits WHERE cage_id=? AND status='已罚没'""",
            (cage["id"],),
        ).fetchone()
        assert row["c"] == 1
        assert float(row["s"]) == amount
        cage_row = conn.execute(
            "SELECT available,status FROM cages WHERE id=?", (cage["id"],)
        ).fetchone()
        assert float(cage_row["available"]) == 0.0
        assert cage_row["status"] == "已罚没"
        # 双扣命运纪要：甲路径封死
        fate = conn.execute(
            "SELECT caliber,status FROM forfeit_fate ORDER BY id LIMIT 1"
        ).fetchone()
        assert fate is not None
        assert fate["caliber"] == "甲"
        assert fate["status"] == "封死"


def test_path_a_frozen_short_means_full_rollback_and_yaw_refusal():
    c = TestClient(app)
    h = _auth(c)
    party = "P-A2"
    amount = 40000.0
    cage = _make_breeched_cage(c, h, party, "CG-A2-001", amount)

    # 人为制造「冻结少扣」漂移态：账户冻结列小于笼可用额
    with connect() as conn:
        conn.execute(
            "UPDATE bond_accounts SET frozen=frozen-25000 WHERE party_key=?",
            (party,),
        )
        conn.commit()
    bal0, fro0 = _acct(party)

    r = c.post("/api/forfeits", headers=h, json={"cage_id": cage["id"]})
    assert r.status_code == 409

    # 正式零改回滚：余额、冻结一列都不许动
    bal1, fro1 = _acct(party)
    assert (bal1, fro1) == (bal0, fro0)
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) c FROM forfeits WHERE cage_id=?", (cage["id"],)
            ).fetchone()["c"]
            == 0
        )
        cage_row = conn.execute(
            "SELECT available,status FROM cages WHERE id=?", (cage["id"],)
        ).fetchone()
        assert float(cage_row["available"]) == amount
        assert cage_row["status"] == "核验违约"
        # 偏航记留痕、罚没拒写
        yaw = conn.execute(
            "SELECT * FROM forfeit_yaw_log WHERE cage_id=?", (cage["id"],)
        ).fetchone()
        assert yaw is not None
        assert float(yaw["attempt_amount"]) == amount
        assert float(yaw["frozen_on_hand"]) == fro0
        # 锁必须释放，不留活跃持锁尸位
        assert (
            conn.execute(
                "SELECT COUNT(*) c FROM cage_locks WHERE lock_key=? AND released_at IS NULL",
                (f"cage:{cage['id']}",),
            ).fetchone()["c"]
            == 0
        )
