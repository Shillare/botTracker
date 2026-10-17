"""Репозитории: доступ к сущностям поверх :class:`app.database.db.Database`."""

from __future__ import annotations

import logging
from typing import Any

from app.constants import STATUS_PENDING
from app.database.db import Database, Row, utcnow_iso

logger = logging.getLogger(__name__)


class AdminRepository:
    """Список администраторов бота."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def is_admin(self, user_id: int) -> bool:
        row = await self._db.fetch_one("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
        return row is not None

    async def get_all(self) -> list[Row]:
        return await self._db.fetch_all("SELECT * FROM admins ORDER BY created_at ASC")

    async def add(
        self,
        user_id: int,
        *,
        username: str | None = None,
        full_name: str | None = None,
        source: str = "command",
        added_by: int | None = None,
    ) -> bool:
        """Добавляет админа. ``True`` — админ появился, ``False`` — уже был."""
        affected = await self._db.execute(
            "INSERT OR IGNORE INTO admins (user_id, username, full_name, source, added_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, full_name, source, added_by, utcnow_iso()),
        )
        if affected:
            logger.info("Добавлен админ %s (%s) источник=%s", user_id, username, source)
        return bool(affected)

    async def remove(self, user_id: int) -> bool:
        affected = await self._db.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        if affected:
            logger.info("Права админа сняты с %s", user_id)
        return bool(affected)

    async def count(self) -> int:
        row = await self._db.fetch_one("SELECT COUNT(*) AS total FROM admins")
        return int(row["total"]) if row else 0

    async def seed_once(self, user_ids: set[int]) -> int:
        """Однократно добавляет стартовых админов из конфигурации.

        Повторно не выполняется, чтобы команда ``/remove_admin`` не откатывалась
        при перезапуске бота.
        """
        if await self._db.get_meta("admins_seeded") == "1":
            return 0
        added = 0
        for user_id in user_ids:
            if await self.add(user_id, source="env"):
                added += 1
        await self._db.set_meta("admins_seeded", "1")
        if user_ids:
            logger.info("Проинициализированы админы из конфигурации: %s", sorted(user_ids))
        return added


class BusinessConnectionRepository:
    """Бизнес-подключения бота."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert(self, payload: dict[str, Any]) -> Row:
        now = utcnow_iso()
        await self._db.execute(
            """
            INSERT INTO business_connections (
                id, user_id, user_chat_id, username, first_name, is_enabled,
                can_reply, can_read_messages, can_transfer_and_upgrade,
                can_transfer_stars, can_convert_gifts_to_stars,
                connected_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                user_id                    = excluded.user_id,
                user_chat_id               = excluded.user_chat_id,
                username                   = excluded.username,
                first_name                 = excluded.first_name,
                is_enabled                 = excluded.is_enabled,
                can_reply                  = excluded.can_reply,
                can_read_messages          = excluded.can_read_messages,
                can_transfer_and_upgrade   = excluded.can_transfer_and_upgrade,
                can_transfer_stars         = excluded.can_transfer_stars,
                can_convert_gifts_to_stars = excluded.can_convert_gifts_to_stars,
                updated_at                 = excluded.updated_at
            """,
            (
                payload["id"],
                payload["user_id"],
                payload["user_chat_id"],
                payload.get("username"),
                payload.get("first_name"),
                1 if payload["is_enabled"] else 0,
                1 if payload.get("can_reply") else 0,
                1 if payload.get("can_read_messages") else 0,
                1 if payload.get("can_transfer_and_upgrade") else 0,
                1 if payload.get("can_transfer_stars") else 0,
                1 if payload.get("can_convert_gifts_to_stars") else 0,
                payload.get("connected_at", now),
                now,
            ),
        )
        row = await self.get(payload["id"])
        assert row is not None
        return row

    async def get(self, connection_id: str) -> Row | None:
        return await self._db.fetch_one("SELECT * FROM business_connections WHERE id = ?", (connection_id,))

    async def get_enabled(self, connection_id: str) -> Row | None:
        return await self._db.fetch_one(
            "SELECT * FROM business_connections WHERE id = ? AND is_enabled = 1",
            (connection_id,),
        )

    async def get_any_enabled(self) -> Row | None:
        """Последнее активное подключение — фолбэк для апдейтов без business_connection_id."""
        return await self._db.fetch_one(
            "SELECT * FROM business_connections WHERE is_enabled = 1 ORDER BY updated_at DESC LIMIT 1",
        )

    async def list_all(self) -> list[Row]:
        return await self._db.fetch_all("SELECT * FROM business_connections ORDER BY connected_at DESC")

    async def set_enabled(self, connection_id: str, enabled: bool) -> int:
        return await self._db.execute(
            "UPDATE business_connections SET is_enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, utcnow_iso(), connection_id),
        )

    async def register_from_update(
        self,
        connection_id: str,
        *,
        user_id: int | None,
        user_chat_id: int | None = None,
        username: str | None = None,
        first_name: str | None = None,
    ) -> Row | None:
        """Восстанавливает потерянное подключение из входящего апдейта.

        Telegram присылает ``business_connection`` ровно один раз — в момент
        подключения. Если бот в этот момент не работал (перезапуск, поллинг
        офлайн, свежая база), событие теряется навсегда, и ``business_connection_id``
        больше не находится в базе. Прежний код в этом случае просто откатывался
        на отправку от имени бота: карточки уходили не от бизнес-аккаунта,
        команду нельзя было удалить, а владелец не понимал почему.

        Восстанавливаем запись из самого апдейта. Владелец подключения известен:
        ``business_message`` по определению отправлен подключённым аккаунтом,
        поэтому ``from_user`` — это он.

        Права записываются нулевыми и не влияют на работу сценария (они нужны
        только для показа в ``/business_info``); точные значения приедут при
        следующем событии ``business_connection``.
        """
        if not connection_id:
            return None
        return await self.upsert(
            {
                "id": connection_id,
                "user_id": user_id or 0,
                "user_chat_id": user_chat_id or user_id or 0,
                "username": username,
                "first_name": first_name,
                "is_enabled": True,
                "can_reply": True,
                "connected_at": utcnow_iso(),
            },
        )


