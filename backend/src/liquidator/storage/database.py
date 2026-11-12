"""Async database connection and session management."""

from __future__ import annotations

import os
from typing import AsyncGenerator

from liquidator.storage.models import Base
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class Database:
    def __init__(self, database_url: str):
        # Ensure directory exists for sqlite
        if database_url.startswith("sqlite+aiosqlite:///"):
            db_path = database_url.replace("sqlite+aiosqlite:///", "")
            dir_name = os.path.dirname(db_path)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)

        self.engine: AsyncEngine = create_async_engine(
            database_url,
            echo=False,
            future=True,
        )
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def init_models(self) -> None:
        """Create database tables if they do not exist."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """Dependency for FastAPI route handlers."""
        async with self.session_factory() as session:
            yield session

    async def close(self) -> None:
        """Close the database engine."""
        await self.engine.dispose()
