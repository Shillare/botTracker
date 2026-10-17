"""Слой доступа к данным: соединение, миграции и низкоуровневые хелперы."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

Row = aiosqlite.Row
Params = Sequence[Any] | Mapping[str, Any]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admins (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    full_name  TEXT,
    source     TEXT    NOT NULL DEFAULT 'command',
    added_by   INTEGER,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS business_connections (
    id                            TEXT    PRIMARY KEY,
    user_id                       INTEGER NOT NULL,
    user_chat_id                  INTEGER NOT NULL,
    username                      TEXT,
    first_name                    TEXT,
    is_enabled                    INTEGER NOT NULL DEFAULT 1,
    can_reply                     INTEGER NOT NULL DEFAULT 0,
    can_read_messages             INTEGER NOT NULL DEFAULT 0,
    can_transfer_and_upgrade      INTEGER NOT NULL DEFAULT 0,
    can_transfer_stars            INTEGER NOT NULL DEFAULT 0,
    can_convert_gifts_to_stars    INTEGER NOT NULL DEFAULT 0,
    connected_at                  TEXT    NOT NULL,
    updated_at                    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_business_connections_user
    ON business_connections (user_id);

CREATE TABLE IF NOT EXISTS offers (
    id                      TEXT    PRIMARY KEY,
    command_key             TEXT,
    business_connection_id  TEXT,
    chat_id                 INTEGER NOT NULL,
    chat_type               TEXT    NOT NULL DEFAULT 'private',
    message_id              INTEGER,
    buyer_user_id           INTEGER NOT NULL DEFAULT 0,
    buyer_username          TEXT,
    seller_user_id          INTEGER NOT NULL,
    seller_username         TEXT,
    seller_name             TEXT,
    recipient_username      TEXT,
    gift_slug               TEXT    NOT NULL,
    gift_number             INTEGER,
    gift_title              TEXT    NOT NULL,
    gift_url                TEXT    NOT NULL,
    stars                   INTEGER NOT NULL,
    currency                TEXT    NOT NULL DEFAULT 'Stars',
    status                  TEXT    NOT NULL DEFAULT 'pending',
    created_at              TEXT    NOT NULL,
    expires_at              TEXT    NOT NULL,
    resolved_at             TEXT,
    resolved_by             INTEGER
);

CREATE INDEX IF NOT EXISTS ix_offers_status ON offers (status);
CREATE INDEX IF NOT EXISTS ix_offers_chat   ON offers (chat_id, status);
"""

# Колонки, добавленные после первой публикации схемы.
# Подключаются к существующей БД через ALTER TABLE в Database.migrate().
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("offers", "chat_type", "TEXT NOT NULL DEFAULT 'private'"),
    ("offers", "currency", "TEXT NOT NULL DEFAULT 'Stars'"),
    ("offers", "recipient_username", "TEXT"),
    ("offers", "language", "TEXT NOT NULL DEFAULT 'ru'"),
    ("offers", "command_key", "TEXT"),
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def utcnow_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


class Database:
    """Тонкая асинхронная обёртка над aiosqlite с одним соединением."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() не был вызван")
        return self._conn

    async def connect(self) -> None:
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self.conn.commit()
        await self.migrate()
        logger.info("SQLite подключена: %s", self._path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("SQLite соединение закрыто")

    async def migrate(self) -> None:
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()
        await self._apply_migrations()
        # Уникальный индекс идемпотентности: одна команда = одна карточка.
        # Создаём ПОСЛЕ миграций, т.к. у старых баз колонки ещё нет.
        # NULL-ключи (предложения, созданные в обход команды) SQLite считает
        # различными, поэтому индекс им не мешает.
        await self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_offers_command_key "
            "ON offers (command_key)"
        )
        await self.conn.commit()

    async def _apply_migrations(self) -> None:
        """Добавляет колонки, которых ещё нет в существующей базе.

        ``CREATE TABLE IF NOT EXISTS`` не трогает уже созданные таблицы, поэтому
        новые поля приходится доклеивать вручную. Идемпотентно: повторный
        запуск ничего не делает.
        """
        for table, column, ddl in MIGRATIONS:
            existing = await self.fetch_all(f"PRAGMA table_info({table})")
            if any(row["name"] == column for row in existing):
                continue
            await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            logger.info("Миграция: %s.%s добавлена", table, column)
        await self.conn.commit()

    async def execute(self, sql: str, params: Params = ()) -> int:
        """Выполняет DML и возвращает количество затронутых строк."""
        cursor = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cursor.rowcount or 0

    async def fetch_one(self, sql: str, params: Params = ()) -> Row | None:
        async with self.conn.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def fetch_all(self, sql: str, params: Params = ()) -> list[Row]:
        async with self.conn.execute(sql, params) as cursor:
            return list(await cursor.fetchall())

    # --- служебное ---------------------------------------------------------------
    async def get_meta(self, key: str) -> str | None:
        row = await self.fetch_one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
