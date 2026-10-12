"""Direct Argus WSS transport and superuser configuration API."""

import asyncio
import hmac
import json
import secrets
import time
from contextlib import suppress

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from lnbits.decorators import check_super_user

from . import payment_authorization as ledger
from . import services as argus
from .services import db

argus_router = APIRouter(prefix="/api/v1/argus", tags=["Argus"])


async def require_active():
    if not argus.active():
        raise HTTPException(503, "Argus extension is unavailable")


admin = [Depends(check_super_user), Depends(require_active)]


class Confirmation(BaseModel):
    session: str
    approve: bool


class PolicyProposal(BaseModel):
    automatic: bool = False
    transaction: int = Field(ge=0, le=2_100_000_000_000)
    hour: int = Field(ge=0, le=2_100_000_000_000)
    day: int = Field(ge=0, le=2_100_000_000_000)


@argus_router.get("/settings", dependencies=admin)
async def get_settings():
    cfg = await argus.config()
    pair = cfg.get("pair", {})
    now = int(time.time())
    return {
        **argus.Configuration(**cfg).dict(),
        "enabled": await ledger.selected() == "argus",
        "paired": bool(cfg.get("key")),
        "device": cfg.get("device", ""),
        "connected": cfg.get("seen", 0) > now - 15,
        "policy": cfg.get("policy", argus.Policy().dict()),
        "proposal": cfg.get("proposal"),
        "pairing": (
            {
                "session": pair.get("session"),
                "code": (
                    argus.pairing_code(pair["key"], pair["session"])
                    if pair.get("key")
                    else None
                ),
                "expires": pair.get("expires", 0),
                "device_confirmed": pair.get("device_confirmed", False),
                "server_confirmed": pair.get("server_confirmed", False),
            }
            if pair.get("expires", 0) > now
            else None
        ),
    }


@argus_router.put("/settings", dependencies=admin)
async def update_settings(value: argus.Configuration):
    await argus.set_enabled(value.enabled, value.timeout)
    return {"saved": True}


@argus_router.post("/pair", dependencies=admin)
async def start_pair():
    async with argus.transaction() as conn:
        cfg = await argus.config(conn)
        if cfg.get("key"):
            raise HTTPException(409, "Revoke the existing pairing first")
        cfg["pair"] = {"expires": int(time.time()) + 180}
        await argus.save_config(cfg, conn)
    return {"expires": cfg["pair"]["expires"]}


@argus_router.post("/pair/confirm", dependencies=admin)
async def confirm_pair(value: Confirmation):
    async with argus.transaction() as conn:
        cfg = await argus.config(conn)
        pair = cfg.get("pair", {})
        if pair.get("session") != value.session or pair.get("expires", 0) <= int(
            time.time()
        ):
            raise HTTPException(409, "Pairing expired")
        if value.approve:
            pair["server_confirmed"] = True
        else:
            cfg.pop("pair", None)
        await argus.save_config(cfg, conn)
    return {"confirmed": value.approve}


@argus_router.delete("/pair", dependencies=admin)
async def revoke_pair():
    await argus.revoke()
    return {"revoked": True}


@argus_router.put("/policy", dependencies=admin)
async def propose_policy(value: PolicyProposal):
    async with argus.transaction() as conn:
        cfg = await argus.config(conn)
        if not cfg.get("key"):
            raise HTTPException(409, "Pair Argus first")
        policy = argus.Policy(
            automatic=value.automatic,
            transaction=value.transaction * 1000,
            hour=value.hour * 1000,
            day=value.day * 1000,
            revision=cfg.get("policy", {}).get("revision", 0) + 1,
        )
        cfg["proposal"] = {
            "id": secrets.token_hex(16),
            "policy": policy.dict(),
            "expires": int(time.time()) + 300,
        }
        await argus.save_config(cfg, conn)
    return {"proposed": True}


