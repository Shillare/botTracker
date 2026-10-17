"""Админ-панель.

Права распределены так:

* **владелец** (``OWNER_IDS`` в ``.env``) — выдаёт и снимает права админов,
  смотрит список и бизнес-подключения;
* **админ** (таблица ``admins``) — полноценно пользуется ботом (карточки,
  кнопки, команды), но админ-панель ему недоступна;
* **все остальные** — бот их полностью игнорирует: ни ответа, ни реакции на
  подключение бизнес-аккаунта.

Не-админы отсекаются гейтом :class:`app.middlewares.AdminGateMiddleware`
до роутеров, поэтому проверка внутри обработчика — это уже «свой» случай:
админ, который попробовал выдать права. Отказ отвечает явно — отличать
«вы не владелец» от «бот вас не заметил» полезнее, чем молчание.

Панель служебная и ею пользуется владелец, поэтому тексты здесь только
русские: переводятся карточки предложений, а не служебные разделы.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.config import Settings
from app.database import AdminRepository, BusinessConnectionRepository, Database
from app.services.telegram import TelegramGateway
from app.utils.text import esc

logger = logging.getLogger(__name__)

ADAPTER_HINT = (
    "⚠️ Укажите корректный Telegram ID.\n"
    "<code>{command} 123456789</code>"
)

OWNER_ONLY_HINT = (
    "⛔ <b>Только для владельца.</b>\n\n"
    "Выдавать и снимать права администратора может только владелец бота "
    "(<code>OWNER_IDS</code> в <code>.env</code>)."
)

NO_CONNECTION_TEXT = "<i>Подключений нет.</i>"
NO_ADMINS_TEXT = "<i>Список пуст.</i>"
OWNER_BADGE = "(владелец)"


def parse_user_id(raw: str | None) -> int | None:
    """Достаёт Telegram ID из аргумента команды."""
    if not raw:
        return None
    candidate = raw.strip().lstrip("@")
    return int(candidate) if candidate.lstrip("-").isdigit() else None


def command_argument(text: str | None, command: str) -> str | None:
    """Возвращает аргумент команды, отсекая саму команду."""
    if not text:
        return None
    tokens = text.strip().split(maxsplit=1)
    return tokens[1] if len(tokens) > 1 and tokens[0].lower().lstrip("/") == command.lstrip("/") else None


async def render_admins(db: Database, settings: Settings) -> str:
    """Список админов; владельцы помечены короной."""
    rows = await AdminRepository(db).get_all()
    owners = settings.owner_id_set

    def owner_row(uid: int, username: str | None = None) -> str:
        name = esc(f"@{username}") if username else "<i>без username</i>"
        return f"• 👑 {name} <code>{uid}</code> <i>{OWNER_BADGE}</i>"

    def admin_row(uid: int, username: str | None = None) -> str:
        name = esc(f"@{username}") if username else "<i>без username</i>"
        return f"• {name} <code>{uid}</code>"

    lines = [
        owner_row(int(row["user_id"]), row["username"])
        if int(row["user_id"]) in owners
        else admin_row(int(row["user_id"]), row["username"])
        for row in rows
    ]
    # Владелец из .env без записи в БД тоже должен быть виден в списке.
    known = {int(row["user_id"]) for row in rows}
    lines += [owner_row(uid) for uid in sorted(owners - known)]

    if not lines:
        return NO_ADMINS_TEXT
    return f"<b>Администраторы ({len(lines)})</b>\n\n" + "\n".join(lines)


async def render_connections(db: Database) -> str:
    rows = await BusinessConnectionRepository(db).list_all()
    if not rows:
        return NO_CONNECTION_TEXT
    return "\n\n".join(
        "{status} — {account}\n<code>{cid}</code>".format(
            status="✅ активна" if row["is_enabled"] else "⛔ отключена",
            account=esc(f"@{row['username']}") if row["username"] else f"id={row['user_id']}",
            cid=esc(row["id"]),
        )
        for row in rows
    )


def create_admin_router() -> Router:
    """Фабрика роутера админ-панели: доступно только владельцу."""
    router = Router(name="admin")

    async def _deny_if_not_owner(
        message: Message,
        settings: Settings,
        gateway: TelegramGateway,
    ) -> bool:
        """``True`` — запрос отклонён, дальше идти не нужно."""
        if settings.is_owner(message.from_user.id if message.from_user else None):
            return False
        await gateway.send(
            message.chat.id,
            OWNER_ONLY_HINT,
            business_connection_id=message.business_connection_id,
            reply_to_message_id=message.message_id,
        )
        return True

    @router.message(Command("admins"))
    async def on_admins(
        message: Message, db: Database, settings: Settings, gateway: TelegramGateway
    ) -> None:
        if await _deny_if_not_owner(message, settings, gateway):
            return
        await gateway.send(
            message.chat.id,
            await render_admins(db, settings),
            business_connection_id=message.business_connection_id,
        )

    @router.message(Command("add_admin"))
    async def on_add_admin(
        message: Message, db: Database, settings: Settings, gateway: TelegramGateway
    ) -> None:
        if await _deny_if_not_owner(message, settings, gateway):
            return

        actor = message.from_user
        target_id = parse_user_id(command_argument(message.text, "add_admin"))
        if target_id is None or (actor is not None and target_id == actor.id):
            await gateway.send(
                message.chat.id,
                ADAPTER_HINT.format(command="/add_admin"),
                business_connection_id=message.business_connection_id,
            )
            return

        if settings.is_owner(target_id):
            await gateway.send(
                message.chat.id,
                f"ℹ️ <code>{target_id}</code> и так владелец бота.",
                business_connection_id=message.business_connection_id,
                reply_to_message_id=message.message_id,
            )
            return

        added = await AdminRepository(db).add(
            target_id,
            source="command",
            added_by=actor.id if actor else None,
        )
        await gateway.send(
            message.chat.id,
            f"✅ Админ <code>{target_id}</code> добавлен." if added
            else f"ℹ️ <code>{target_id}</code> уже является админом.",
            business_connection_id=message.business_connection_id,
            reply_to_message_id=message.message_id,
        )

    @router.message(Command("remove_admin"))
    async def on_remove_admin(
        message: Message, db: Database, settings: Settings, gateway: TelegramGateway
    ) -> None:
        if await _deny_if_not_owner(message, settings, gateway):
            return

        repository = AdminRepository(db)
        target_id = parse_user_id(command_argument(message.text, "remove_admin"))
        if target_id is None:
            await gateway.send(
                message.chat.id,
                ADAPTER_HINT.format(command="/remove_admin"),
                business_connection_id=message.business_connection_id,
            )
            return

        # Владельца нельзя разжаловать: иначе бот можно «замуровать» навсегда.
        if settings.is_owner(target_id):
            await gateway.send(
                message.chat.id,
                "⛔ Права владельца снять нельзя.",
                business_connection_id=message.business_connection_id,
                reply_to_message_id=message.message_id,
            )
            return

        # Страховка от «замуровки» нужна только когда владельца нет: он всегда
        # админ и может вернуть любые права.
        if not settings.owner_id_set and await repository.count() <= 1:
            await gateway.send(
                message.chat.id,
                "⛔ Нельзя снять права с последнего администратора.",
                business_connection_id=message.business_connection_id,
            )
            return

        removed = await repository.remove(target_id)
        await gateway.send(
            message.chat.id,
            f"✅ Права администратора <code>{target_id}</code> сняты." if removed
            else f"ℹ️ <code>{target_id}</code> не является админом.",
            business_connection_id=message.business_connection_id,
            reply_to_message_id=message.message_id,
        )

    @router.message(Command("business_status"))
    async def on_business_status(
        message: Message, db: Database, settings: Settings, gateway: TelegramGateway
    ) -> None:
        if await _deny_if_not_owner(message, settings, gateway):
            return
        await gateway.send(
            message.chat.id,
            f"<b>Бизнес-подключения</b>\n\n{await render_connections(db)}",
            business_connection_id=message.business_connection_id,
        )

    return router