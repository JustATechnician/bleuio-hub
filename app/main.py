from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import config
from app.api import hub, idle_reaper, mount

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bleuio-hub")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.MACROS_DIR.mkdir(parents=True, exist_ok=True)
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    await hub.manager.start()
    reaper = asyncio_create_reaper()
    yield
    reaper.cancel()
    await hub.manager.close()


def asyncio_create_reaper():
    import asyncio

    return asyncio.create_task(idle_reaper())


app = FastAPI(title="BleuIO Hub", version="1.0.0", lifespan=lifespan)
mount(app)
app.mount("/", StaticFiles(directory=str(config.STATIC_DIR), html=True), name="static")


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=config.HOST,
        port=config.PORT,
        reload=False,
    )


if __name__ == "__main__":
    main()
