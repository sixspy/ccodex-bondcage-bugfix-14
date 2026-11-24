"""罚没：核验违约后将笼内可用额写入罚没册。

唯一口径（甲路径，当日封死，见 forfeit_fate 纪要）：
罚没名册入账，则账户余额与冻结必须在同一事务内同额扣减；
冻结未齐扣则该笔正式零改回滚——forfeits 零增、笼可用零改、账户两列零动，
只落 forfeit_yaw_log 偏航记并拒写。不存在乙路径式「只动余额单列」。
"""
from __future__ import annotations

import os
import time

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit, write_forfeit_yaw
from locks import release, require_lock
from models import ForfeitIn

router = APIRouter(tags=["forfeit"])

# 测试探针：仅当环境变量显式给正值时，持锁后延时，供争用测确定性复现；
# 生产/常态为 0，不改变任何业务时序。
LOCK_HOLD_SECONDS = float(os.environ.get("BONDCAGE_FORFEIT_LOCK_HOLD", "0") or 0)


@router.get("/api/forfeits")
def list_forfeits(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT f.*, c.cage_code, c.contract_no
               FROM forfeits f
               JOIN cages c ON c.id=f.cage_id
               ORDER BY f.id DESC LIMIT 200"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/forfeits")
def do_forfeit(body: ForfeitIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"cage:{body.cage_id}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            cage = conn.execute(
                "SELECT * FROM cages WHERE id=?", (body.cage_id,)
            ).fetchone()
            if not cage:
                raise HTTPException(404, "笼不存在")
            if cage["status"] != "核验违约":
                raise HTTPException(409, "仅核验违约的笼可罚没")
            att = conn.execute(
                """SELECT * FROM attestations
                   WHERE cage_id=? AND attest_gen=? AND outcome='违约'
                   ORDER BY id DESC LIMIT 1""",
                (body.cage_id, int(cage["generation"])),
            ).fetchone()
            if not att:
                raise HTTPException(409, "缺少与笼代对齐的违约核验")
            avail = float(cage["available"])
            amount = float(body.amount) if body.amount is not None else avail
            if amount <= 0:
                raise HTTPException(400, "罚没金额须大于 0")
            if amount > avail + 1e-9:
                raise HTTPException(409, "罚没金额超过笼可用额")

            acct = conn.execute(
                "SELECT balance,frozen FROM bond_accounts WHERE party_key=?",
                (cage["party_key"],),
            ).fetchone()
            if not acct:
                raise HTTPException(409, "当事人保证金户不存在")
            frozen_on_hand = float(acct["frozen"])
            # 甲路径前置闸：冻结未齐扣，禁止只动余额单列
            if frozen_on_hand + 1e-9 < amount:
                conn.rollback()
                write_forfeit_yaw(
                    conn,
                    cage_id=body.cage_id,
                    cage_code=cage["cage_code"],
                    party_key=cage["party_key"],
                    attempt_amount=amount,
                    frozen_on_hand=frozen_on_hand,
                    needed_frozen=amount,
                    reason="罚没双扣偏航：冻结未齐扣，正式零改回滚并拒写",
                    actor=user,
                )
                conn.commit()
                raise HTTPException(
                    409,
                    "罚没双扣偏航：账户冻结不足以同额扣减，该笔已零改回滚并记偏航",
                )

            if LOCK_HOLD_SECONDS > 0:  # 测试探针，生产恒为 0
                time.sleep(LOCK_HOLD_SECONDS)

            now = now_iso()
            gen = int(cage["generation"])
            ref = f"FOR-{cage['cage_code']}-g{gen}"
            cur = conn.execute(
                """INSERT INTO forfeits(cage_id,amount,reason,cage_generation,status,forfeit_ref,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    body.cage_id,
                    amount,
                    body.reason or "履约违约",
                    gen,
                    "已罚没",
                    ref,
                    now,
                ),
            )
            new_avail = round(avail - amount, 6)
            new_status = "已罚没" if new_avail <= 1e-9 else "部分罚没"
            conn.execute(
                "UPDATE cages SET available=?, status=? WHERE id=?",
                (max(new_avail, 0.0), new_status, body.cage_id),
            )
            # 甲路径核心：余额与冻结同事务、同额扣减（一条语句同一成败边界）
            cur_acct = conn.execute(
                """UPDATE bond_accounts
                   SET balance = balance - ?, frozen = frozen - ?
                   WHERE party_key=? AND frozen + 1e-9 >= ?""",
                (amount, amount, cage["party_key"], amount),
            )
            if cur_acct.rowcount != 1:
                # 兜底：并发漂移导致冻结不再足额，整笔回滚不留半截账
                conn.rollback()
                write_forfeit_yaw(
                    conn,
                    cage_id=body.cage_id,
                    cage_code=cage["cage_code"],
                    party_key=cage["party_key"],
                    attempt_amount=amount,
                    frozen_on_hand=frozen_on_hand,
                    needed_frozen=amount,
                    reason="罚没双扣偏航：同额双扣条件更新失败，整笔回滚",
                    actor=user,
                )
                conn.commit()
                raise HTTPException(
                    409,
                    "罚没双扣偏航：同事务双扣未达成，该笔已零改回滚并记偏航",
                )
            write_audit(
                conn,
                user,
                "cage_forfeit",
                cage["cage_code"],
                {
                    "forfeit_id": cur.lastrowid,
                    "amount": amount,
                    "reason": body.reason,
                    "cage_generation": gen,
                    "forfeit_ref": ref,
                    "double_deduct": "balance/frozen 同事务同额",
                },
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM forfeits WHERE id=?", (cur.lastrowid,)
            ).fetchone()
            return dict(row)
        finally:
            release(conn, lock_key, user)
            conn.commit()
