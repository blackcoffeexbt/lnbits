import asyncio
import json
import time
from unittest.mock import patch

import pytest

from lnbits.core.crud import create_wallet
from lnbits.core.crud import get_standalone_payment as _get_payment
from lnbits.core.crud import get_wallet as _get_wallet
from lnbits.core.db import db
from lnbits.core.models import CreateInvoice, CreatePayment
from lnbits.core.services import create_user_account
from lnbits.core.services.payments import (
    check_payment_status,
    create_wallet_invoice,
    pay_invoice,
    update_wallet_balance,
)
from lnbits.db import TransactionalConnection
from lnbits.exceptions import PaymentError
from lnbits.extensions.argus import payment_authorization as ledger
from lnbits.extensions.argus import services as argus
from lnbits.wallets.base import PaymentResponse


async def get_wallet(wallet_id):
    wallet = await _get_wallet(wallet_id)
    assert wallet is not None
    return wallet


async def get_standalone_payment(checking_id, **kwargs):
    payment = await _get_payment(checking_id, **kwargs)
    assert payment is not None
    return payment


@pytest.fixture
async def enabled_argus(argus_extension):
    original = await argus.config()
    async with argus.transaction() as conn:
        cfg = {
            **original,
            "enabled": True,
            # Legacy pairing metadata must not restrict Mainnet invoices.
            "network": "Testnet4",
            "key": "ab" * 32,
            "device": "cd" * 16,
            "timeout": 300,
        }
        await argus.save_config(cfg, conn)
    await ledger.configure("argus")
    yield cfg
    await ledger.configure("")
    async with argus.transaction() as conn:
        await argus.save_config(original, conn)


@pytest.fixture
async def argus_wallets(enabled_argus):
    user = await create_user_account()
    sender = await create_wallet(user_id=user.id, wallet_name="Argus sender")
    receiver = await create_wallet(user_id=user.id, wallet_name="Argus recipient")
    await update_wallet_balance(sender, 100000)
    return sender, receiver


async def approve(payment):
    request_id = payment.extra["authorization"]["id"]
    await argus.project()
    row = await argus.db.fetchone(
        "SELECT * FROM argus.argus_requests WHERE id = :id", {"id": request_id}
    )
    await argus.decide(
        {
            "id": request_id,
            "digest": row["digest"],
            "decision": "approved",
            "revision": 0,
        }
    )
    return request_id


def test_argus_pairing_code_and_direction_binding():
    key = "ab" * 32
    code = argus.pairing_code(key, "cd" * 16)
    assert len(code) == 6 and code.isdigit()
    assert argus.mac(key, "server:session:0:body") != argus.mac(
        key, "device:session:0:body"
    )
    assert argus.digest('{"amount":1}') != argus.digest('{"amount":2}')
    with pytest.raises(ValueError):
        argus.Policy(automatic=True)


@pytest.mark.anyio
async def test_argus_internal_reserves_without_credit_then_settles_once(argus_wallets):
    from_wallet, to_wallet = argus_wallets
    invoice = await create_wallet_invoice(
        to_wallet.id, CreateInvoice(amount=123, memo="Argus test")
    )
    before_sender = await get_wallet(from_wallet.id)
    before_receiver = await get_wallet(to_wallet.id)
    payment = await asyncio.wait_for(
        pay_invoice(wallet_id=from_wallet.id, payment_request=invoice.bolt11), 2
    )
    assert payment.pending and payment.extra["authorization"]["stage"] == "queued"
    sender = await get_wallet(from_wallet.id)
    receiver = await get_wallet(to_wallet.id)
    assert sender.balance_msat == before_sender.balance_msat - 123000 - abs(payment.fee)
    assert receiver.balance_msat == before_receiver.balance_msat
    assert (await check_payment_status(payment)).pending
    duplicate = await pay_invoice(
        wallet_id=from_wallet.id, payment_request=invoice.bolt11
    )
    assert duplicate.checking_id == payment.checking_id
    request_id = await approve(payment)
    await asyncio.gather(argus.execute(request_id), argus.execute(request_id))
    paid = await get_standalone_payment(payment.payment_hash)
    assert paid.success
    assert (
        await get_wallet(to_wallet.id)
    ).balance_msat == before_receiver.balance_msat + 123000
    assert (await get_wallet(from_wallet.id)).balance_msat == sender.balance_msat


