import asyncio
import json
from typing import cast

import pytest
from fastapi import HTTPException, WebSocket
from starlette.websockets import WebSocketDisconnect

from lnbits.extensions.argus import services as argus
from lnbits.extensions.argus.views_api import (
    Confirmation,
    confirm_pair,
    device_socket,
    get_settings,
    start_pair,
)

pytestmark = pytest.mark.usefixtures("argus_extension")


class Device:
    """An asynchronous transport peer; no physical device or live payment."""

    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()

    async def accept(self):
        pass

    async def receive_text(self):
        value = await self.incoming.get()
        if value is None:
            raise WebSocketDisconnect()
        return json.dumps(value)

    async def send_json(self, value):
        await self.outgoing.put(value)

    async def close(self):
        await self.outgoing.put({"closed": True})

    async def next(self):
        return await asyncio.wait_for(self.outgoing.get(), timeout=3)


@pytest.mark.anyio
async def test_argus_settings_require_superuser(client):
    assert (await client.get("/api/v1/argus/settings")).status_code in (401, 403)
    assert (await client.post("/api/v1/argus/pair")).status_code in (401, 403)


@pytest.mark.anyio
async def test_argus_pairing_requires_both_confirmations_and_authenticates_frames(app):
    original = await argus.config()
    async with argus.transaction() as conn:
        cfg = {**original, "enabled": False}
        for field in ("key", "pair", "device"):
            cfg.pop(field, None)
        await argus.save_config(cfg, conn)
    device = Device()
    task = None
    try:
        await start_pair()
        await device.incoming.put({"device": "ab" * 16, "nonce": "cd" * 16})
        task = asyncio.create_task(device_socket(cast(WebSocket, device)))
        pair = await device.next()
        assert pair["type"] == "pair"
        info = await get_settings()
        assert info["pairing"]["code"] == argus.pairing_code(
            pair["key"], pair["session"]
        )
        assert not info["paired"]
        await confirm_pair(Confirmation(session=pair["session"], approve=True))
        assert not (await get_settings())["paired"]
        await device.incoming.put(
            {
                "type": "confirm",
                "proof": argus.mac(pair["key"], "confirm:" + pair["session"]),
            }
        )
        session = await device.next()
        assert session["type"] == "session"
        assert (await get_settings())["paired"]
        sync = await device.next()
        assert sync["mac"] == argus.mac(
            pair["key"], f"server:{session['session']}:0:{sync['body']}"
        )
        # An invalid MAC closes the connection, never processes supplied decisions.
        await device.incoming.put({"body": '{"decisions":[]}', "mac": "00" * 32})
        assert await device.next() == {"closed": True}
        await task
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with argus.transaction() as conn:
            await argus.save_config(original, conn)


@pytest.mark.anyio
async def test_argus_reject_pairing_cannot_be_confirmed_later(app):
    original = await argus.config()
    try:
        async with argus.transaction() as conn:
            cfg = {
                **original,
                "enabled": False,
                "pair": {"session": "12" * 16, "expires": 2_000_000_000},
            }
            cfg.pop("key", None)
            await argus.save_config(cfg, conn)
        await confirm_pair(Confirmation(session="12" * 16, approve=False))
        with pytest.raises(HTTPException) as rejected:
            await confirm_pair(Confirmation(session="12" * 16, approve=True))
        assert rejected.value.status_code == 409
        assert not (await get_settings())["paired"]
    finally:
        async with argus.transaction() as conn:
            await argus.save_config(original, conn)


@pytest.mark.anyio
async def test_argus_extension_page_and_core_recovery_without_extension(
    client, superuser_token
):
    from lnbits.extensions.argus import payment_authorization as ledger

    headers = {"Authorization": f"Bearer {superuser_token}"}
    original = await argus.config()
    try:
        async with argus.transaction() as conn:
            await argus.save_config({**original, "key": "ab" * 32}, conn)
        enabled = await client.put(
            "/api/v1/argus/settings",
            headers=headers,
            json={"enabled": True, "timeout": 300},
        )
        assert enabled.status_code == 200
        redirect = await client.get("/argus", headers=headers)
        assert redirect.status_code == 307
        assert redirect.headers["location"].endswith("/argus/")
        page = await client.get("/argus/", headers=headers)
        assert page.status_code == 200
        assert "argus-settings" in page.text and "/argus/static/index.js" in page.text
        assert (await client.get("/argus/static/index.js")).status_code == 200
        await argus.stop()
        selection = await client.get(
            "/admin/api/v1/payment-authorization", headers=headers
        )
        assert selection.status_code == 200
        assert selection.json() == {"provider": "argus", "available": False}
        assert (
            await client.get("/api/v1/argus/settings", headers=headers)
        ).status_code == 503
        disabled = await client.put(
            "/admin/api/v1/payment-authorization",
            headers=headers,
            json={"provider": ""},
        )
        assert disabled.status_code == 200 and await ledger.selected() == ""
    finally:
        async with argus.transaction() as conn:
            await argus.save_config(original, conn)
