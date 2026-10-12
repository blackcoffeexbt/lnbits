"""Superuser recovery control, independent of any extension being installed."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from lnbits.core.services import payment_hooks
from lnbits.decorators import check_super_user

authorization_router = APIRouter(
    prefix="/admin/api/v1/payment-authorization",
    tags=["Admin"],
    dependencies=[Depends(check_super_user)],
)


class Selection(BaseModel):
    provider: str = ""


@authorization_router.get("")
async def get_selection():
    provider = await payment_hooks.selected()
    return {
        "provider": provider,
        "available": payment_hooks.available(provider)
        and await payment_hooks.installed(provider),
    }


@authorization_router.put("")
async def set_selection(value: Selection):
    await payment_hooks.configure(value.provider)
    return await get_selection()
