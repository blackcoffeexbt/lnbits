"""Durable payment authorization envelopes; providers never own the ledger.

Only local database work runs under the core transaction. Providers must never
call core services while holding their own database transaction.
"""

import hashlib
import json
import secrets
import time
from datetime import datetime, timezone

from lnbits.core.crud import (
    check_internal,
    create_payment,
    get_standalone_payment,
    get_wallet,
    update_payment,
)
from lnbits.core.db import db
from lnbits.core.models import CreatePayment, Payment, PaymentState, Wallet
from lnbits.core.services import payment_hooks as hooks
from lnbits.core.services.payment_hooks import (
    available as available,
)
from lnbits.core.services.payment_hooks import (
    configure as configure,
)
from lnbits.core.services.payment_hooks import (
    expire as expire,
)
from lnbits.core.services.payment_hooks import (
    pending_status as pending_status,
)
from lnbits.core.services.payment_hooks import (
    register as register,
)
from lnbits.core.services.payment_hooks import (
    selected as selected,
)
from lnbits.core.services.payment_hooks import (
    transaction as transaction,
)
from lnbits.core.services.payment_hooks import (
    unregister as unregister,
)
from lnbits.db import Connection
from lnbits.exceptions import PaymentError
from lnbits.settings import settings
from lnbits.task_manager import task_manager

providers = hooks.providers
WAITING = hooks.WAITING