@pytest.mark.anyio
async def test_argus_rejection_expiry_and_bad_digest(argus_wallets):
    from_wallet, to_wallet = argus_wallets
    for terminal in ("rejected", "expired", "cancelled"):
        invoice = await create_wallet_invoice(to_wallet.id, CreateInvoice(amount=124))
        before = (await get_wallet(from_wallet.id)).balance_msat
        payment = await pay_invoice(
            wallet_id=from_wallet.id, payment_request=invoice.bolt11
        )
        request_id = payment.extra["authorization"]["id"]
        await argus.project()
        row = await argus.db.fetchone(
            "SELECT * FROM argus.argus_requests WHERE id = :id", {"id": request_id}
        )
        with pytest.raises(ValueError):
            await argus.decide(
                {"id": request_id, "digest": "00" * 32, "decision": "approved"}
            )
        if terminal == "rejected":
            await argus.decide(
                {"id": request_id, "digest": row["digest"], "decision": terminal}
            )
        elif terminal == "expired":
            async with ledger.transaction() as conn:
                fresh = await _get_payment(payment.payment_hash, conn=conn)
                assert fresh is not None
                fresh.extra["authorization"]["expires"] = int(time.time()) - 1
                from lnbits.core.crud import update_payment

                await update_payment(fresh, conn=conn)
            await ledger.expire()
        else:
            await argus.cancel(request_id)
        assert (await get_standalone_payment(payment.payment_hash)).failed
        assert (await get_wallet(from_wallet.id)).balance_msat == before
        assert (
            await get_standalone_payment(invoice.checking_id, incoming=True)
        ).pending


@pytest.mark.anyio
async def test_argus_external_does_not_dispatch_until_approved(
    argus_wallets, external_funding_source, mocker
):
    from_wallet, _ = argus_wallets
    invoice = await external_funding_source.create_invoice(125, "Argus external")
    send = mocker.patch(
        "lnbits.core.services.payments._fundingsource_pay_invoice",
        new_callable=mocker.AsyncMock,
    )
    send.return_value = PaymentResponse(ok=None)
    payment = await pay_invoice(
        wallet_id=from_wallet.id, payment_request=invoice.payment_request
    )
    send.assert_not_awaited()
    request_id = await approve(payment)
    await argus.execute(request_id)
    await argus.execute(request_id)
    send.assert_awaited_once()
    # Unknown backend result is retained, never retried after a worker restart.
    assert (await get_standalone_payment(payment.payment_hash)).pending
    await argus.tick()
    send.assert_awaited_once()
    row = await argus.db.fetchone(
        "SELECT * FROM argus.argus_requests WHERE id = :id", {"id": request_id}
    )
    assert row["stage"] == "dispatched"
    assert json.loads(row["body"])["fees"] == abs(payment.fee)


@pytest.mark.anyio
async def test_argus_core_envelope_and_reservation_roll_back_together(
    argus_wallets, mocker
):
    sender, recipient = argus_wallets
    invoice = await create_wallet_invoice(recipient.id, CreateInvoice(amount=126))
    before = (await get_wallet(sender.id)).balance_msat
    reserve = ledger.reserve_attempt

    async def fail_reservation(*args, **kwargs):
        await reserve(*args, **kwargs)
        raise RuntimeError("Simulated storage failure")

    mocker.patch.object(ledger, "reserve_attempt", fail_reservation)
    with pytest.raises(RuntimeError, match="storage failure"):
        await pay_invoice(wallet_id=sender.id, payment_request=invoice.bolt11)
    assert (await get_wallet(sender.id)).balance_msat == before
    row = await db.fetchone(
        "SELECT checking_id FROM apipayments WHERE payment_hash = :hash AND amount < 0",
        {"hash": invoice.payment_hash},
    )
    assert row is None


