from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from lnbits.core.crud import get_user_from_account
from lnbits.core.models import Account
from lnbits.decorators import check_super_user
from lnbits.helpers import template_renderer

from .services import active

page_router = APIRouter()


@page_router.get("/argus/", include_in_schema=False)
async def index(request: Request, account: Account = Depends(check_super_user)):
    if not active():
        raise HTTPException(503, "Argus extension is unavailable")
    user = await get_user_from_account(account)
    if not user:
        raise HTTPException(404, "User not found")
    return template_renderer(
        [str(Path(__file__).parent / "templates")]
    ).TemplateResponse(request, "argus.html", {"user": user.json()})
