"""事故复现脚本（红前）：在本仓 SQLite 文件库的副本上走完整罚没链路。

口径：不改原件 data/app.db，复制到独立临时副本后由后端服务直接打开。
复现 BUGFIX-14 埋雷：罚没只减余额、冻结少扣/不扣，且对账互证仍亮绿。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path("/workspace")
sys.path.insert(0, str(REPO / "backend"))

import db  # noqa: E402

SRC = REPO / "data" / "app.db"


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="bondcage-repro-"))
    clone = workdir / "app.db"
    shutil.copy2(SRC, clone)
    db.DB_PATH = clone
    print(f"[repro] 原件 {SRC} 只读复制到 {clone}")

    from fastapi.testclient import TestClient

    from app import app

    with TestClient(app) as c:
        tok = c.post(
            "/api/login", json={"username": "admin", "password": "123456"}
        ).json()["token"]
        h = {"Authorization": "Bearer " + tok}

        cage = next(
            x
            for x in c.get("/api/cages", headers=h).json()["items"]
            if x["cage_code"] == "CG-P-SOUTH-001"
        )
        party = cage["party_key"]
        cage_id = cage["id"]
        amount = float(cage["available"])
        acct0 = c.get(f"/api/bonds/{party}", headers=h).json()["account"]
        print(
            f"[repro] 事故前 {party}: 余额={acct0['balance']} 冻结={acct0['frozen']}"
        )

        r = c.post(
            "/api/attestations",
            headers=h,
            json={"cage_id": cage_id, "outcome": "违约", "evidence": "逾期复现"},
        )
        assert r.status_code == 200, r.text
        print("[repro] 核验违约已写入")

        r = c.post(
            "/api/forfeits",
            headers=h,
            json={"cage_id": cage_id, "reason": "复现双扣偏航"},
        )
        assert r.status_code == 200, r.text
        f = r.json()
        print(f"[repro] 罚没入册: id={f['id']} amount={f['amount']} status={f['status']}")

        acct1 = c.get(f"/api/bonds/{party}", headers=h).json()["account"]
        print(f"[repro] 罚没后 {party}: 余额={acct1['balance']} 冻结={acct1['frozen']}")
        bal_drop = acct0["balance"] - acct1["balance"]
        fro_drop = acct0["frozen"] - acct1["frozen"]
        print(f"[repro] 余额降幅={bal_drop} 冻结降幅={fro_drop} 罚没额={amount}")

        proof = c.get(f"/api/proof/{party}", headers=h).json()
        print(
            "[repro] 当事人互证: frozen_matches_live=%s 左(冻结)=%s 右(活笼)=%s"
            % (
                proof["frozen_matches_live"],
                proof["proof"]["left"],
                proof["proof"]["right"],
            )
        )
        recon = c.get(f"/api/reconcile/{cage['cage_code']}", headers=h).json()
        print(
            f"[repro] 笼对账: balanced={recon['balanced']} "
            f"账户冻结={recon['account_frozen']} 活笼可用={recon['live_cage_available']}"
        )

    drift = abs(fro_drop - amount) > 1e-9
    fake_green = proof["frozen_matches_live"] is True and abs(
        proof["proof"]["left"] - proof["proof"]["right"]
    ) > 1e-9
    print(f"[repro] 冻结未齐扣={drift} 互证假绿={fake_green}")
    if drift and fake_green:
        print("[repro] 事故复现成功：余额降而冻结漂，互证仍亮绿")
    else:
        print("[repro] 未复现（可能已修复）")


if __name__ == "__main__":
    main()