@argus_router.get("/requests", dependencies=admin)
async def list_requests():
    await argus.project()
    rows = await db.fetchall(
        "SELECT id, stage, body, expires FROM argus.argus_requests "
        "ORDER BY expires DESC LIMIT 100"
    )
    return [
        {
            "id": r["id"],
            "stage": r["stage"],
            "expires": r["expires"],
            **{
                k: json.loads(r["body"])[k]
                for k in ("amount", "fees", "wallet", "memo")
            },
        }
        for r in rows
    ]


@argus_router.delete("/requests/{request_id}", dependencies=admin)
async def cancel_request(request_id: str):
    await argus.cancel(request_id)
    return {"cancelled": True}


async def receive(ws: WebSocket) -> dict:
    raw = await asyncio.wait_for(ws.receive_text(), timeout=15)
    if len(raw) > 20000:
        raise ValueError("Frame too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Object required")
    return value


async def pairing(ws: WebSocket, cfg: dict, device: str, session: str, key: str):
    """A provisional credential becomes paired only after two physical/UI confirms."""
    await ws.send_json(
        {
            "type": "pair",
            "session": session,
            "key": key,
            "instance": cfg["instance"],
            "network": cfg["network"],
            "expires": cfg["pair"]["expires"],
        }
    )
    incoming = asyncio.create_task(ws.receive_text())
    try:
        while not incoming.done():
            valid_pair(await argus.config(), session)
            await asyncio.wait({incoming}, timeout=0.25)
        raw = incoming.result()
    finally:
        if not incoming.done():
            incoming.cancel()
            with suppress(asyncio.CancelledError):
                await incoming
    if len(raw) > 1024:
        raise ValueError("Invalid confirmation")
    confirm = json.loads(raw)
    if confirm.get("type") != "confirm" or not hmac.compare_digest(
        confirm.get("proof", ""), argus.mac(key, "confirm:" + session)
    ):
        raise ValueError("Pairing not confirmed")
    async with argus.transaction() as conn:
        cfg = await argus.config(conn)
        pair = valid_pair(cfg, session)
        pair["device_confirmed"] = True
        await argus.save_config(cfg, conn)
    disconnected = asyncio.create_task(ws.receive_text())
    try:
        while True:
            if disconnected.done():
                disconnected.result()
                raise ValueError("Unexpected message during pairing")
            async with argus.transaction() as conn:
                cfg = await argus.config(conn)
                pair = valid_pair(cfg, session)
                if pair.get("server_confirmed"):
                    cfg.update(key=key, device=device, policy=argus.Policy().dict())
                    cfg.pop("pair", None)
                    await argus.save_config(cfg, conn)
                    return cfg
            await asyncio.sleep(0.5)
    finally:
        disconnected.cancel()
        with suppress(asyncio.CancelledError, WebSocketDisconnect):
            await disconnected


def valid_pair(cfg: dict, session: str):
    if not argus.active():
        raise ValueError("Argus extension stopped")
    pair = cfg.get("pair", {})
    if pair.get("session") != session or pair.get("expires", 0) <= int(time.time()):
        raise ValueError("Pairing expired or cancelled")
    return pair


async def handshake(ws: WebSocket):
    hello = await receive(ws)
    device, nonce = hello.get("device", ""), hello.get("nonce", "")
    if (
        not isinstance(device, str)
        or not isinstance(nonce, str)
        or len(device) != 32
        or len(nonce) != 32
        or any(c not in "0123456789abcdef" for c in device + nonce)
    ):
        raise ValueError("Invalid device hello")
    session = secrets.token_hex(16)
    async with argus.transaction() as conn:
        cfg = await argus.config(conn)
        if cfg.get("key"):
            if device != cfg["device"] or not hmac.compare_digest(
                hello.get("proof", ""),
                argus.mac(cfg["key"], "hello:" + device + ":" + nonce),
            ):
                raise ValueError("Unpaired device")
            key = cfg["key"]
        else:
            pair = cfg.get("pair", {})
            if pair.get("expires", 0) <= int(time.time()) or pair.get("session"):
                raise ValueError("Open Pair device in LNbits")
            key = secrets.token_hex(32)
            cfg["pair"] = {**pair, "key": key, "session": session, "device": device}
            await argus.save_config(cfg, conn)
    if not cfg.get("key"):
        cfg = await pairing(ws, cfg, device, session, key)
    await ws.send_json(
        {
            "type": "session",
            "session": session,
            "instance": cfg["instance"],
            "network": cfg["network"],
            "proof": argus.mac(
                key,
                "session:"
                + nonce
                + ":"
                + session
                + ":"
                + cfg["instance"]
                + ":"
                + cfg["network"],
            ),
        }
    )
    return key, session


