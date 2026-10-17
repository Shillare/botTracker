"""Основные команды: /start, /help, /business_info.

Текст справки переводится: язык берётся из хвоста команды
(``benz ... EU`` -> английский), иначе — русский.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.database import BusinessConnectionRepository, Database
from app.i18n import resolve_lang
from app.services.telegram import TelegramGateway
from app.utils.text import esc

logger = logging.getLogger(__name__)

HELP_TEXT: dict[str, str] = {
    "ru": (
        "<b>Как продать подарок за валюту</b>\n\n"
        "Отправьте команду в любом чате (точка или слэш, можно с «s» в конце):\n"
        "<code>benz https://t.me/nft/StarNotepad-21211 2500 Stars</code>\n"
        "<code>benz https://t.me/nft/StarNotepad-21211 2.5k Gram</code>\n\n"
        "Язык карточки — последний аргумент: <code>EU</code> (английский), "
        "<code>ar</code> (арабский). Без него карточка на русском.\n\n"
        "Бот сформирует карточку с кнопками «Отклонить» и «Принять». "
        "После принятия появится кнопка «Передать» — она открывает интерфейс "
        "передачи подарка покупателю.\n\n"
        "<b>Команды</b>\n"
        "<code>benz</code> — создать предложение\n"
        "<code>/id</code> — мой Telegram ID\n"
        "<code>/business_info</code> — статус бизнес-подключения\n"
        "<code>/admins</code>, <code>/add_admin</code>, <code>/remove_admin</code> — "
        "админ-панель\n"
    ),
    "en": (
        "<b>How to sell a gift for currency</b>\n\n"
        "Send the command in any chat (dot or slash, a trailing «s» is optional):\n"
        "<code>benz https://t.me/nft/StarNotepad-21211 2500 Stars</code>\n"
        "<code>benz https://t.me/nft/StarNotepad-21211 2.5k Gram</code>\n\n"
        "The card language is the last argument: <code>EU</code> (English), "
        "<code>ar</code> (Arabic). Without it the card is in Russian.\n\n"
        "The bot posts a card with Decline and Accept buttons. Once it is "
        "accepted, a Transfer button appears — it opens the gift transfer screen.\n\n"
        "<b>Commands</b>\n"
        "<code>benz</code> — create an offer\n"
        "<code>/id</code> — my Telegram ID\n"
        "<code>/business_info</code> — business connection status\n"
        "<code>/admins</code>, <code>/add_admin</code>, <code>/remove_admin</code> — "
        "admin panel\n"
    ),
    "ar": (
        "<b>كيف تبيع هدية مقابل عملة</b>\n\n"
        "أرسل الأمر في أي محادثة (نقطة أو شرطة مائلة، وحرف «s» في النهاية اختياري):\n"
        "<code>benz https://t.me/nft/StarNotepad-21211 2500 Stars</code>\n"
        "<code>benz https://t.me/nft/StarNotepad-21211 2.5k Gram</code>\n\n"
        "لغة البطاقة هي الوسيط الأخير: <code>EU</code> (إنجليزي)، "
        "<code>ar</code> (عربي). بدونه تكون البطاقة بالروسية.\n\n"
        "سيعرض البوت بطاقة بزرّي «رفض» و«قبول»، وبعد القبول يظهر زر «تحويل» "
        "الذي يفتح شاشة إرسال الهدية.\n\n"
        "<b>الأوامر</b>\n"
        "<code>benz</code> — إنشاء عرض\n"
        "<code>/id</code> — معرّف Telegram\n"
        "<code>/business_info</code> — حالة اتصال الحساب التجاري\n"
        "<code>/admins</code>، <code>/add_admin</code>، <code>/remove_admin</code> — "
        "لوحة الإدارة\n"
    ),
}

BUSINESS_INFO_TEXT = (
    "<b>Бизнес-подключения</b>\n\n"
    "{connections}\n\n"
    "<i>Подключение выполняется в Telegram: Настройки → Бизнес → Подключить аккаунт.</i>"
)

ID_TEXT = {
    "role_owner": "👑 владелец бота",
    "role_admin": "✅ администратор",
    "role_none": "⛔ доступа нет — бот отвечает только владельцу и админам",
    "how_to_get": (
        "Передайте этот ID владельцу бота — он выдаст права командой "
        "<code>/add_admin {uid}</code>."
    ),
}


def language_from_text(text: str | None) -> str:
    """Язык из хвоста команды: ``benz ... EU`` -> ``en``."""
    if not text:
        return "ru"
    for token in reversed(text.split()):
        code = resolve_lang(token)
        if code is not None:
            return code
    return "ru"


def create_commands_router() -> Router:
    """Фабрика роутера: экземпляр можно подключить только к одному Dispatcher."""
    router = Router(name="commands")

    @router.message(CommandStart())
    async def on_start(message: Message, gateway: TelegramGateway) -> None:
        lang = language_from_text(message.text)
        await gateway.send(
            message.chat.id,
            HELP_TEXT[lang],
            business_connection_id=message.business_connection_id,
        )

    @router.message(Command("help"))
    async def on_help(message: Message, gateway: TelegramGateway) -> None:
        lang = language_from_text(message.text)
        await gateway.send(
            message.chat.id,
            HELP_TEXT[lang],
            business_connection_id=message.business_connection_id,
        )

    @router.message(Command("business_info", "бизнес"))
    async def on_business_info(message: Message, db: Database, gateway: TelegramGateway) -> None:
        await gateway.send(
            message.chat.id,
            BUSINESS_INFO_TEXT.format(connections=await _render_connections(db)),
            business_connection_id=message.business_connection_id,
        )

    @router.message(Command("id", "whoami"))
    async def on_id(
        message: Message,
        gateway: TelegramGateway,
        is_admin: bool,
        is_owner: bool,
    ) -> None:
        """Свой Telegram ID и текущая роль.

        Эта команда отвечает **всем**, включая тех, кому закрыт весь остальной
        бот (см. :data:`app.middlewares.PUBLIC_COMMANDS`).

        Зачем при нарушении «не-админам бот не отвечает»: свой ID невозможно
        вычислить в уме, а без него посторонний не понимает причину молчания —
        сообщение в чате висит с часиками, и человек ждёт ответа, которого
        не будет никогда. Одна короткая команда вместо бесконечного ожидания.

        Роль сообщается честно: она и есть ответ на вопрос «почему меня
        игнорируют».
        """
        user = message.from_user
        if user is None:
            return

        role = ID_TEXT["role_owner"] if is_owner else ID_TEXT["role_admin" if is_admin else "role_none"]
        lines = [f"🆔 <b>Ваш Telegram ID</b>: <code>{user.id}</code>"]
        if user.username:
            lines.append(f"Юзернейм: @{esc(user.username)}")
        lines.append("")
        lines.append(f"Роль: {role}")
        if not is_admin:
            lines.append("")
            lines.append(ID_TEXT["how_to_get"].format(uid=user.id))

        await gateway.send(
            message.chat.id,
            "\n".join(lines),
            business_connection_id=message.business_connection_id,
        )

    return router


async def _render_connections(db: Database) -> str:
    """Подключения с вердиктом «может ли бот писать от имени аккаунта».

    Права выдаются в галочках при подключении, и их легко не заметить: бот
    пишет «подключён», а отправка от имени аккаунта не проходит. Показываем
    вердикт прямо здесь — иначе причину приходится искать в логах.
    """
    connections = await BusinessConnectionRepository(db).list_all()
    if not connections:
        return "<i>Подключений пока нет.</i>"

    blocks: list[str] = []
    can_send_any = False
    for row in connections:
        active = bool(row["is_enabled"])
        can_reply = bool(row["can_reply"])
        if active and can_reply:
            can_send_any = True
            verdict = "✅ отправляет от имени аккаунта"
        elif active:
            verdict = (
                "⛔ <b>не может отправлять от имени аккаунта</b>: не выдано право "
                "«Разрешить отвечать на сообщения»"
            )
        else:
            verdict = "⛔ отключено"

        blocks.append(
            "{status} — {account}\n<code>{cid}</code>\n{verdict}".format(
                status="✅ активна" if active else "⛔ отключена",
                account=esc(f"@{row['username']}") if row["username"] else f"id={row['user_id']}",
                cid=esc(row["id"]),
                verdict=verdict,
            )
        )

    summary = (
        ""
        if can_send_any
        else "\n\n⚠️ <b>Сейчас карточки будут уходить от имени бота, а не аккаунта.</b>\n"
        "Переподключите бота: Telegram → Настройки → Бизнес → отметьте все права, "
        "включая «Разрешить отвечать на сообщения». Право действует только на личные чаты, "
        "в которых были входящие сообщения за последние 24 часа."
    )
    return "\n\n".join(blocks) + summary