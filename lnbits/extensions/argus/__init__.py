from fastapi import APIRouter

from lnbits.task_manager import task_manager

from . import payment_authorization as ledger
from .services import db as db
from .services import provider, stop, tick
from .views import page_router
from .views_api import argus_router

argus_ext = APIRouter()
argus_ext.include_router(argus_router)
argus_ext.include_router(page_router)
argus_static_files = [{"path": "/argus/static", "name": "argus_static"}]


def argus_start():
    task_manager.create_permanent_task(tick, name="argus_tick", interval=1)
    ledger.register("argus", provider)


async def argus_stop():
    await stop()
