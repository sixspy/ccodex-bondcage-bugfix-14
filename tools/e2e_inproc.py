"""端到端验收·进程内段：用 TestClient 打真实文件库 data/app.db。

偏航拒写与抢笼争用需要可控的漂移/并发，置于同一进程内避免外部进程文件缓存；
终态全部回读本仓 SQLite 文件核验。运行前请停掉占库的 uvicorn，避免库级写锁互斥。
"""
from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from fastapi.testclient import TestClient

import db
import forfeit_svc
from app import app

DB = "/workspace/data/app.db"
# 句柄直指本仓文件库（不换址）
db.DB_PATH = DB
db.init_db()  # 显式迁移（TestClient 不触发 startup）
forfeit_svc.LOCK_HOLD_SECONDS = 0.5  # 确定性争用窗口

ok = True


def check(name, cond, detail=""):
    global ok
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    ok = ok and cond


def sql(query, args=()):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(query, args).fetchall()]
    c.close()
    return rows


c = TestClient(app)
tok = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
h = {"Authorization": "Bearer " + tok}
tok2 = c.post("/api/login", json={"username": "clerk", "password": "123456"}).json()["token"]
h2 = {"Authorization": "Bearer " + tok2}

# ---- 偏航拒写：取一只已冻结笼，外部（同进程直连）掏空冻结后罚没 ----
cage = next(x for x in c.get("/api/cages", headers=h).json()["items"]
            if x["cage_code"] == "CG-P-EAST-001")
party = cage["party_key"]
amount = float(cage["available"])
assert c.post("/api/attestations", headers=h,
              json={"cage_id": cage["id"], "outcome": "违约", "evidence": "偏航演练"}).status_code == 200
with db.connect() as conn:
    conn.execute("UPDATE bond_accounts SET frozen=0 WHERE party_key=?", (party,))
    conn.commit()
b0 = sql("SELECT balance,frozen FROM bond_accounts WHERE party_key=?", (party,))[0]
r = c.post("/api/forfeits", headers=h, json={"cage_id": cage["id"]})
check("漂移态罚没 409 拒写", r.status_code == 409, r.text[:100])
b1 = sql("SELECT balance,frozen FROM bond_accounts WHERE party_key=?", (party,))[0]
check("偏航零改：余额冻结不动", (b1["balance"], b1["frozen"]) == (b0["balance"], b0["frozen"]))
check("偏航正式册零增",
      sql("SELECT COUNT(*) c FROM forfeits WHERE cage_id=?", (cage["id"],))[0]["c"] == 0)
yaw = sql("SELECT * FROM forfeit_yaw_log WHERE cage_id=? ORDER BY id DESC LIMIT 1", (cage["id"],))
check("偏航记留痕且金额正确",
      len(yaw) == 1 and float(yaw[0]["attempt_amount"]) == amount and yaw[0]["frozen_on_hand"] == 0.0)
check("偏航后无活跃锁",
      sql("SELECT COUNT(*) c FROM cage_locks WHERE lock_key=? AND released_at IS NULL",
          (f"cage:{cage['id']}",))[0]["c"] == 0)

# 修正漂移后同笼可按甲路径正常罚没
with db.connect() as conn:
    conn.execute("UPDATE bond_accounts SET frozen=? WHERE party_key=?", (amount, party))
    conn.commit()
r = c.post("/api/forfeits", headers=h, json={"cage_id": cage["id"]})
check("修正后甲路径罚没 200", r.status_code == 200, r.text[:100])
b2 = sql("SELECT balance,frozen FROM bond_accounts WHERE party_key=?", (party,))[0]
check("修正后余额冻结同额降",
      round(b0["balance"] - b2["balance"], 6) == amount and float(b2["frozen"]) == 0.0)

# ---- 争用：双线程同时打同一笼罚没（用另一只笼）----
cage2 = next(x for x in c.get("/api/cages", headers=h).json()["items"]
             if x["cage_code"] == "CG-P-NORTH-001")
assert c.post("/api/attestations", headers=h,
              json={"cage_id": cage2["id"], "outcome": "违约", "evidence": "争用演练"}).status_code == 200
barrier = threading.Barrier(2)
res = {}
def hit(name, hdr):
    barrier.wait()
    rr = c.post("/api/forfeits", headers=hdr,
                json={"cage_id": cage2["id"], "reason": f"{name}争用"})
    res[name] = rr.status_code
t1 = threading.Thread(target=hit, args=("admin", h))
t2 = threading.Thread(target=hit, args=("clerk", h2))
t1.start(); t2.start(); t1.join(); t2.join()
check("双请求一成一争用", sorted(res.values()) == [200, 409], str(res))
cont = sql("SELECT * FROM contention_log WHERE lock_key=?", (f"cage:{cage2['id']}",))
check("争用留痕可查", len(cont) >= 1, str([(x["actor"], x["detail"]) for x in cont]))
check("争用后无活跃锁",
      sql("SELECT COUNT(*) c FROM cage_locks WHERE lock_key=? AND released_at IS NULL",
          (f"cage:{cage2['id']}",))[0]["c"] == 0)
check("争用下正式册仅一笔",
      sql("SELECT COUNT(*) c FROM forfeits WHERE cage_id=?", (cage2["id"],))[0]["c"] == 1)
acct = sql("SELECT balance,frozen FROM bond_accounts WHERE party_key=?", (cage2["party_key"],))[0]
check("争用后账户只降一次",
      round(float(acct["frozen"]), 6) == 0.0)

print("\n进程内段总判定:", "全部通过" if ok else "存在失败项")
raise SystemExit(0 if ok else 1)