@pytest.mark.anyio
async def test_argus_concurrent_reservations_cannot_overspend(argus_wallets):
    sender, recipient = argus_wallets
    invoices = [
        await create_wallet_invoice(recipient.id, CreateInvoice(amount=60000))
        for _ in range(2)
    ]
    results = await asyncio.gather(
        *(pay_invoice(wallet_id=sender.id, payment_request=i.bolt11) for i in invoices),
        return_exceptions=True,
    )
    assert sum(isinstance(r, Exception) for r in results) == 1
    assert (await get_wallet(sender.id)).balance_msat >= 0
    assert (await get_wallet(recipient.id)).balance_msat == 0


@pytest.mark.anyio
async def test_argus_freezes_service_fee_until_settlement(
    argus_wallets, external_funding_source, mocker, settings
):
    sender, recipient = argus_wallets
    settings.lnbits_service_fee_wallet = recipient.id
    settings.lnbits_service_fee = 3
    invoice = await external_funding_source.create_invoice(1000, "Frozen fee")
    send = mocker.patch(
        "lnbits.core.services.payments._fundingsource_pay_invoice",
        new_callable=mocker.AsyncMock,
    )
    send.return_value = PaymentResponse(ok=True, fee_msat=100)
    payment = await pay_invoice(
        wallet_id=sender.id, payment_request=invoice.payment_request
    )
    request_id = await approve(payment)
    settings.lnbits_service_fee = 10
    await argus.execute(request_id)
    assert (await get_wallet(recipient.id)).balance_msat == 30000
    settled = await get_standalone_payment(payment.payment_hash)
    assert settled.success and settled.fee == -30100
    assert settled.extra["authorization"]["stage"] == "settled"


@pytest.mark.anyio
async def test_argus_queue_rotation_keeps_manual_request_active(argus_wallets):
    from lnbits.extensions.argus.views_api import sync_body

    sender, recipient = argus_wallets
    for _ in range(12):
        invoice = await create_wallet_invoice(
            recipient.id, CreateInvoice(amount=1, memo="long review " * 30)
        )
        await pay_invoice(wallet_id=sender.id, payment_request=invoice.bolt11)
    cfg = await argus.config()
    first, offset = await sync_body(cfg, 0)
    second, _ = await sync_body(cfg, offset)
    batch1, batch2 = json.loads(first), json.loads(second)
    assert len(batch1["active"]) == 12
    assert len(batch1["requests"]) < 12
    assert batch1["requests"][0]["id"] != batch2["requests"][0]["id"]
    assert batch1["requests"][0]["id"] in batch2["active"]
    assert len(argus.encode({"body": first, "mac": "0" * 64})) <= 19000


@pytest.mark.anyio
async def test_argus_rejected_invoice_can_get_fresh_approval(argus_wallets):
    sender, recipient = argus_wallets
    invoice = await create_wallet_invoice(recipient.id, CreateInvoice(amount=100))
    first = await pay_invoice(wallet_id=sender.id, payment_request=invoice.bolt11)
    old_id = first.extra["authorization"]["id"]
    await argus.project()
    old = await argus.db.fetchone(
        "SELECT * FROM argus.argus_requests WHERE id = :id", {"id": old_id}
    )
    await argus.decide({"id": old_id, "digest": old["digest"], "decision": "rejected"})
    second = await pay_invoice(wallet_id=sender.id, payment_request=invoice.bolt11)
    assert second.extra["authorization"]["id"] != old_id
    await argus.decide({"id": old_id, "digest": old["digest"], "decision": "approved"})
    assert (await get_wallet(recipient.id)).balance_msat == 0
    await argus.execute(await approve(second))
    assert (await get_wallet(recipient.id)).balance_msat == 100000


