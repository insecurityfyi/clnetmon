from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__, db, scheduler
from .api.routes import router
from .config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.pool = await db.create_pool(settings.database_url)
    await db.migrate(app.state.pool)
    tasks = scheduler.start(app.state.pool, settings)
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await app.state.pool.close()


app = FastAPI(title="Collaborative NMS", version=__version__, lifespan=lifespan)
app.include_router(router)


@app.get("/health")
async def health():
    await app.state.pool.fetchval("SELECT 1")
    return {"status": "ok", "version": __version__}
