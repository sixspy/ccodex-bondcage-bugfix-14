"""端到端验收·真实 HTTP 段：对运行中的本机服务（库句柄指 data/app.db）。

只走服务自身 HTTP 写入（不做跨进程直连改库，避开文件系统缓存边界）。
覆盖五页数据面、真实罚没同事务双扣、对账真绿、命运纪要封死。
偏航拒写与抢笼争用见 e2e_inproc.py。
"""
from __future__ import annotations

import json
import sqlite3

import httpx

BASE = "http://127.0.0.1:11339"
DB = "/workspace/data/app.db"
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


r = httpx.post(BASE + "/api/login", json={"username": "admin", "password": "123456"}, timeout=10)
assert r.status_code == 200, r.text
h = {"Authorization": "Bearer " + r.json()["token"]}

# 五页数据面
for name, path in [
    ("罚没页-名册", "/api/forfeits"),
    ("罚没页-命运纪要", "/api/forfeit-fate"),
    ("罚没页-偏航记", "/api/forfeit-yaw"),
    ("账户页", "/api/bonds"),
    ("对账页-笼对账", "/api/reconcile"),
    ("入笼页", "/api/cages"),
    ("审计页-审计", "/api/audit"),
    ("审计页-争用", "/api/contention"),
    ("审计页-持锁", "/api/locks"),
]:
    check(name, httpx.get(BASE + path, headers=h, timeout=10).status_code == 200)

fate = httpx.get(BASE + "/api/forfeit-fate", headers=h).json()
check("命运口径封死为甲", fate["caliber"] == "甲" and fate["status"] == "封死",
      json.dumps(fate, ensure_ascii=False))

# 真实罚没双扣
cage = next(x for x in httpx.get(BASE + "/api/cages", headers=h).json()["items"]
            if x["cage_code"] == "CG-P-SOUTH-001")
party, amount = cage["party_key"], float(cage["available"])
a0 = sql("SELECT balance,frozen FROM bond_accounts WHERE party_key=?", (party,))[0]
assert httpx.post(BASE + "/api/attestations", headers=h,
                  json={"cage_id": cage["id"], "outcome": "违约", "evidence": "端到端违约"}).status_code == 200
r = httpx.post(BASE + "/api/forfeits", headers=h,
               json={"cage_id": cage["id"], "reason": "端到端罚没双扣"})
check("罚没入册 200", r.status_code == 200, r.text[:100])
a1 = sql("SELECT balance,frozen FROM bond_accounts WHERE party_key=?", (party,))[0]
check("余额同额降", round(a0["balance"] - a1["balance"], 6) == amount,
      f"{a0['balance']}->{a1['balance']} 应降 {amount}")
check("冻结同额降", round(a0["frozen"] - a1["frozen"], 6) == amount,
      f"{a0['frozen']}->{a1['frozen']} 应降 {amount}")
check("余额冻结降幅一致",
      round((a0["balance"] - a1["balance"]) - (a0["frozen"] - a1["frozen"]), 6) == 0)
frow = sql("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM forfeits WHERE cage_id=? AND status='已罚没'",
           (cage["id"],))[0]
check("罚没正式册一笔同额", frow["c"] == 1 and float(frow["s"]) == amount)

# 对账真绿（非硬编码）
proof = httpx.get(BASE + f"/api/proof/{party}", headers=h).json()
check("互证账实相符真绿",
      proof["frozen_matches_live"] is True
      and abs(proof["proof"]["left"] - proof["proof"]["right"]) < 1e-6,
      json.dumps(proof["proof"], ensure_ascii=False))
recon = httpx.get(BASE + f"/api/reconcile/{cage['cage_code']}", headers=h).json()
check("笼对账平衡且冻结合规", recon["balanced"] is True and recon["frozen_consistent"] is True)
bonds = {b["party_key"]: b for b in httpx.get(BASE + "/api/bonds", headers=h).json()["items"]}
check("账户页冻结合规字段为真", bonds[party]["frozen_consistent"] is True)
allrecon = httpx.get(BASE + "/api/reconcile", headers=h).json()
check("对账汇总含冻结漂移计数", "frozen_drift" in allrecon["summary"])

print("\n真实 HTTP 段总判定:", "全部通过" if ok else "存在失败项")
raise SystemExit(0 if ok else 1)
