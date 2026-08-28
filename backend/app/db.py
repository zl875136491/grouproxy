from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from .config import Settings
from .models import DOCUMENT_MODELS


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: AsyncIOMotorClient | None = None

    async def connect(self) -> None:
        self.client = AsyncIOMotorClient(
            self.settings.mongodb_url,
            serverSelectionTimeoutMS=3000,
            tz_aware=True,
        )
        await self.client.admin.command("ping")
        await init_beanie(
            database=self.client[self.settings.mongodb_database],
            document_models=DOCUMENT_MODELS,
        )

    async def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None


@asynccontextmanager
async def database_lifespan(settings: Settings) -> AsyncIterator[Database]:
    database = Database(settings)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()