def encode(value: dict) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def digest(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


async def requests(
    provider: str, conn: Connection | None = None, pending: bool = True
) -> list[dict]:
    payments = await (conn or db).fetchall(
        "SELECT * FROM apipayments WHERE amount < 0 AND extra LIKE :marker "
        "AND (:pending = false OR status = 'pending')",
        {"marker": '%"authorization":%', "pending": pending},
        Payment,
    )
    return [
        {**p.extra["authorization"], "checking_id": p.checking_id}
        for p in payments
        if p.extra.get("authorization", {}).get("provider") == provider
    ]


async def request(request_id: str, conn: Connection | None = None) -> dict | None:
    if len(request_id) != 32 or any(c not in "0123456789abcdef" for c in request_id):
        return None
    payment = await get_standalone_payment(
        "temp_authorization_" + request_id, conn=conn
    )
    if payment and payment.extra.get("authorization", {}).get("id") == request_id:
        return {**payment.extra["authorization"], "checking_id": payment.checking_id}
    # Backend references replace the temporary ID after dispatch; only then scan.
    payments = await (conn or db).fetchall(
        "SELECT * FROM apipayments WHERE amount < 0 AND extra LIKE :marker",
        {"marker": "%" + request_id + "%"},
        Payment,
    )
    for payment in payments:
        envelope = payment.extra.get("authorization", {})
        if envelope.get("id") == request_id:
            return {**envelope, "checking_id": payment.checking_id}
    return None


async def set_stage(row: dict, stage: str, conn: Connection):
    payment = await get_standalone_payment(row["checking_id"], conn=conn)
    if not payment or payment.extra.get("authorization", {}).get("id") != row["id"]:
        return
    payment.extra["authorization"]["stage"] = stage
    if stage in ("rejected", "expired", "cancelled"):
        payment.status = PaymentState.FAILED
    await update_payment(payment, conn=conn)


async def cancel_provider(provider: str):
    async with transaction() as conn:
        for row in await requests(provider, conn):
            if row["stage"] in WAITING:
                await set_stage(row, "cancelled", conn)


async def release(request_id: str, provider: str, stage: str):
    if stage not in ("rejected", "expired", "cancelled"):
        raise ValueError("Invalid terminal authorization state")
    async with transaction() as conn:
        row = await request(request_id, conn)
        if row and row["provider"] == provider and row["stage"] in WAITING:
            await set_stage(row, stage, conn)


async def enqueue(  # noqa: C901
    wallet: Wallet,
    data: CreatePayment,
    base: Connection | None,
    origin_wallet_id: str | None = None,
) -> Payment | None:
    from lnbits.core.services.payments import fee_reserve, fee_reserve_total

    from .services import provider as handler

    async with transaction(base) as conn:
        provider = await selected(conn)
        if not provider:
            return None
        if not available(provider):
            raise PaymentError(
                "Authorization provider is unavailable; payment blocked.",
                status="failed",
            )
        old = await previous_attempt(wallet.id, data.payment_hash, conn)
        if old and old.pending:
            return old
        current_wallet = await get_wallet(wallet.id, conn=conn)
        if not current_wallet or not current_wallet.can_send_payments:
            raise PaymentError("Wallet cannot send payments.", status="failed")
        wallet = current_wallet
        await check_hard_caps(wallet.id, abs(data.amount_msat), conn)
        internal = await check_internal(data.payment_hash, conn=conn)
        total_fee = fee_reserve_total(data.amount_msat, internal=bool(internal))
        amount = abs(data.amount_msat)
        if wallet.balance_msat < amount + total_fee:
            raise PaymentError("Insufficient balance including fees.", status="failed")
        if len(await requests(provider, conn)) >= 128:
            raise PaymentError(
                "Authorization queue is full; try again later.", status="failed"
            )
        context: dict = {
            "id": secrets.token_hex(16),
            "amount": amount,
            "fees": total_fee,
            "wallet": wallet.name[:80],
            "wallet_id": wallet.id,
            "origin_wallet_id": origin_wallet_id or wallet.id,
            "memo": data.memo[:240],
            "invoice": data.bolt11,
            "payment_hash": data.payment_hash,
            "fee_wallet": settings.lnbits_service_fee_wallet or "",
            "internal": bool(internal),
            "expires": int(data.expiry.timestamp()) if data.expiry else None,
        }
        payload = await handler.prepare(context)
        # Providers may enrich display context and shorten expiry.
        # Financial bindings must remain unchanged.
        for key, value in context.items():
            if key != "expires" and payload.get(key) != value:
                raise PaymentError("Authorization provider changed payment binding.")
        if context["expires"] and payload["expires"] > context["expires"]:
            raise PaymentError("Authorization exceeds invoice expiry.")
        if payload["expires"] <= int(time.time()):
            raise PaymentError("Invoice expired.", status="failed")
        body = encode(payload)
        route_fee = fee_reserve(amount, bool(internal))
        data.fee = -total_fee
        data.extra = {
            **(data.extra or {}),
            "authorization": {
                "provider": provider,
                "id": context["id"],
                "stage": "queued",
                "body": body,
                "digest": digest(body),
                "expires": payload["expires"],
                "route_fee": route_fee,
                "service_fee": total_fee - route_fee,
                "fee_wallet": context["fee_wallet"],
            },
        }
        checking_id = "temp_authorization_" + context["id"]
        return await reserve_attempt(old, checking_id, data, conn)


async def execute(request_id: str):  # noqa: C901
    from lnbits.core.services.payments import (
        _pay_internal_invoice,
        _send_payment_notification_in_background,
    )
    from lnbits.decorators import check_user_extension_access

    result: Payment | None = None
    async with transaction() as conn:
        row = await request(request_id, conn)
        if not row or row["stage"] not in WAITING:
            return
        provider = row["provider"]
        if (
            not available(provider)
            or not await hooks.installed(provider, conn)
            or await selected(conn) != provider
        ):
            return
        handler = providers[provider]
        if not await handler.authorize(row):
            return
        # stop()/uninstall can run while the provider's local DB call yields.
        if not available(provider) or providers.get(provider) is not handler:
            return
        payment = await get_standalone_payment(row["checking_id"], conn=conn)
        if not payment or not payment.pending:
            await set_stage(row, "cancelled", conn)
            return
        wallet = await get_wallet(payment.wallet_id, conn=conn)
        payload = json.loads(row["body"])
        origin = await get_wallet(payload["origin_wallet_id"], conn=conn)
        allowed = (
            await check_user_extension_access(origin.user, payment.tag or "", conn)
            if origin
            else None
        )
        if (
            row["expires"] <= int(time.time())
            or not wallet
            or not wallet.can_send_payments
            or not origin
            or not origin.can_send_payments
            or origin.source_wallet_id != payment.wallet_id
            or wallet.balance_msat < 0
            or settings.lnbits_only_allow_incoming_payments
            or not allowed
            or not allowed.success
            or abs(payment.amount)
            > settings.lnbits_max_outgoing_payment_amount_sats * 1000
        ):
            await set_stage(row, "cancelled", conn)
            return
        if (
            payment.bolt11 != payload["invoice"]
            or abs(payment.amount) != payload["amount"]
            or abs(payment.fee) != payload["fees"]
        ):
            await set_stage(row, "cancelled", conn)
            return
        # Reservation already reduced the balance. Restore it only in this local
        # snapshot for the existing internal settlement balance check.
        wallet.balance_msat += abs(payment.amount) + abs(payment.fee)
        if payload["internal"]:
            data = CreatePayment(
                wallet_id=wallet.id,
                payment_hash=payment.payment_hash,
                bolt11=payment.bolt11,
                amount_msat=payment.amount,
                memo=payment.memo or "",
                extra=payment.extra,
                fee=payment.fee,
            )
            result = await _pay_internal_invoice(wallet, data, conn, reserved=payment)
            if not result:
                await set_stage(row, "cancelled", conn)
                return
            row = {**row, "checking_id": result.checking_id}
            await set_stage(row, "settled", conn)
            await credit_fee(row, result, conn)
        else:
            await set_stage(row, "dispatched", conn)
        # Persist before network I/O. A crash here is deliberately ambiguous;
        # recovery queries payment status and NEVER retries node dispatch.
    if result is not None:
        await _send_payment_notification_in_background(wallet.id, result)
        incoming = await get_standalone_payment(payment.payment_hash, incoming=True)
        if incoming:
            task_manager.internal_invoice_queue.put_nowait(incoming)
    else:
        await finish_external(row, payment, wallet)


async def finish_external(row: dict, payment: Payment, wallet: Wallet):
    from lnbits.core.services.payments import (
        _fundingsource_pay_invoice,
    )

    request_id = row["id"]
    try:
        response = await _fundingsource_pay_invoice(
            payment.checking_id, payment.bolt11, row["route_fee"]
        )
    except Exception:
        return  # Unknown result: keep reservation and reconcile with node.
    async with transaction() as conn:
        current_payment = await get_standalone_payment(row["checking_id"], conn=conn)
        if (
            not current_payment
            or not current_payment.pending
            or current_payment.extra.get("authorization", {}).get("id") != request_id
        ):
            return
        payment = current_payment
        if response.failed:
            payment.status = PaymentState.FAILED
            payment.extra["authorization"]["stage"] = "failed"

        elif response.success:
            await finalize_success(payment, response, conn, response.checking_id)
            return
        payment = await update_payment(
            payment, new_checking_id=response.checking_id, conn=conn
        )


def existing_payment(old: Payment, wallet_id: str) -> Payment:
    if old.wallet_id == wallet_id and old.pending and old.extra.get("authorization"):
        return old
    raise PaymentError("Payment already exists.", status="pending")


async def check_hard_caps(wallet_id: str, amount: int, conn: Connection):
    from lnbits.core.services.payments import check_time_limit_between_transactions
    from lnbits.helpers import daystart_timestamp

    await check_time_limit_between_transactions(wallet_id, conn)
    limit = settings.lnbits_wallet_limit_daily_max_withdraw
    if limit:
        row: dict = await conn.fetchone(
            "SELECT COALESCE(SUM(-amount), 0) AS spent FROM apipayments "  # noqa: S608
            "WHERE wallet_id = :wallet AND amount < 0 "
            "AND status IN ('pending','success') AND time >= "
            + db.timestamp_placeholder("since"),
            {"wallet": wallet_id, "since": daystart_timestamp()},
        )
        if int(row["spent"]) + amount > limit * 1000:
            raise PaymentError("Daily withdrawal hard limit reached.", status="failed")


async def credit_fee(row: dict, payment: Payment, conn: Connection):
    destination = json.loads(row["body"]).get("fee_wallet")
    if not destination or not row["service_fee"]:
        return
    checking_id = f"service_fee_{payment.payment_hash}"
    if await get_standalone_payment(checking_id, conn=conn):
        return
    await create_payment(
        checking_id,
        CreatePayment(
            wallet_id=destination,
            bolt11=payment.bolt11,
            payment_hash=payment.payment_hash,
            amount_msat=row["service_fee"],
            memo="Service fee for authorized payment",
        ),
        status=PaymentState.SUCCESS,
        conn=conn,
    )


async def finalize_success(payment, status, conn=None, new_checking_id=None):
    return await hooks.finalize_success(payment, status, conn, new_checking_id)


async def previous_attempt(wallet_id: str, payment_hash: str, conn: Connection):
    old = await conn.fetchone(
        "SELECT * FROM apipayments WHERE payment_hash = :hash AND amount < 0",
        {"hash": payment_hash},
        Payment,
    )
    if not old:
        return None
    if old.pending:
        return existing_payment(old, wallet_id)
    previous = old.extra.get("authorization", {})
    if previous and previous.get("stage") not in (
        "rejected",
        "expired",
        "cancelled",
        "failed",
    ):
        raise PaymentError("Payment cannot be retried.", status="pending")
    return old


async def reserve_attempt(old, checking_id, data, conn):
    if not old:
        return await create_payment(checking_id, data, conn=conn)
    old.status = PaymentState.PENDING
    old.fee = data.fee
    old.extra = data.extra
    old.memo = data.memo
    old.time = datetime.now(timezone.utc)
    return await update_payment(old, conn=conn)