@pytest.mark.anyio
async def test_argus_pending_refresh_reuses_database_connection(argus_wallets):
    from lnbits.core.services.payments import (
        check_transaction_status,
        update_pending_payment,
    )

    sender, receiver = argus_wallets
    invoice = await create_wallet_invoice(receiver.id, CreateInvoice(amount=123))
    payment = await pay_invoice(wallet_id=sender.id, payment_request=invoice.bolt11)
    async with db.connect() as conn:
        refreshed = await asyncio.wait_for(update_pending_payment(payment, conn), 2)
        status = await asyncio.wait_for(
            check_transaction_status(sender.id, payment.payment_hash, conn), 2
        )
    assert refreshed.pending and status.pending
    assert await asyncio.wait_for(get_wallet(receiver.id), 2)


@pytest.mark.anyio
async def test_argus_payment_list_refresh_leaves_database_available(
    argus_wallets, monkeypatch
):
    from lnbits.core.models import KeyType, PaymentFilters
    from lnbits.core.models.wallets import BaseWalletTypeInfo
    from lnbits.core.views import payment_api
    from lnbits.db import Filters

    sender, receiver = argus_wallets
    invoice = await create_wallet_invoice(receiver.id, CreateInvoice(amount=123))
    payment = await pay_invoice(wallet_id=sender.id, payment_request=invoice.bolt11)
    started, release = asyncio.Event(), asyncio.Event()
    original = payment_api.update_pending_payment

    async def slow_status(p, **kwargs):
        started.set()
        await release.wait()
        return await original(p, **kwargs)

    monkeypatch.setattr(payment_api, "update_pending_payment", slow_status)
    refresh = asyncio.create_task(
        payment_api.api_payments_paginated(
            BaseWalletTypeInfo(key_type=KeyType.invoice, wallet=sender),
            recheck_pending=True,
            filters=Filters(model=PaymentFilters),
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        # A slow status check must not block an unrelated wallet read.
        assert await asyncio.wait_for(get_wallet(receiver.id), 2)
    finally:
        release.set()
        page = await asyncio.wait_for(refresh, 2)
    assert next(p for p in page.data if p.checking_id == payment.checking_id).pending


@pytest.mark.anyio
async def test_argus_offer_fails_before_reserving_balance(argus_wallets):
    sender, _ = argus_wallets
    before = await get_wallet(sender.id)
    data = CreatePayment(
        wallet_id=sender.id,
        bolt11="lno1unsupported",
        memo="Unsupported offer",
        payment_hash="ab" * 32,
        amount_msat=-1000,
    )
    with pytest.raises(PaymentError, match="BOLT12 offers are not supported"):
        await ledger.enqueue(sender, data, None)
    after = await get_wallet(sender.id)
    assert after.balance_msat == before.balance_msat
    assert await _get_payment(data.payment_hash) is None


@pytest.mark.anyio
async def test_argus_rebuilds_missing_extension_insert_after_core_commit(
    argus_wallets, mocker
):
    sender, receiver = argus_wallets
    invoice = await create_wallet_invoice(receiver.id, CreateInvoice(amount=321))
    payment = await pay_invoice(wallet_id=sender.id, payment_request=invoice.bolt11)
    envelope = payment.extra["authorization"]
    before = (await get_wallet(sender.id)).balance_msat
    execute = TransactionalConnection.execute

    async def fail_insert(self, query, values=None):
        if "INSERT INTO argus.argus_requests" in query:
            raise RuntimeError("Projection write lost")
        return await execute(self, query, values)

    with patch.object(TransactionalConnection, "execute", fail_insert):
        with pytest.raises(RuntimeError, match="Projection write lost"):
            await argus.project()
    assert (await get_wallet(sender.id)).balance_msat == before
    assert (await get_standalone_payment(payment.payment_hash)).pending
    await argus.project()
    await argus.project()
    rows = await argus.db.fetchall(
        "SELECT * FROM argus.argus_requests WHERE id = :id", {"id": envelope["id"]}
    )
    assert len(rows) == 1
    assert (
        rows[0]["body"] == envelope["body"] and rows[0]["digest"] == envelope["digest"]
    )
    assert (await get_wallet(receiver.id)).balance_msat == 0
    await argus.execute(await approve(payment))
    assert (await get_wallet(receiver.id)).balance_msat == 321000


@pytest.mark.anyio
async def test_argus_durable_approval_and_stale_audit_recover_once(
    argus_wallets, mocker
):
    sender, receiver = argus_wallets
    invoice = await create_wallet_invoice(receiver.id, CreateInvoice(amount=322))
    payment = await pay_invoice(wallet_id=sender.id, payment_request=invoice.bolt11)
    request_id = await approve(payment)
    ledger.unregister("argus")
    ledger.register(
        "argus", argus.provider
    )  # Process-local state recreated after restart.
    with patch.object(argus, "project", side_effect=RuntimeError("Audit update lost")):
        with pytest.raises(RuntimeError, match="Audit update lost"):
            await argus.execute(request_id)
    assert (await get_standalone_payment(payment.payment_hash)).success
    stale = await argus.db.fetchone(
        "SELECT stage FROM argus.argus_requests WHERE id = :id", {"id": request_id}
    )
    assert stale["stage"] == "approved"
    await argus.execute(request_id)
    await argus.project()
    current = await argus.db.fetchone(
        "SELECT stage FROM argus.argus_requests WHERE id = :id", {"id": request_id}
    )
    assert current["stage"] == "settled"
    assert (await get_wallet(receiver.id)).balance_msat == 322000


@pytest.mark.anyio
async def test_argus_crash_after_dispatch_claim_never_resends(
    argus_wallets, external_funding_source, mocker
):
    sender, _ = argus_wallets
    invoice = await external_funding_source.create_invoice(323, "Claim crash")
    payment = await pay_invoice(
        wallet_id=sender.id, payment_request=invoice.payment_request
    )
    request_id = await approve(payment)
    send = mocker.patch(
        "lnbits.core.services.payments._fundingsource_pay_invoice",
        new_callable=mocker.AsyncMock,
    )
    with patch.object(
        ledger, "finish_external", side_effect=RuntimeError("Crash after claim")
    ):
        with pytest.raises(RuntimeError, match="Crash after claim"):
            await argus.execute(request_id)
    claimed = await get_standalone_payment(payment.payment_hash)
    assert claimed.extra["authorization"]["stage"] == "dispatched"
    ledger.unregister("argus")
    ledger.register("argus", argus.provider)
    await argus.db.execute(
        "DELETE FROM argus.argus_requests WHERE id = :id", {"id": request_id}
    )
    await argus.project()
    await argus.execute(request_id)
    await argus.tick()
    send.assert_not_awaited()
    backend_status = mocker.patch("lnbits.core.services.payments.get_funding_source")
    assert (await check_payment_status(claimed)).pending
    backend_status.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["disable", "uninstall"])
async def test_argus_extension_removal_keeps_enforcement(
    argus_wallets, mocker, settings, operation
):
    from types import SimpleNamespace

    from lnbits.core.services.extensions import (
        deactivate_extension,
        uninstall_extension,
    )

    sender, receiver = argus_wallets
    invoice = await create_wallet_invoice(receiver.id, CreateInvoice(amount=324))
    payment = await pay_invoice(wallet_id=sender.id, payment_request=invoice.bolt11)
    request_id = await approve(payment)
    mocker.patch.object(settings, "lnbits_deactivated_extensions", set())
    cleaned = []

    def clean_files():
        assert not ledger.available("argus")
        cleaned.append(True)

    mocker.patch(
        "lnbits.core.services.extensions.get_installed_extension",
        return_value=SimpleNamespace(is_wasm=False, clean_extension_files=clean_files),
    )
    if operation == "disable":
        await deactivate_extension("argus")
    else:
        await uninstall_extension("argus")
        assert cleaned
    assert await ledger.selected() == "argus"
    await ledger.execute(request_id)
    assert (await get_standalone_payment(payment.payment_hash)).pending
    assert (await get_wallet(receiver.id)).balance_msat == 0
    next_invoice = await create_wallet_invoice(receiver.id, CreateInvoice(amount=1))
    with pytest.raises(PaymentError, match="unavailable"):
        await pay_invoice(wallet_id=sender.id, payment_request=next_invoice.bolt11)
    # The extension-independent admin recovery control explicitly disables enforcement.
    await ledger.configure("")
    assert (await get_standalone_payment(payment.payment_hash)).failed
    normal = await pay_invoice(wallet_id=sender.id, payment_request=next_invoice.bolt11)
    assert normal.success


@pytest.mark.anyio
async def test_argus_missing_extension_database_cannot_restore_approval(
    argus_wallets, tmp_path, mocker, settings
):
    from lnbits.db import Database
    from lnbits.extensions.argus.migrations import m001_initial

    sender, receiver = argus_wallets
    invoice = await create_wallet_invoice(receiver.id, CreateInvoice(amount=325))
    payment = await pay_invoice(wallet_id=sender.id, payment_request=invoice.bolt11)
    request_id = await approve(payment)
    mocker.patch.object(settings, "lnbits_data_folder", str(tmp_path))
    empty_db = Database("ext_argus")
    with patch.object(argus, "db", empty_db):
        async with empty_db.connect() as conn:
            await m001_initial(conn)
        await argus.project()
        reconstructed = await empty_db.fetchone(
            "SELECT stage FROM argus.argus_requests WHERE id = :id", {"id": request_id}
        )
        assert reconstructed["stage"] == "queued"
        await argus.execute(request_id)
        assert (await get_standalone_payment(payment.payment_hash)).pending
        assert (await get_wallet(receiver.id)).balance_msat == 0
    await empty_db.engine.dispose()


@pytest.mark.anyio
async def test_argus_stop_during_approval_lookup_prevents_dispatch(argus_wallets):
    sender, receiver = argus_wallets
    invoice = await create_wallet_invoice(receiver.id, CreateInvoice(amount=326))
    payment = await pay_invoice(wallet_id=sender.id, payment_request=invoice.bolt11)
    request_id = await approve(payment)
    entered, release = asyncio.Event(), asyncio.Event()
    original = argus.provider.authorize

    async def slow_lookup(row):
        entered.set()
        await release.wait()
        return await original(row)

    with patch.object(argus.provider, "authorize", side_effect=slow_lookup):
        worker = asyncio.create_task(ledger.execute(request_id))
        await asyncio.wait_for(entered.wait(), 2)
        await argus.stop()
        release.set()
        await asyncio.wait_for(worker, 2)
    assert (await get_standalone_payment(payment.payment_hash)).pending
    assert (await get_wallet(receiver.id)).balance_msat == 0


@pytest.mark.anyio
async def test_argus_cannot_accept_client_authorization_metadata(argus_wallets):
    sender, receiver = argus_wallets
    invoice = await create_wallet_invoice(receiver.id, CreateInvoice(amount=327))
    forged = {"provider": "argus", "id": "ff" * 16, "stage": "approved"}
    payment = await pay_invoice(
        wallet_id=sender.id,
        payment_request=invoice.bolt11,
        extra={"authorization": forged, "argus": forged},
    )
    assert payment.extra["authorization"]["id"] != forged["id"]
    assert payment.extra["authorization"]["stage"] == "queued"
    await argus.execute(payment.extra["authorization"]["id"])
    assert (await get_wallet(receiver.id)).balance_msat == 0


@pytest.mark.anyio
async def test_argus_database_is_separate_from_core(argus_extension):
    assert argus.db.path != db.path
    tables = await argus.db.fetchall(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )
    assert {r["name"] for r in tables} == {"argus_config", "argus_requests"}
    core_tables = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )
    assert not {"argus_config", "argus_requests", "payment_authorization"} & {
        r["name"] for r in core_tables
    }


