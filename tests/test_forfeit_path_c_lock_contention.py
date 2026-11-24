"""争用失败测丙：两名经办对同一笼同时提交罚没，须抢同一把笼锁。

口径：起真实 uvicorn 服务（库句柄指 tmp 下 SQLite 文件），
两个请求线程在同一屏障后经真实 HTTP 同时打罚没提交。
后到争用：HTTP 409、contention_log 可查、不写业务；
先到者按甲路径同事务双扣成功，全笼只允许一笔罚没、账户只降一次。
"""
from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn

import db
from app import app


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    # 确定性争用：持锁窗口打开 0.5 秒（生产环境变量缺省为 0，时序不变）
    monkeypatch.setattr("forfeit_svc.LOCK_HOLD_SECONDS", 0.5)
    db.init_db()

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server_ = uvicorn.Server(config)
    thread = threading.Thread(target=server_.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:  # pragma: no cover
        raise RuntimeError("测试服务未就绪")
    yield base
    server_.should_exit = True
    thread.join(timeout=10)


def _token(base: str, user: str) -> str:
    r = httpx.post(
        base + "/api/login",
        json={"username": user, "password": "123456"},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _setup_breeched_cage(base: str) -> tuple[int, str, float]:
    h = {"Authorization": "Bearer " + _token(base, "admin")}
    party = "P-C"
    amount = 50000.0
    r = httpx.post(
        base + "/api/bonds",
        headers=h,
        json={"party_key": party, "party_name": "争用口径户", "initial_deposit": 100000},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    r = httpx.post(
        base + "/api/cages/freeze",
        headers=h,
        json={
            "party_key": party,
            "contract_no": "HT-C-1",
            "amount": amount,
            "beneficiary": "受益方丙",
            "cage_code": "CG-C-001",
        },
        timeout=10,
    )
    assert r.status_code == 200, r.text
    cage_id = r.json()["id"]
    r = httpx.post(
        base + "/api/attestations",
        headers=h,
        json={"cage_id": cage_id, "outcome": "违约", "evidence": "逾期"},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    return cage_id, party, amount


def test_two_clerks_concurrent_forfeit_one_wins_one_contends(server):
    base = server
    cage_id, party, amount = _setup_breeched_cage(base)
    tokens = {"admin": _token(base, "admin"), "clerk": _token(base, "clerk")}
    barrier = threading.Barrier(2)
    results: dict[str, dict] = {}
    errors: dict[str, object] = {}

    def worker(actor: str) -> None:
        h = {"Authorization": "Bearer " + tokens[actor]}
        barrier.wait()
        try:
            r = httpx.post(
                base + "/api/forfeits",
                headers=h,
                json={"cage_id": cage_id, "reason": f"{actor}同时罚没"},
                timeout=15,
            )
            results[actor] = {"status": r.status_code, "body": r.text[:300]}
        except Exception as exc:  # pragma: no cover - 并发异常须可见
            errors[actor] = repr(exc)

    t1 = threading.Thread(target=worker, args=("admin",))
    t2 = threading.Thread(target=worker, args=("clerk",))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)
    assert not errors, errors

    statuses = sorted(v["status"] for v in results.values())
    assert statuses == [200, 409], results
    winner = next(a for a, v in results.items() if v["status"] == 200)
    loser = next(a for a, v in results.items() if v["status"] == 409)
    # 后到者必须是抢锁失败，而不是业务状态变化导致的 409
    assert "争用" in results[loser]["body"], results[loser]

    with db.connect() as conn:
        # 后到争用可查
        cont = conn.execute(
            "SELECT * FROM contention_log WHERE lock_key=?",
            (f"cage:{cage_id}",),
        ).fetchall()
        assert len(cont) >= 1
        assert any(r["actor"] == loser for r in cont)
        # 正式册只入一笔
        frows = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM forfeits WHERE cage_id=?",
            (cage_id,),
        ).fetchone()
        assert frows["c"] == 1
        assert float(frows["s"]) == amount
        # 账户只降一次：余额、冻结各降同一笔
        acct = conn.execute(
            "SELECT balance,frozen FROM bond_accounts WHERE party_key=?", (party,)
        ).fetchone()
        assert round(float(acct["balance"]), 6) == round(100000.0 - amount, 6)
        assert round(float(acct["frozen"]), 6) == 0.0
        # 赢家收尾后锁已释放，无活跃持锁尸位
        assert (
            conn.execute(
                "SELECT COUNT(*) c FROM cage_locks WHERE lock_key=? AND released_at IS NULL",
                (f"cage:{cage_id}",),
            ).fetchone()["c"]
            == 0
        )
        # 输家未留任何审计业务写（仅争用登记）
        loser_audits = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE actor=? AND action='cage_forfeit'",
            (loser,),
        ).fetchone()["c"]
        assert loser_audits == 0

    # 输家随后可正常开新户，证明争用失败未污染会话
    r = httpx.post(
        base + "/api/bonds",
        headers={"Authorization": "Bearer " + tokens[loser]},
        json={"party_key": "P-C2", "party_name": "输家再开一户", "initial_deposit": 1000},
        timeout=10,
    )
    assert r.status_code == 200
    assert winner in ("admin", "clerk")
