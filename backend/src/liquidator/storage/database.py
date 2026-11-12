"""Async database connection and session management."""

from __future__ import annotations

import os
from typing import AsyncGenerator

from liquidator.storage.models import Base
from sqlalchemy import text
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
        """Create database tables if they do not exist and apply schema migrations."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

            # Auto-migrate SQLite columns if table was created with older schema
            def _migrate_sqlite_columns(connection):
                try:
                    res = connection.execute(text("PRAGMA table_info(borrower_positions)")).fetchall()
                    cols = [r[1] for r in res]
                    needed = [
                        ("liquidation_price_usd", "NUMERIC(18, 6)"),
                        ("tier", "VARCHAR(16) DEFAULT 'COLD'"),
                        ("collateral_symbol", "VARCHAR(32)"),
                        ("debt_symbol", "VARCHAR(32)"),
                        ("distance_to_liq_pct", "NUMERIC(10, 4)"),
                        ("metadata_json", "TEXT"),
                    ]
                    for name, col_type in needed:
                        if name not in cols:
                            connection.execute(text(f"ALTER TABLE borrower_positions ADD COLUMN {name} {col_type}"))
                except Exception:
                    pass

            await conn.run_sync(_migrate_sqlite_columns)

    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """Dependency for FastAPI route handlers."""
        async with self.session_factory() as session:
            yield session

    async def close(self) -> None:
        """Close the database engine."""
        await self.engine.dispose()
