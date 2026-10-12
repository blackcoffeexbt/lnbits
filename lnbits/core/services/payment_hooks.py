"""Small authorization boundary; extensions own reservation and execution logic."""

import json
import time
from contextlib import asynccontextmanager
from typing import Protocol

from lnbits.core.crud import get_standalone_payment, update_payment
from lnbits.core.crud.extensions import get_installed_extension
from lnbits.core.crud.settings import get_settings_field
from lnbits.core.db import db
from lnbits.core.models import CreatePayment, Payment, PaymentState, Wallet
from lnbits.db import Connection, TransactionalConnection
from lnbits.exceptions import PaymentError
from lnbits.settings import settings
from lnbits.wallets.base import (
    PaymentFailedStatus,
    PaymentPendingStatus,
    PaymentSuccessStatus,
)

WAITING = ("queued", "review", "approved")
TAG = "payment_authorization"


class Provider(Protocol):
    async def enqueue(
        self,
        wallet: Wallet,
        data: CreatePayment,
        conn: Connection,
        origin_wallet_id: str | None,
    ) -> Payment | None: ...
    async def authorize(self, request: dict) -> bool: ...


providers: dict[str, Provider] = {}


def register(name: str, provider: Provider):
    providers[name] = provider


def unregister(name: str):
    providers.pop(name, None)


def available(name: str) -> bool:
    return (
        name in providers
        and not settings.lnbits_extensions_deactivate_all
        and name not in settings.lnbits_deactivated_extensions
    )


async def installed(name: str, conn: Connection | None = None) -> bool:
    if isinstance(conn, TransactionalConnection):
        # Serialize admission/dispatch with durable disable/uninstall.
        await conn.execute(
            "UPDATE installed_extensions SET active = active WHERE id = :id",
            {"id": name},
        )
    extension = await get_installed_extension(name, conn)
    return bool(extension and extension.active)


@asynccontextmanager
async def transaction(existing: Connection | None = None):
    async with db.transaction(existing) as conn:
        # Reuse settings storage as the durable selector and cross-worker lock.
        await conn.execute(
            """INSERT INTO system_settings (id,value,tag)
            VALUES ('provider', :value, :tag) ON CONFLICT (id,tag) DO NOTHING""",
            {"value": json.dumps(""), "tag": TAG},
        )
        await conn.execute(
            "UPDATE system_settings SET value = value "
            "WHERE id = 'provider' AND tag = :tag",
            {"tag": TAG},
        )
        yield conn


async def selected(conn: Connection | None = None) -> str:
    field = await get_settings_field("provider", TAG, conn)
    return str(field.value or "") if field else ""


async def _holds(conn: Connection) -> list[Payment]:
    return await conn.fetchall(
        "SELECT * FROM apipayments WHERE amount < 0 "
        "AND status = 'pending' AND extra LIKE :marker",
        {"marker": '%"authorization":%'},
        Payment,
    )


async def _release(payment: Payment, stage: str, conn: Connection):
    payment.status = PaymentState.FAILED
    payment.extra["authorization"]["stage"] = stage
    await update_payment(payment, conn=conn)


async def configure(provider: str):
    async with transaction() as conn:
        if provider and (
            not available(provider) or not await installed(provider, conn)
        ):
            raise PaymentError("Authorization provider is unavailable.")
        previous = await selected(conn)
        if previous != provider:
            for payment in await _holds(conn):
                envelope = payment.extra["authorization"]
                if envelope["provider"] == previous and envelope["stage"] in WAITING:
                    await _release(payment, "cancelled", conn)
        await conn.execute(
            "UPDATE system_settings SET value = :value "
            "WHERE id = 'provider' AND tag = :tag",
            {"value": json.dumps(provider), "tag": TAG},
        )


async def enqueue(
    wallet: Wallet,
    data: CreatePayment,
    conn: Connection | None,
    origin_wallet_id: str | None = None,
) -> Payment | None:
    if not await selected(conn):
        return None
    async with transaction(conn) as atomic:
        provider = await selected(atomic)
        if not provider:
            return None
        if not available(provider) or not await installed(provider, atomic):
            raise PaymentError(
                "Authorization provider is unavailable; payment blocked.",
                status="failed",
            )
        payment = await providers[provider].enqueue(
            wallet, data, atomic, origin_wallet_id
        )
        if payment is None:
            raise PaymentError(
                "Authorization provider did not reserve payment.", status="failed"
            )
        return payment


async def expire():
    if not await selected():
        return
    async with transaction() as conn:
        for payment in await _holds(conn):
            envelope = payment.extra["authorization"]
            if envelope["stage"] in WAITING and envelope["expires"] <= int(time.time()):
                await _release(payment, "expired", conn)


async def pending_status(payment: Payment, conn: Connection | None = None):
    if payment.is_in:
        return None
    envelope = payment.extra.get("authorization")
    if not envelope:
        return None
    if payment.success:
        return PaymentSuccessStatus(fee_msat=payment.fee, preimage=payment.preimage)
    if payment.failed:
        return PaymentFailedStatus()
    if envelope["stage"] in WAITING or payment.checking_id.startswith(
        "temp_authorization_"
    ):
        return PaymentPendingStatus()
    return None


async def finalize_success(payment, status, conn=None, new_checking_id=None):
    """Complete confirmed dispatch even if its extension was subsequently removed."""
    from .payments import (
        _credit_service_fee_wallet,
        _send_payment_notification_in_background,
    )

    async with transaction(conn) as atomic:
        current = await get_standalone_payment(payment.checking_id, conn=atomic)
        if (
            not current
            or current.extra.get("authorization", {}).get("id")
            != payment.extra["authorization"]["id"]
        ):
            raise PaymentError("Authorization attempt is no longer current.")
        if current.success:
            return current
        envelope = current.extra["authorization"]
        if envelope["stage"] != "dispatched":
            raise PaymentError("Payment has not been authorized for dispatch.")
        current.status = PaymentState.SUCCESS
        current.fee = -(abs(status.fee_msat or 0) + envelope["service_fee"])
        current.preimage = current.preimage or status.preimage
        envelope["stage"] = "settled"
        current = await update_payment(
            current, new_checking_id=new_checking_id, conn=atomic
        )
        await _credit_service_fee_wallet(
            current,
            conn=atomic,
            fee_msat=envelope["service_fee"],
            fee_wallet_id=envelope["fee_wallet"],
        )
        await _send_payment_notification_in_background(
            current.wallet_id, current, conn=atomic
        )
        return current