async def sync_body(cfg: dict, offset: int) -> tuple[str, int]:
    await argus.project()
    # Rotate through the queue so manual requests cannot starve automatic ones.
    rows: list[dict] = (
        await db.fetchall(
            "SELECT id, body, digest FROM argus.argus_requests "
            "WHERE stage IN ('queued','review') AND expires > :now "
            "ORDER BY expires, id",
            {"now": int(time.time())},
        )
        if cfg["enabled"]
        else []
    )
    proposal = cfg.get("proposal")
    if proposal and proposal["expires"] <= int(time.time()):
        proposal = None
    active = [r["id"] for r in rows]
    offset = offset % len(rows) if rows else 0
    candidates = rows[offset:] + rows[:offset]
    selected: list[dict] = []
    body = argus.encode(
        {"type": "sync", "requests": selected, "active": active, "proposal": proposal}
    )
    for row in candidates[:32]:
        selected.append(dict(row))
        candidate = argus.encode(
            {
                "type": "sync",
                "requests": selected,
                "active": active,
                "proposal": proposal,
            }
        )
        # Bound the complete wire frame, including nested JSON escaping.
        if len(argus.encode({"body": candidate, "mac": "0" * 64})) > 19000:
            selected.pop()
            break
        body = candidate
    return body, offset + max(1, len(selected))


async def process_reply(key: str, message: dict):
    if not argus.active():
        raise ValueError("Argus extension stopped")
    await argus.project()
    async with argus.transaction() as conn:
        cfg = await argus.config(conn)
        if cfg.get("key") != key:
            raise ValueError("Pairing revoked")
        cfg["seen"] = int(time.time())
        if "policy" in message:
            cfg["policy"] = argus.Policy(**message["policy"]).dict()
        proposed = cfg.get("proposal")
        if (
            proposed
            and message.get("policy_id") == proposed["id"]
            and proposed["expires"] > int(time.time())
        ):
            cfg.pop("proposal", None)
        await argus.save_config(cfg, conn)
        # Verify the pairing and consume decisions under the same DB lock.
        for decision in message.get("decisions", [])[:32]:
            await argus.decide_locked(decision, cfg, conn)


@argus_router.websocket("/ws")
async def device_socket(ws: WebSocket):
    await ws.accept()
    if not argus.active():
        await ws.close(code=1013)
        return
    try:
        key, session = await handshake(ws)
        sent = received = offset = 0
        while True:
            if not argus.active():
                raise ValueError("Argus extension stopped")
            cfg = await argus.config()
            if cfg.get("key") != key:
                raise ValueError("Pairing revoked")
            body, offset = await sync_body(cfg, offset)
            await ws.send_json(
                {"body": body, "mac": argus.mac(key, f"server:{session}:{sent}:{body}")}
            )
            sent += 1
            frame = await receive(ws)
            body = frame.get("body", "")
            if not isinstance(body, str) or not hmac.compare_digest(
                frame.get("mac", ""),
                argus.mac(key, f"device:{session}:{received}:{body}"),
            ):
                raise ValueError("Invalid authenticated frame")
            received += 1
            message = json.loads(body)
            if not isinstance(message, dict):
                raise ValueError("Invalid reply")
            await process_reply(key, message)
            await asyncio.sleep(1)
    except (
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        asyncio.TimeoutError,
        WebSocketDisconnect,
    ):
        pass
    finally:
        try:
            await ws.close()
        except (RuntimeError, WebSocketDisconnect):
            pass