@pytest.mark.anyio
async def test_argus_standard_lifecycle_starts_and_awaits_worker_shutdown(
    argus_wallets,
):
    from lnbits.extensions.argus import argus_start, argus_stop
    from lnbits.task_manager import task_manager

    argus_start()
    job = task_manager.get_task("argus_tick")
    assert job is not None
    await asyncio.sleep(0)
    await argus_stop()
    assert job.task.done()
    assert not task_manager.get_task("argus_tick")
    assert not ledger.available("argus")
    assert await ledger.selected() == "argus"


@pytest.mark.anyio
async def test_argus_stale_worker_registration_cannot_bypass_durable_disable(
    argus_wallets,
):
    from lnbits.core.crud.extensions import update_installed_extension_state

    sender, receiver = argus_wallets
    invoice = await create_wallet_invoice(receiver.id, CreateInvoice(amount=328))
    payment = await pay_invoice(wallet_id=sender.id, payment_request=invoice.bolt11)
    request_id = await approve(payment)
    # Simulate another process disabling the extension while this worker retains it.
    await update_installed_extension_state(ext_id="argus", active=False)
    assert ledger.available("argus")
    await ledger.execute(request_id)
    assert (await get_wallet(receiver.id)).balance_msat == 0
    assert (await get_standalone_payment(payment.payment_hash)).pending
    another = await create_wallet_invoice(receiver.id, CreateInvoice(amount=1))
    with pytest.raises(PaymentError, match="unavailable"):
        await pay_invoice(wallet_id=sender.id, payment_request=another.bolt11)


