"""Argus protocol state and decisions, stored in ext_argus.sqlite3.

Never call core ledger operations while holding an extension transaction.
"""

import asyncio
import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager

from bolt11 import decode
from pydantic import BaseModel, Field, root_validator

from lnbits.db import Database
from lnbits.exceptions import PaymentError
from lnbits.task_manager import task_manager

from . import payment_authorization as ledger

db = Database("ext_argus")
WAITING = ("queued", "review", "approved")
MAX_MSAT = 2_100_000_000_000_000_000


class Policy(BaseModel):
    automatic: bool = False
    transaction: int = Field(default=0, ge=0, le=MAX_MSAT)
    hour: int = Field(default=0, ge=0, le=MAX_MSAT)
    day: int = Field(default=0, ge=0, le=MAX_MSAT)
    revision: int = Field(default=0, ge=0)

    @root_validator
    def enabled_limits(cls, values):
        if values.get("automatic") and not all(
            values.get(k) for k in ("transaction", "hour", "day")
        ):
            raise ValueError("Automatic authorization requires all three limits")
        return values


class Configuration(BaseModel):
    enabled: bool = False
    timeout: int = Field(default=300, ge=30, le=3600)


def encode(value: dict) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def digest(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def mac(key: str, message: str) -> str:
    return hmac.new(bytes.fromhex(key), message.encode(), hashlib.sha256).hexdigest()


def pairing_code(key: str, session: str) -> str:
    return f"{int(mac(key, 'argus-pair-v1:' + session)[:8], 16) % 1000000:06d}"


@asynccontextmanager
async def transaction():
    async with db.connect() as base:
        async with db.transaction(base) as conn:
            await conn.execute("UPDATE argus.argus_config SET id = id WHERE id = 1")
            yield conn


async def config(conn=None) -> dict:
    row = await (conn or db).fetchone(
        "SELECT data FROM argus.argus_config WHERE id = 1"
    )
    return json.loads(row["data"])


async def save_config(value: dict, conn):
    await conn.execute(
        "UPDATE argus.argus_config SET data = :data WHERE id = 1",
        {"data": encode(value)},
    )


class ArgusProvider:
    async def enqueue(self, wallet, data, conn, origin_wallet_id):
        return await ledger.enqueue(wallet, data, conn, origin_wallet_id)

    async def prepare(self, context: dict) -> dict:
        cfg = await config()
        if not cfg["enabled"] or not cfg.get("key"):
            raise PaymentError(
                "Pair and enable Argus before sending payments.", status="failed"
            )
        if context["invoice"].lower().startswith("lno1"):
            raise PaymentError(
                "BOLT12 offers are not supported with Argus authorization enabled.",
                status="failed",
            )
        if len(context["invoice"]) > 4096:
            raise PaymentError("Invoice exceeds Argus request limit.", status="failed")
        invoice = decode(context["invoice"])
        return {
            **context,
            "instance": cfg["instance"],
            "network": {"bc": "Mainnet", "tb": "Testnet", "bcrt": "Regtest"}.get(
                invoice.currency, invoice.currency
            ),
            "recipient": invoice.payee or "",
            "expires": min(
                int(time.time()) + cfg["timeout"],
                context["expires"] or int(time.time()) + cfg["timeout"],
            ),
        }

    async def authorize(self, request: dict) -> bool:
        async with transaction() as conn:
            cfg = await config(conn)
            row = await conn.fetchone(
                "SELECT * FROM argus.argus_requests WHERE id = :id",
                {"id": request["id"]},
            )
            return bool(
                cfg["enabled"]
                and cfg.get("key")
                and row
                and json.loads(request["body"]).get("instance") == cfg["instance"]
                and row["stage"] == "approved"
                and row["digest"] == request["digest"]
                and row["body"] == request["body"]
                and row["expires"] > int(time.time())
            )


provider = ArgusProvider()


def active() -> bool:
    return ledger.available("argus")


async def project():
    # Core commit is the durable outbox. Losing an extension insert is recoverable.
    snapshots = await ledger.requests("argus")
    active_rows = await db.fetchall(
        "SELECT id, checking_id FROM argus.argus_requests "
        "WHERE stage IN ('queued','review','approved','dispatched')"
    )
    known = {row["id"] for row in snapshots}
    for row in active_rows:
        if row["id"] not in known:
            current = await ledger.request(row["id"])
            if current:
                snapshots.append(current)
            else:
                # Retried payments replace their old request ID; never replay it.
                await db.execute(
                    "UPDATE argus.argus_requests SET stage = 'cancelled' "
                    "WHERE id = :id",
                    {"id": row["id"]},
                )
    async with transaction() as conn:
        for row in snapshots:
            exists = await conn.fetchone(
                "SELECT stage FROM argus.argus_requests WHERE id = :id",
                {"id": row["id"]},
            )
            if not exists:
                await conn.execute(
                    """INSERT INTO argus.argus_requests
                    (id, checking_id, body, digest, stage, expires,
                     route_fee, service_fee)
                    VALUES (:id, :checking_id, :body, :digest, :stage,
                            :expires, :route_fee, :service_fee)""",
                    {
                        key: row[key]
                        for key in (
                            "id",
                            "checking_id",
                            "body",
                            "digest",
                            "stage",
                            "expires",
                            "route_fee",
                            "service_fee",
                        )
                    },
                )
            elif row["stage"] not in ledger.WAITING:
                await conn.execute(
                    "UPDATE argus.argus_requests SET stage = :stage, "
                    "checking_id = :checking_id WHERE id = :id",
                    {
                        "id": row["id"],
                        "stage": row["stage"],
                        "checking_id": row["checking_id"],
                    },
                )


async def decide(message: dict):
    await project()
    async with transaction() as conn:
        cfg = await config(conn)
        await decide_locked(message, cfg, conn)
    if message.get("decision") == "rejected":
        await ledger.release(message["id"], "argus", "rejected")
        await project()


async def decide_locked(message: dict, cfg: dict, conn):
    row = await conn.fetchone(
        "SELECT * FROM argus.argus_requests WHERE id = :id",
        {"id": message.get("id", "")},
    )
    if not row or row["stage"] not in ("queued", "review"):
        return
    if not cfg["enabled"] or row["expires"] <= int(time.time()):
        stage = "expired"
    else:
        if message.get("digest") != row["digest"]:
            raise ValueError("Authorization binding mismatch")
        stage = str(message.get("decision", ""))
        if stage not in ("approved", "rejected", "review"):
            raise ValueError("Invalid authorization decision")
    await conn.execute(
        "UPDATE argus.argus_requests SET stage = :stage WHERE id = :id",
        {"stage": stage, "id": row["id"]},
    )


async def cancel(request_id: str):
    await ledger.release(request_id, "argus", "cancelled")
    await project()


async def tick():
    if not active():
        return
    await project()
    # Snapshot decisions, then release the extension lock before touching core.
    rows = await db.fetchall(
        "SELECT * FROM argus.argus_requests "
        "WHERE stage IN ('queued','review','approved','rejected','expired')"
    )
    for row in rows:
        if row["stage"] in ("rejected", "expired"):
            await ledger.release(row["id"], "argus", row["stage"])
        elif row["expires"] <= int(time.time()):
            await ledger.release(row["id"], "argus", "expired")
        elif row["stage"] == "approved":
            name = f"argus_execute_{row['id']}"
            if not task_manager.get_task(name):
                task_manager.create_task(ledger.execute(row["id"]), name)


async def execute(request_id: str):
    await ledger.execute(request_id)
    await project()


async def set_enabled(enabled: bool, timeout: int):
    async with transaction() as conn:
        cfg = await config(conn)
        if enabled and not cfg.get("key"):
            raise PaymentError("Pair Argus first.")
        cfg.update(enabled=enabled, timeout=timeout)
        await save_config(cfg, conn)
    await ledger.configure("argus" if enabled else "")
    await project()


async def revoke():
    async with transaction() as conn:
        cfg = await config(conn)
        for key in ("key", "device", "pair", "seen", "policy", "proposal"):
            cfg.pop(key, None)
        await save_config(cfg, conn)
    await ledger.cancel_provider("argus")
    await project()


async def stop():
    ledger.unregister("argus")
    jobs = [
        task
        for task in list(task_manager.tasks)
        if task.name == "argus_tick" or task.name.startswith("argus_execute_")
    ]
    for task in jobs:
        task_manager.cancel_task(task)
    await asyncio.gather(*(task.task for task in jobs), return_exceptions=True)
    # Never clear durable core provider selection here.