class OfferRepository:
    """Предложения по подаркам."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, payload: dict[str, Any]) -> Row:
        await self._db.execute(
            """
            INSERT INTO offers (
                id, business_connection_id, chat_id, chat_type, message_id,
                buyer_user_id, buyer_username,
                seller_user_id, seller_username, seller_name,
                recipient_username,
                gift_slug, gift_number, gift_title, gift_url,
                stars, currency, language, status, created_at, expires_at
            )
            VALUES (
                :id, :business_connection_id, :chat_id, :chat_type, :message_id,
                :buyer_user_id, :buyer_username,
                :seller_user_id, :seller_username, :seller_name,
                :recipient_username,
                :gift_slug, :gift_number, :gift_title, :gift_url,
                :stars, :currency, :language, :status, :created_at, :expires_at
            )
            """,
            payload,
        )
        row = await self.get(payload["id"])
        assert row is not None
        return row

    async def create_if_unique(self, payload: dict[str, Any]) -> tuple[Row, bool]:
        """Создаёт предложение, пресекая повторную обработку одной команды.

        ``command_key`` (идентификатор исходного сообщения с командой)
        защищён уникальным индексом: повторная доставка апдейта Telegram или
        параллельный второй инстанс бота получают ``inserted=False`` и строку
        уже созданной сделки вместо второй карточки.

        Возвращает ``(row, inserted)``: ``inserted=False`` — команда уже
        обработана, карточку нужно пропустить.
        """
        affected = await self._db.execute(
            """
            INSERT INTO offers (
                id, command_key, business_connection_id, chat_id, chat_type,
                message_id, buyer_user_id, buyer_username,
                seller_user_id, seller_username, seller_name,
                recipient_username,
                gift_slug, gift_number, gift_title, gift_url,
                stars, currency, language, status, created_at, expires_at
            )
            VALUES (
                :id, :command_key, :business_connection_id, :chat_id, :chat_type,
                :message_id, :buyer_user_id, :buyer_username,
                :seller_user_id, :seller_username, :seller_name,
                :recipient_username,
                :gift_slug, :gift_number, :gift_title, :gift_url,
                :stars, :currency, :language, :status, :created_at, :expires_at
            )
            ON CONFLICT(command_key) DO NOTHING
            """,
            payload,
        )
        row = await self._db.fetch_one(
            "SELECT * FROM offers WHERE command_key = ?", (payload["command_key"],)
        )
        assert row is not None
        return row, affected > 0

    async def get(self, offer_id: str) -> Row | None:
        return await self._db.fetch_one("SELECT * FROM offers WHERE id = ?", (offer_id,))

    async def set_message_id(self, offer_id: str, message_id: int) -> None:
        await self._db.execute("UPDATE offers SET message_id = ? WHERE id = ?", (message_id, offer_id))

    async def set_recipient(self, offer_id: str, username: str | None) -> None:
        """Фиксирует получателя подарка для кнопки «Передать»."""
        await self._db.execute(
            "UPDATE offers SET recipient_username = ? WHERE id = ?", (username, offer_id)
        )

    async def delete(self, offer_id: str) -> int:
        return await self._db.execute("DELETE FROM offers WHERE id = ?", (offer_id,))

    async def claim(self, offer_id: str, status: str, actor_id: int) -> Row | None:
        """Атомарный переход ``pending -> status``.

        Возвращает строку предложения только тому «гонщику», который первым
        выполнил UPDATE. Это и есть защита от повторного нажатия кнопки:
        все последующие вызовы получают ``rowcount == 0`` и ``None``.
        """
        now = utcnow_iso()
        affected = await self._db.execute(
            """
            UPDATE offers
               SET status = ?, resolved_at = ?, resolved_by = ?
             WHERE id = ?
               AND status = ?
               AND (expires_at IS NULL OR expires_at > ?)
            """,
            (status, now, actor_id, offer_id, STATUS_PENDING, now),
        )
        if affected == 0:
            return None
        return await self.get(offer_id)

    async def list_pending(self, chat_id: int, limit: int = 20) -> list[Row]:
        return await self._db.fetch_all(
            "SELECT * FROM offers WHERE chat_id = ? AND status = ? ORDER BY created_at DESC LIMIT ?",
            (chat_id, STATUS_PENDING, limit),
        )

    async def count_by_status(self) -> dict[str, int]:
        rows = await self._db.fetch_all("SELECT status, COUNT(*) AS total FROM offers GROUP BY status")
        return {row["status"]: int(row["total"]) for row in rows}