@pytest.mark.anyio
async def test_argus_backend_settlement_survives_extension_uninstall(
    argus_wallets, external_funding_source, mocker, settings
):
    from lnbits.core.services.payments import update_payment_success_status
    from lnbits.wallets.base import PaymentSuccessStatus

    sender, fees = argus_wallets
    settings.lnbits_service_fee_wallet = fees.id
    settings.lnbits_service_fee = 3
    invoice = await external_funding_source.create_invoice(
        1000, "Settlement after removal"
    )
    send = mocker.patch(
        "lnbits.core.services.payments._fundingsource_pay_invoice",
        new_callable=mocker.AsyncMock,
    )
    send.return_value = PaymentResponse(ok=None, checking_id="known-backend-reference")
    payment = await pay_invoice(
        wallet_id=sender.id, payment_request=invoice.payment_request
    )
    await ledger.execute(await approve(payment))
    dispatched = await get_standalone_payment(payment.payment_hash)
    assert dispatched.extra["authorization"]["stage"] == "dispatched"
    await argus.stop()
    settings.lnbits_service_fee = 10
    status = PaymentSuccessStatus(fee_msat=100, preimage="ab" * 32)
    settled = await update_payment_success_status(dispatched, status)
    assert settled.success and settled.fee == -30100
    assert (await get_wallet(fees.id)).balance_msat == 30000
    await update_payment_success_status(settled, status)
    assert (await get_wallet(fees.id)).balance_msat == 30000


@pytest.mark.anyio
async def test_argus_extension_migration_is_repeatable_without_compatibility_import(
    argus_extension,
):
    from lnbits.extensions.argus.migrations import m001_initial

    before = await argus.config()
    async with argus.db.connect() as conn:
        await m001_initial(conn)
    assert await argus.config() == before


@pytest.mark.anyio
async def test_argus_empty_provider_result_cannot_fall_through_to_payment(
    argus_wallets, mocker
):
    sender, receiver = argus_wallets
    invoice = await create_wallet_invoice(receiver.id, CreateInvoice(amount=329))
    before = (await get_wallet(sender.id)).balance_msat
    mocker.patch.object(
        argus.provider, "enqueue", new_callable=mocker.AsyncMock, return_value=None
    )
    with pytest.raises(PaymentError, match="did not reserve"):
        await pay_invoice(wallet_id=sender.id, payment_request=invoice.bolt11)
    assert (await get_wallet(sender.id)).balance_msat == before
    assert (await get_wallet(receiver.id)).balance_msat == 0
