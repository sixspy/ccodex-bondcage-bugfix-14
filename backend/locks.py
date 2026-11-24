"""笼锁与争用登记（公共并发面）。"""
from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit

router = APIRouter(tags=["locks"])

# 无竞争 BEGIN IMMEDIATE 为微秒级（实测<0.01ms）；超过该阈值即曾等待他人写锁。
# 用于覆盖「等待结束时先到者已提交释放、活跃锁查不到」的窗口。
CONTENTION_WAIT_SECONDS = 0.05


class LockIn(BaseModel):
    lock_key: str
    action: str = "acquire"


def _log_contention(conn, lock_key: str, holder: str, detail: str, now: str) -> None:
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, holder, "争用", detail, now),
    )


def try_acquire(conn, lock_key: str, holder: str) -> bool:
    """原子抢锁：活跃锁 = released_at 为空的行，每把锁同一时刻至多一条活跃行。

    双判据：
    1. BEGIN IMMEDIATE 取库级写锁，串行化所有抢锁者；若等待超过阈值，说明
       先到者正持写锁，即使其随后提交释放，也凭「等待过」判争用；
    2. 取得库锁后查活跃锁行，他人仍持锁即争用；自持同锁视为重入。
    """
    now = now_iso()
    conn.commit()
    t0 = time.perf_counter()
    conn.execute("BEGIN IMMEDIATE")
    waited = (time.perf_counter() - t0) > CONTENTION_WAIT_SECONDS

    active = conn.execute(
        "SELECT holder FROM cage_locks WHERE lock_key=? AND released_at IS NULL",
        (lock_key,),
    ).fetchone()
    if active is not None and active["holder"] != holder:
        conn.rollback()
        _log_contention(
            conn, lock_key, holder, f"持有者={active['holder']}", now
        )
        conn.commit()
        return False
    if active is not None and active["holder"] == holder:
        return True  # 同一持有者重入
    if waited:
        # 活跃锁已查不到，但确曾等待先到者的写锁：判争用，不接管
        conn.rollback()
        _log_contention(
            conn, lock_key, holder, "等待写锁后原锁已释放，后到争用拒入", now
        )
        conn.commit()
        return False
    conn.execute(
        "INSERT INTO cage_locks(lock_key,holder,acquired_at,released_at) VALUES (?,?,?,NULL)",
        (lock_key, holder, now),
    )
    return True


def release(conn, lock_key: str, holder: str) -> None:
    # 只释放自己仍持有的活跃锁；标记释放而非物理删除，保留持锁历史
    conn.execute(
        """UPDATE cage_locks SET released_at=?
           WHERE lock_key=? AND holder=? AND released_at IS NULL""",
        (now_iso(), lock_key, holder),
    )


def require_lock(conn, lock_key: str, holder: str) -> None:
    if not try_acquire(conn, lock_key, holder):
        raise HTTPException(409, "争用：后到只记争用，不写业务")


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM cage_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        ok = try_acquire(conn, body.lock_key, user)
        write_audit(
            conn,
            user,
            "lock_acquire",
            body.lock_key,
            {"ok": ok},
        )
        conn.commit()
        if not ok:
            raise HTTPException(409, "争用：未能获得锁")
        return {"ok": True, "lock_key": body.lock_key, "holder": user}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        release(conn, body.lock_key, user)
        write_audit(conn, user, "lock_release", body.lock_key, {})
        conn.commit()
        return {"ok": True}
