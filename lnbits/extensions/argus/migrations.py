"""Argus schema, entirely owned by the extension database."""

import json
import secrets


async def m001_initial(conn):
    cfg = {
        "enabled": False,
        "timeout": 300,
        "network": "Mainnet",
        "instance": secrets.token_hex(16),
    }
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS argus.argus_config "
        "(id INTEGER PRIMARY KEY, data TEXT NOT NULL)"
    )
    await conn.execute(
        "INSERT INTO argus.argus_config (id,data) VALUES (1,:data) "
        "ON CONFLICT (id) DO NOTHING",
        {"data": json.dumps(cfg)},
    )
    await conn.execute("""CREATE TABLE IF NOT EXISTS argus.argus_requests (
        id TEXT PRIMARY KEY, checking_id TEXT NOT NULL,
        body TEXT NOT NULL, digest TEXT NOT NULL,
        stage TEXT NOT NULL, expires BIGINT NOT NULL,
        route_fee BIGINT NOT NULL, service_fee BIGINT NOT NULL)""")
    from lnbits.db import SQLITE

    index = (
        "CREATE INDEX IF NOT EXISTS argus.argus_queue ON argus_requests (stage,expires)"
        if conn.type == SQLITE
        else "CREATE INDEX IF NOT EXISTS argus_queue "
        "ON argus.argus_requests (stage,expires)"
    )
    await conn.execute(index)
