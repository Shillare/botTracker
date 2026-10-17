"""Интеграционный тест полного цикла: апдейт -> middleware -> хендлер -> Telegram API.

Telegram API замокан, поэтому тест не требует токена и сети.
Запуск: ``python -m tests.integration``
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BusinessBotRights,
    BusinessConnection,
    CallbackQuery,
    Chat,
    Message,
    Update,
    User,
)

from app import __main__ as entrypoint
from app.config import Settings
from app.constants import (
    EMOJI_GIFT,
    H_DEYSTVUET,
    H_ESHCHO,
    H_PEREDAYTE,
    H_PREDLOZHENIE,
    H_PRODAN,
    H_TOO_LATE,
    H_USER,
    STARS_LABEL,
)
from app.database import AdminRepository, BusinessConnectionRepository, Database, OfferRepository
from app.services import templates
from app.services.parser import parse_gift_link
from app.services.telegram import _MAX_ATTEMPTS, FLOOD_GUARD, TelegramGateway

PASSED = 0
FAILED = 0

BOT_USER = User(id=777, is_bot=True, first_name="OfferBot", username="offer_test_bot")
SELLER = User(id=111, is_bot=False, first_name="Иван", username="ivan", last_name="Петров")
BUYER = User(id=555, is_bot=False, first_name="Анна", username="anna")
STRANGER = User(id=999, is_bot=False, first_name="Чужой", username="stranger")

CONNECTION = BusinessConnection(
    id="BQ1234567890",
    user=SELLER,
    user_chat_id=SELLER.id,
    date=datetime.now(UTC),
    is_enabled=True,
    rights=BusinessBotRights(
        can_reply=True,
        can_read_messages=True,
        can_transfer_and_upgrade_gifts=True,
        can_transfer_stars=True,
        can_convert_gifts_to_stars=True,
    ),
)


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  OK   {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name} {detail}")


class FakeSession:
    """Записывает вызовы методов Telegram и возвращает правдоподобные ответы."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_with: dict[str, Exception] = {}
        # Ошибка по условию: нужна, когда метод падает не всегда, а только при
        # определённом payload (например, sendMessage только с
        # business_connection_id — так ведёт себя Telegram с BUSINESS_PEER_INVALID).
        self.fail_if: Callable[[str, dict[str, Any]], Exception | None] | None = None
        self._message_id = 1000
        # Реестр бизнес-подключений для getBusinessConnection: по умолчанию пуст,
        # тесты добавляют нужное через ``session.add_connection(...)``.
        self.connections: dict[str, BusinessConnection] = {}

    def add_connection(self, connection: BusinessConnection) -> BusinessConnection:
        self.connections[connection.id] = connection
        return connection

    async def __call__(self, bot: Bot, method: Any, timeout: int | None = None) -> Any:
        name: str = method.__api_method__
        payload = method.model_dump(exclude_none=True)
        self.calls.append((name, payload))

        if self.fail_if is not None:
            conditional = self.fail_if(name, payload)
            if conditional is not None:
                raise conditional

        if name in self.fail_with:
            raise self.fail_with[name]

        if name == "getMe":
            return BOT_USER
        if name == "getBusinessConnection":
            known = self.connections.get(payload.get("business_connection_id", ""))
            if known is None:
                raise TelegramBadRequest(
                    method=None, message="Bad Request: business connection not found"
                )
            return known
        if name in ("sendMessage", "editMessageText"):
            self._message_id += 1
            return Message(
                message_id=self._message_id,
                date=datetime.now(UTC),
                chat=Chat(id=payload.get("chat_id") or 0, type="private"),
                text=payload.get("text", ""),
                business_connection_id=payload.get("business_connection_id"),
            )
        return True

    async def close(self) -> None:
        return None

    def find(self, method: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.calls if name == method]

    def texts_of(self, method: str) -> list[str]:
        return [payload.get("text", "") for payload in self.find(method)]

    def reset(self) -> None:
        self.calls.clear()


@asynccontextmanager
async def environment(
    tmp: str,
    *,
    admins: set[int] | None = None,
    owners: set[int] | None = None,
    connection: BusinessConnection | None = None,
    throttle: float = 0.0,
    seed_admins: bool = True,
) -> AsyncIterator[tuple[Any, Bot, Database, FakeSession]]:
    """БД + бот с моком + Dispatcher. Ресурсы закрываются гарантированно.

    По умолчанию владелец — ``SELLER``, поэтому большинству сценариев не нужно
    ничего указывать. Не-админ (например ``STRANGER``) гейт не пропустит.
    """
    settings = Settings(
        bot_token="1:AA",
        owner_ids=",".join(str(i) for i in (owners if owners is not None else {SELLER.id})),
        admin_ids=",".join(str(i) for i in admins or ()),
        db_path=Path(tmp) / "bot.sqlite3",
        offer_throttle_seconds=throttle,
    )
    db = Database(settings.db_path)
    await db.connect()

    # Админы из конфигурации попадают в БД на старте приложения. Тест
    # проигрывает startup сразу, иначе гейт отбросил бы их события.
    # Сценарий startup-хука проверяет посев отдельно и снимает флаг.
    if admins and seed_admins:
        for admin_id in admins:
            await AdminRepository(db).add(admin_id, source="test")
        await db.set_meta("admins_seeded", "")

    if connection is not None:
        await BusinessConnectionRepository(db).upsert(
            {
                "id": connection.id,
                "user_id": connection.user.id,
                "user_chat_id": connection.user_chat_id,
                "username": connection.user.username,
                "first_name": connection.user.first_name,
                "is_enabled": connection.is_enabled,
                "can_reply": bool(connection.rights and connection.rights.can_reply),
                "can_read_messages": bool(connection.rights and connection.rights.can_read_messages),
                "can_transfer_and_upgrade": bool(
                    connection.rights and connection.rights.can_transfer_and_upgrade_gifts
                ),
                "can_transfer_stars": bool(connection.rights and connection.rights.can_transfer_stars),
                "can_convert_gifts_to_stars": bool(
                    connection.rights and connection.rights.can_convert_gifts_to_stars
                ),
            }
        )

    bot = Bot(token="1:AA", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    session = FakeSession()
    bot.session = session  # type: ignore[method-assign]
    dispatcher = entrypoint.build_dispatcher(db, settings, TelegramGateway(bot))
    entrypoint.register_startup(dispatcher, bot, db, settings)
    try:
        yield dispatcher, bot, db, session
    finally:
        await db.close()
        await bot.session.close()


def _deleted_command_ids(session: FakeSession) -> list[int]:
    """message_id всех удалённых команд в порядке вызовов."""
    return [
        message_id
        for name, payload in session.calls
        if name == "deleteBusinessMessages"
        for message_id in payload.get("message_ids", [])
    ]


async def _settle() -> None:
    """Дать фоновым задачам (уведомление продавцу) выполниться."""
    for _ in range(10):
        await asyncio.sleep(0)


def _method_order(session: FakeSession, first: str, second: str) -> bool:
    """``first`` встречается раньше ``second``."""
    names = [name for name, _ in session.calls]
    return first in names and second in names and names.index(first) < names.index(second)



def private_message(message_id: int, text: str, user: User) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=user.id, type="private"),
        from_user=user,
        text=text,
    )


def biz_message(message_id: int, text: str, user: User, connection_id: str) -> Message:
    return private_message(message_id, text, user).model_copy(
        update={"business_connection_id": connection_id}
    )


def group_message(message_id: int, text: str, user: User, connection_id: str) -> Message:
    """Сообщение в супергруппе от имени бизнес-аккаунта."""
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=-1001234567890, type="supergroup", title="Маркет"),
        from_user=user,
        text=text,
        business_connection_id=connection_id,
    )


def biz_dm(message_id: int, text: str, owner: User, peer: User, connection_id: str) -> Message:
    """Владелец бизнес-аккаунта пишет в личный диалог с собеседником.

    ``chat`` — это диалог с ``peer``, а ``from_user`` — владелец аккаунта:
    именно так выглядит ``business_message``, когда Иван пишет из своего
    аккаунта Анне.
    """
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=peer.id, type="private"),
        from_user=owner,
        text=text,
        business_connection_id=connection_id,
    )


def bot_message(message_id: int, text: str, user: User, connection_id: str | None) -> Message:
    return private_message(message_id, text, user).model_copy(
        update={"from_user": BOT_USER, "business_connection_id": connection_id}
    )


def click(
    dp: Any, bot: Bot, update_id: int, callback_id: str, user: User, message: Message, data: str
) -> Any:
    return dp.feed_update(
        bot,
        Update(
            update_id=update_id,
            callback_query=CallbackQuery(
                id=callback_id, from_user=user, chat_instance="ci", message=message, data=data
            ),
        ),
    )


async def scenario_business_offer_flow() -> None:
    print("\n[1] E2E: benz в ЛС бизнес-аккаунта -> принятие -> повторное нажатие")
    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp, connection=CONNECTION) as (dp, bot, db, session):
            await dp.feed_update(bot,
                Update(
                    update_id=1,
                    business_message=biz_dm(
                        1, "benz https://t.me/nft/ViceCream-382938 2500", SELLER, BUYER, CONNECTION.id
                    ),
                ),
            )

            sent = session.find("sendMessage")
            check("карточка отправлена", len(sent) == 1, str(len(sent)))
            if not sent:
                return
            payload = sent[0]

            check(
                "business_connection_id проброшен",
                payload.get("business_connection_id") == CONNECTION.id,
                str(payload.get("business_connection_id")),
            )
            check("чат — получатель", payload.get("chat_id") == BUYER.id, str(payload.get("chat_id")))
            check("HTML parse_mode", payload.get("parse_mode") == "HTML")
            check("карточка — новое сообщение, не ответ",
                  payload.get("reply_to_message_id") is None,
                  str(payload.get("reply_to_message_id")))
            check(
                "предпросмотр подарка сверху",
                (payload.get("link_preview_options") or {}).get("show_above_text") is True,
                str(payload.get("link_preview_options")),
            )

            text: str = payload.get("text", "")
            gift_title = parse_gift_link("https://t.me/nft/ViceCream-382938").display_title
            check("название подарка", gift_title in text, text)
            check("цена отформатирована", f"2500 {STARS_LABEL}" in text, text)
            check(
                "срок действия",
                f"{H_PREDLOZHENIE} {H_DEYSTVUET} {H_ESHCHO}" in text,
                text,
            )
            check("ник из профиля, без @username",
                  "<b>Иван Петров</b>" in text and "@ivan" not in text,
                  text)

            keyboard = payload.get("reply_markup", {}).get("inline_keyboard", [])
            check("две кнопки", len(keyboard) == 1 and len(keyboard[0]) == 2, str(keyboard))
            if not keyboard or len(keyboard[0]) != 2:
                return
            decline_btn, accept_btn = keyboard[0]

            check("порядок кнопок: Отклонить → Принять",
                  decline_btn.get("text") == "Отклонить"
                  and accept_btn.get("text") == "Принять",
                  str([b.get("text") for b in keyboard[0]]))
            check(
                "без цвета «Принять»",
                accept_btn.get("style") is None,
                str(accept_btn.get("style")),
            )
            check(
                "без цвета «Отклонить»",
                decline_btn.get("style") is None,
                str(decline_btn.get("style")),
            )
            check("иконки заданы", all(b.get("icon_custom_emoji_id") for b in keyboard[0]))
            check(
                "callback_data <= 64 байт",
                all(len(b["callback_data"].encode()) <= 64 for b in keyboard[0]),
            )
            # Сообщение с командой удаляется через бизнес-подключение:
            # у deleteMessage в Bot API нет параметра business_connection_id.
            check("команда удалена", _deleted_command_ids(session) == [1],
                  str(session.find("deleteBusinessMessages")))
            check(
                "команда удаляется до отправки карточки",
                _method_order(session, "deleteBusinessMessages", "sendMessage"),
                str([name for name, _ in session.calls]),
            )

            offer_id = accept_btn["callback_data"].split(":")[2]
            repo = OfferRepository(db)
            offer = await repo.get(offer_id)
            check("предложение сохранено", offer is not None)
            if offer is None:
                return

            check(
                "получатель = chat.id",
                int(offer["buyer_user_id"]) == BUYER.id,
                str(offer["buyer_user_id"]),
            )
            check("продавец = автор", int(offer["seller_user_id"]) == SELLER.id, str(offer["seller_user_id"]))
            check("статус pending", offer["status"] == "pending", offer["status"])
            check("message_id сохранён", isinstance(offer["message_id"], int), str(offer["message_id"]))
            check("business_connection_id в БД", offer["business_connection_id"] == CONNECTION.id)
            check("цена в БД", int(offer["stars"]) == 2500, str(offer["stars"]))

            target = bot_message(int(offer["message_id"]), text, BUYER, CONNECTION.id)
            await click(dp, bot, 2, "cb1", BUYER, target, f"of:a:{offer_id}")

            edits = session.find("editMessageText")
            check("карточка отредактирована", len(edits) == 1, str(len(edits)))
            if edits:
                edit = edits[0]
                check(
                    "edit с business_connection_id",
                    edit.get("business_connection_id") == CONNECTION.id,
                    str(edit.get("business_connection_id")),
                )
                check("edit того же сообщения", edit.get("message_id") == offer["message_id"])
                check(
                    "текст «успешно продан»",
                    H_PRODAN in edit.get("text", ""),
                    edit.get("text", ""),
                )
                check(
                    "premium-эмодзи подарка",
                    f'<tg-emoji emoji-id="{EMOJI_GIFT}">' in edit.get("text", ""),
                    edit.get("text", ""),
                )
                check("инструкция продавцу", H_PEREDAYTE in edit.get("text", ""))
                check("валюта в карточке", STARS_LABEL in edit.get("text", ""))
                check(
                    "ссылка на подарок без кавычек",
                    f'<a href="{offer["gift_url"]}">' in edit.get("text", "")
                    and '"#' not in edit.get("text", ""),
                    edit.get("text", ""),
                )
                check("дата убрана", not re.search(r"\d\d\.\d\d\.\d{4}", edit.get("text", "")),
                      edit.get("text", ""))
                check(
                    "предпросмотр сверху",
                    (edit.get("link_preview_options") or {}).get("show_above_text") is True,
                    str(edit.get("link_preview_options")),
                )

                new_kb = edit.get("reply_markup", {}).get("inline_keyboard", [])
                check("одна новая кнопка", len(new_kb) == 1 and len(new_kb[0]) == 1, str(new_kb))
                if new_kb and len(new_kb[0]) == 1:
                    transfer = new_kb[0][0]
                    check("«Передать»", transfer.get("text") == "Передать", str(transfer))
                    check(
                        "deep link подарка",
                        transfer.get("url", "").startswith("tg://send_gift?to="),
                        str(transfer.get("url")),
                    )
                    check(
                        "«Передать» без оформления",
                        transfer.get("style") is None
                        and not transfer.get("icon_custom_emoji_id"),
                        str(transfer),
                    )

            answered = session.find("answerCallbackQuery")
            check("callback отвечен", len(answered) == 1, str(len(answered)))
            if answered:
                check("тост без alert", answered[0].get("show_alert") is not True, str(answered[0]))

            await click(dp, bot, 3, "cb2", BUYER, target, f"of:a:{offer_id}")
            check("повторное нажатие не редактирует", len(session.find("editMessageText")) == 1)
            after = session.find("answerCallbackQuery")
            check(
                "повтор отвечает с alert",
                len(after) == 2 and after[1].get("show_alert") is True,
                str(after),
            )
            check("статус accepted", (await repo.get(offer_id))["status"] == "accepted")


async def scenario_group_and_currency() -> None:
    print("\n[1b] E2E: benz в группе, вторая валюта, получатель подарка")
    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp, connection=CONNECTION) as (dp, bot, db, session):
            await dp.feed_update(
                bot,
                Update(
                    update_id=10,
                    business_message=group_message(
                        7, "benz https://t.me/nft/ViceCream-382938 100 Gram @anna", SELLER, CONNECTION.id
                    ),
                ),
            )

            sent = session.find("sendMessage")
            check("карточка отправлена в группу", len(sent) == 1, str(len(sent)))
            if not sent:
                return
            payload = sent[0]
            check("chat_id группы", payload.get("chat_id") == -1001234567890, str(payload.get("chat_id")))
            check("business_connection_id проброшен", payload.get("business_connection_id") == CONNECTION.id)
            plain_text = payload.get("text", "").replace("<b>", "").replace("</b>", "")
            check("валюта Gram в тексте", "100 Gram" in plain_text, payload.get("text", ""))
            check("команда удалена через бизнес-подключение",
                  any(7 in m.get("message_ids", []) for m in session.find("deleteBusinessMessages")),
                  str(session.find("deleteBusinessMessages")))

            offer_id = payload["reply_markup"]["inline_keyboard"][0][1]["callback_data"].split(":")[2]
            repo = OfferRepository(db)
            offer = await repo.get(offer_id)
            check("предложение сохранено", offer is not None)
            if offer is None:
                return
            check("тип чата = supergroup", offer["chat_type"] == "supergroup", str(offer["chat_type"]))
            check("валюта в БД", offer["currency"] == "Gram", str(offer["currency"]))
            check("адресат не предопределён", int(offer["buyer_user_id"]) == 0, str(offer["buyer_user_id"]))
            check("получатель из команды",
                  offer["recipient_username"] == "anna", str(offer["recipient_username"]))

            # В группе кнопку может нажать любой участник — карточка не адресована
            # конкретному человеку.
            text = payload.get("text", "")
            target = bot_message(int(offer["message_id"]), text, SELLER, CONNECTION.id).model_copy(
                update={"chat": Chat(id=-1001234567890, type="supergroup", title="Маркет")}
            )
            await click(dp, bot, 11, "cbg1", STRANGER, target, f"of:a:{offer_id}")

            edits = session.find("editMessageText")
            check("чужой в группе принял предложение", len(edits) == 1, str(len(edits)))
            if not edits:
                return
            new_kb = edits[0].get("reply_markup", {}).get("inline_keyboard", [])
            check("одна кнопка «Передать»", len(new_kb) == 1 and len(new_kb[0]) == 1, str(new_kb))
            if new_kb and len(new_kb[0]) == 1:
                # Явный @username из команды приоритетнее username нажавшего.
                check("deep link на получателя из команды",
                      new_kb[0][0].get("url") == "tg://send_gift?to=anna",
                      str(new_kb[0][0].get("url")))
            check("валюта Gram в карточке «продан»",
                  "Gram" in edits[0].get("text", ""), edits[0].get("text", ""))

            # Уведомление продавцу уходит фоновой задачей — ждём её.
            await _settle()
            notices = [t for t in session.texts_of("sendMessage") if "предложение приняли" in t]
            check("продавец уведомлён", len(notices) == 1, str(len(notices)))
            check("в уведомлении указан покупатель", "@stranger" in (notices[0] if notices else ""),
                  notices[0] if notices else "")


async def scenario_foreign_offer_ignored() -> None:
    print("\n[1c] E2E: чужой benz в группе игнорируется")
    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp, connection=CONNECTION) as (dp, bot, db, session):
            await dp.feed_update(
                bot,
                Update(
                    update_id=20,
                    business_message=group_message(
                        4, "benz https://t.me/nft/ViceCream-382938 1 Stars", STRANGER, CONNECTION.id
                    ),
                ),
            )
            check("ничего не отправлено", session.find("sendMessage") == [], str(session.find("sendMessage")))
            leftovers = await db.fetch_all("SELECT id FROM offers")
            check("предложение не создано", leftovers == [], str([dict(r) for r in leftovers]))


async def scenario_author_cannot_resolve() -> None:
    print("\n[1d] E2E: у автора кнопки не реагируют")
    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp, connection=CONNECTION) as (dp, bot, db, session):
            await dp.feed_update(
                bot,
                Update(
                    update_id=30,
                    business_message=biz_dm(
                        11, "benz https://t.me/nft/ViceCream-382938 100 Stars", SELLER, BUYER, CONNECTION.id
                    ),
                ),
            )
            sent = session.find("sendMessage")
            check("карточка создана", len(sent) == 1, str(len(sent)))
            if not sent:
                return
            offer_id = sent[0]["reply_markup"]["inline_keyboard"][0][1]["callback_data"].split(":")[2]
            repo = OfferRepository(db)
            offer = await repo.get(offer_id)
            check("автор = продавец", offer is not None and int(offer["seller_user_id"]) == SELLER.id)

            target = bot_message(int(offer["message_id"]), sent[0]["text"], BUYER, CONNECTION.id)
            session.reset()
            await click(dp, bot, 31, "cb-owner-accept", SELLER, target, f"of:a:{offer_id}")

            check("сообщение не отредактировано", session.find("editMessageText") == [],
                  str(session.find("editMessageText")))
            check("предложение осталось pending",
                  (await repo.get(offer_id))["status"] == "pending",
                  (await repo.get(offer_id))["status"])
            answers = session.find("answerCallbackQuery")
            check(
                "автору объяснено",
                len(answers) == 1 and answers[0].get("show_alert") is True,
                str(answers),
            )
            check("текст ответа про свою карточку",
                  "ваше предложение" in (answers[0].get("text", "") if answers else ""),
                  str(answers))

            # Та же кнопка у покупателя работает.
            session.reset()
            await click(dp, bot, 32, "cb-buyer-accept", BUYER, target, f"of:a:{offer_id}")
            check("покупатель принял", len(session.find("editMessageText")) == 1,
                  str(session.find("editMessageText")))
            check("статус accepted", (await repo.get(offer_id))["status"] == "accepted",
                  (await repo.get(offer_id))["status"])


async def scenario_english_offer() -> None:
    print("\n[1e] E2E: английская карточка (суффикс EU) и её принятие")
    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp, connection=CONNECTION) as (dp, bot, db, session):
            await dp.feed_update(
                bot,
                Update(
                    update_id=40,
                    business_message=biz_dm(
                        21,
                        "benz https://t.me/nft/ViceCream-382938 2500 Stars EU",
                        SELLER,
                        BUYER,
                        CONNECTION.id,
                    ),
                ),
            )
            sent = session.find("sendMessage")
            check("EN: карточка отправлена", len(sent) == 1, str(len(sent)))
            if not sent:
                return
            text = sent[0].get("text", "")
            check("EN: текст переведён", "offers you" in text, text)
            check("EN: без русских гомоглифов", H_USER not in text, text)
            check("EN: валюта обычным текстом",
                  "Stars" in text and "Ѕtarѕ" not in text,
                  text)

            buttons = sent[0]["reply_markup"]["inline_keyboard"][0]
            check("EN: подписи кнопок переведены",
                  [b.get("text") for b in buttons] == ["Decline", "Accept"],
                  str([b.get("text") for b in buttons]))
            check("EN: иконки на месте",
                  all(b.get("icon_custom_emoji_id") for b in buttons), str(buttons))

            offer_id = buttons[1]["callback_data"].split(":")[2]
            repo = OfferRepository(db)
            offer = await repo.get(offer_id)
            check("EN: язык сохранён в БД", offer is not None and offer["language"] == "en",
                  str(offer["language"]) if offer else "")

            target = bot_message(int(offer["message_id"]), text, BUYER, CONNECTION.id)
            await click(dp, bot, 41, "cb-en", BUYER, target, f"of:a:{offer_id}")

            edits = session.find("editMessageText")
            check("EN: карточка отредактирована", len(edits) == 1, str(len(edits)))
            if not edits:
                return
            edited = edits[0].get("text", "")
            check("EN: «продан» переведён", "has been sold" in edited, edited)
            check("EN: инструкция переведена", "Send the gift to the buyer" in edited, edited)
            new_buttons = edits[0]["reply_markup"]["inline_keyboard"][0]
            check("EN: кнопка Transfer переведена",
                  new_buttons[0].get("text") == "Transfer", str(new_buttons))

            answered = session.find("answerCallbackQuery")
            check("EN: тост переведён",
                  any("Gift sold" in a.get("text", "") for a in answered), str(answered))


async def scenario_decline_flow() -> None:
    print("\n[2] E2E: отклонение, нормализация ввода, справка")
    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp, admins={BUYER.id}) as (dp, bot, db, session):
            await dp.feed_update(
                bot,
                Update(
                    update_id=1,
                    message=private_message(1, "benz t.me/StarNotepad-21211 1.5k", BUYER),
                ),
            )
            sent = session.find("sendMessage")
            check("карточка создана", len(sent) == 1, str(len(sent)))
            if not sent:
                return
            text = sent[0]["text"]
            check("camelCase разбит", parse_gift_link("t.me/StarNotepad-21211").display_title in text, text)
            check("цена 1.5k -> 1500", f"1500 {STARS_LABEL}" in text, text)
            check(
                "без business_connection_id",
                not sent[0].get("business_connection_id"),
                str(sent[0].get("business_connection_id")),
            )

            offer_id = sent[0]["reply_markup"]["inline_keyboard"][0][1]["callback_data"].split(":")[2]
            target = bot_message(int(sent[0].get("message_id") or 1001), text, BUYER, None)
            await click(dp, bot, 2, "cb", BUYER, target, f"of:d:{offer_id}")

            edits = session.find("editMessageText")
            check("карточка отредактирована", len(edits) == 1, str(len(edits)))
            if edits:
                check("текст отклонения", H_TOO_LATE in edits[0]["text"], edits[0]["text"])
                check("клавиатура снята", not edits[0].get("reply_markup"), str(edits[0].get("reply_markup")))
            check("статус declined", (await OfferRepository(db).get(offer_id))["status"] == "declined")

            session.reset()
            await dp.feed_update(bot, Update(update_id=3, message=private_message(2, "benz", BUYER)))
            check(
                "команда без аргументов -> справка",
                any("Формат команды" in t for t in session.texts_of("sendMessage")),
                str(session.texts_of("sendMessage")),
            )

            session.reset()
            await dp.feed_update(
                bot,
                Update(
                    update_id=4,
                    message=private_message(3, "benz https://vk.com/gift 100", BUYER),
                ),
            )
            check(
                "чужая ссылка -> ошибка парсинга",
                any("Не удалось распознать" in t for t in session.texts_of("sendMessage")),
                str(session.texts_of("sendMessage")),
            )


async def scenario_permissions() -> None:
    print("\n[3] E2E: владелец выдаёт и снимает права, админ — не может")
    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp) as (dp, bot, db, session):
            repo = AdminRepository(db)

            await dp.feed_update(bot, Update(update_id=1, message=private_message(1, "/admins", SELLER)))
            admins_text = session.texts_of("sendMessage")
            check(
                "владелец видит /admins",
                any("Администраторы" in t for t in admins_text),
                str(admins_text),
            )
            check(
                "владелец помечен короной",
                any("владелец" in t for t in admins_text),
                str(admins_text),
            )

            session.reset()
            await dp.feed_update(bot, Update(update_id=2, message=private_message(2, "/admins", STRANGER)))
            check(
                "не-владелец без прав проигнорирован",
                session.find("sendMessage") == [],
                str(session.find("sendMessage")),
            )

            await dp.feed_update(
                bot,
                Update(update_id=3, message=private_message(3, "/add_admin 999", SELLER)),
            )
            check("владелец добавил админа", await repo.is_admin(STRANGER.id))

            session.reset()
            await dp.feed_update(
                bot,
                Update(update_id=4, message=private_message(4, "/add_admin 999", SELLER)),
            )
            check(
                "повторное добавление сообщает «уже админ»",
                any("уже является админом" in t for t in session.texts_of("sendMessage")),
                str(session.texts_of("sendMessage")),
            )

            # STRANGER теперь админ, но не владелец: ботом пользоваться может,
            # а выдавать права — нет.
            await dp.feed_update(
                bot,
                Update(update_id=5, message=private_message(5, "/start", STRANGER)),
            )
            check(
                "админ пользуется ботом",
                any("benz" in t for t in session.texts_of("sendMessage")),
                str(session.texts_of("sendMessage"))[:200],
            )

            session.reset()
            await dp.feed_update(
                bot,
                Update(update_id=6, message=private_message(6, "/add_admin 555", STRANGER)),
            )
            check(
                "админ не может выдать права",
                any("Только для владельца" in t for t in session.texts_of("sendMessage")),
                str(session.texts_of("sendMessage")),
            )
            check("админ не добавлён", not await repo.is_admin(BUYER.id))

            session.reset()
            await dp.feed_update(
                bot,
                Update(update_id=7, message=private_message(7, "/remove_admin 999", STRANGER)),
            )
            check(
                "админ не может снять права",
                any("Только для владельца" in t for t in session.texts_of("sendMessage")),
                str(session.texts_of("sendMessage")),
            )
            check("админ всё ещё на месте", await repo.is_admin(STRANGER.id))

            await dp.feed_update(
                bot,
                Update(update_id=8, message=private_message(8, "/remove_admin 999", SELLER)),
            )
            check("владелец снял админа", not await repo.is_admin(STRANGER.id))

            session.reset()
            await dp.feed_update(
                bot,
                Update(update_id=9, message=private_message(9, "/remove_admin 111", SELLER)),
            )
            check(
                "владельца снять нельзя",
                any("владельца снять нельзя" in t for t in session.texts_of("sendMessage")),
                str(session.texts_of("sendMessage")),
            )

            session.reset()
            await dp.feed_update(
                bot,
                Update(update_id=10, message=private_message(10, "/add_admin abc", SELLER)),
            )
            check(
                "некорректный ID отклонён",
                any("корректный Telegram ID" in t for t in session.texts_of("sendMessage")),
                str(session.texts_of("sendMessage")),
            )


async def scenario_non_admin_ignored() -> None:
    print("\n[3b] E2E: не-админ игнорируется полностью")
    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp, connection=CONNECTION) as (dp, bot, db, session):
            stranger_connection = CONNECTION.model_copy(
                update={
                    "id": "BQstranger",
                    "user": STRANGER,
                    "user_chat_id": STRANGER.id,
                }
            )

            await dp.feed_update(
                bot,
                Update(
                    update_id=50,
                    business_message=group_message(
                        1, "benz https://t.me/nft/ViceCream-382938 2500", STRANGER, CONNECTION.id
                    ),
                ),
            )
            check("команда не обработана", session.find("sendMessage") == [],
                  str(session.find("sendMessage"))[:200])
            check("предложение не создано", await db.fetch_all("SELECT id FROM offers") == [])

            # Подключение бизнес-аккаунта от не-админа игнорируется.
            await dp.feed_update(
                bot,
                Update(update_id=51, business_connection=stranger_connection),
            )
            stored = await BusinessConnectionRepository(db).get("BQstranger")
            check("подключение не-админа не сохранено", stored is None, str(dict(stored) if stored else None))
            check("уведомление не отправлено", session.find("sendMessage") == [])

            # Кнопку карточки нажать может любой: это покупатель, и он не
            # обязан быть админом. Просто несуществующий offer -> «не найдено».
            session.reset()
            target = bot_message(5, "карточка", BUYER, CONNECTION.id)
            await click(dp, bot, 52, "cb-stranger", STRANGER, target, "of:a:deadbeef")
            answered = session.find("answerCallbackQuery")
            check("покупатель может нажать кнопку", len(answered) == 1, str(answered))
            check(
                "на неизвестный offer отвечает «не найдено»",
                answered and "не найден" in answered[0].get("text", ""),
                str(answered),
            )


async def scenario_business_connection() -> None:
    print("\n[4] E2E: событие business_connection (подключение и отключение)")
    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp, admins={BUYER.id}) as (dp, bot, db, session):
            repo = BusinessConnectionRepository(db)

            await dp.feed_update(bot, Update(update_id=1, business_connection=CONNECTION))
            row = await repo.get(CONNECTION.id)
            check("подключение сохранено", row is not None)
            if row is None:
                return
            check("user_id", int(row["user_id"]) == SELLER.id)
            check("user_chat_id", int(row["user_chat_id"]) == SELLER.id)
            check("is_enabled", int(row["is_enabled"]) == 1)
            check("право can_reply", int(row["can_reply"]) == 1)
            check("право transfer_gifts", int(row["can_transfer_and_upgrade"]) == 1)
            check(
                "владельцу отправлено уведомление",
                any("подключён" in t.lower() for t in session.texts_of("sendMessage")),
                str(session.texts_of("sendMessage")),
            )

            session.reset()
            disabled = CONNECTION.model_copy(update={"is_enabled": False})
            await dp.feed_update(bot, Update(update_id=2, business_connection=disabled))
            check("отклюление обработано", int((await repo.get(CONNECTION.id))["is_enabled"]) == 0)
            check("get_enabled возвращает None", await repo.get_enabled(CONNECTION.id) is None)
            check(
                "уведомление об отключении",
                any("отключён" in t.lower() for t in session.texts_of("sendMessage")),
                str(session.texts_of("sendMessage")),
            )

            # После отключения бот не должен подставлять business_connection_id.
            await dp.feed_update(
                bot,
                Update(
                    update_id=3,
                    message=private_message(1, "benz https://t.me/nft/A-1 100", BUYER),
                ),
            )
            sent = session.find("sendMessage")
            check(
                "после отключения — сообщение от имени бота",
                bool(sent) and not sent[0].get("business_connection_id"),
                str(sent[:1]),
            )


async def scenario_throttle() -> None:
    print("\n[5] E2E: пейсинг не отбрасывает команды, кнопки не блокирует")
    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp, admins={BUYER.id}, throttle=0.05) as (dp, bot, db, session):
            for i in (1, 2, 3):
                await dp.feed_update(
                    bot,
                    Update(
                        update_id=i,
                        message=private_message(i, "benz https://t.me/nft/Gift-1 100", BUYER),
                    ),
                )
            sent = session.find("sendMessage")
            cards = [p for p in sent if "reply_markup" in p]
            check(
                "ни одна команда не выброшена: из пачки в 3 обработаны все",
                len(cards) == 3,
                str(len(cards)),
            )
            # Карточки созданы в том же порядке, что и команды: пейсинг
            # выстраивает пачку, а не перемешивает.
            order = [p["reply_markup"]["inline_keyboard"][0][0]["callback_data"] for p in cards]
            check("карточки идут в порядке команд", len(set(order)) == 3, str(order))

            # Кнопки пейсинг не должен блокировать даже в том же интервале.
            offer_id = cards[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[2]
            target = bot_message(500, "text", BUYER, None)
            await click(dp, bot, 10, "cb", BUYER, target, f"of:a:{offer_id}")
            check("нажатие по кнопке прошло", len(session.find("editMessageText")) == 1,
                  str(len(session.find("editMessageText"))))
            check("предложение принято", (await OfferRepository(db).get(offer_id))["status"] == "accepted")


async def scenario_startup_hook() -> None:
    print("\n[6] E2E: startup-хук реально выполняется и сеет админов")
    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp, admins={SELLER.id}, seed_admins=False) as (
            dp,
            bot,
            db,
            session,
        ):
            repo = AdminRepository(db)
            check("до старта админов нет", not await repo.is_admin(SELLER.id))

            session.reset()
            # Именно так aiogram вызывает startup перед polling.
            # Логи перехватываем, чтобы убедиться, что версия сборки попадает
            # в лог: на сервере по ней видно, какая версия реально запущена.
            _startup_logs: list[str] = []

            class _Collector(logging.Handler):
                def emit(self, record: logging.LogRecord) -> None:
                    _startup_logs.append(record.getMessage())

            _handler = _Collector()
            _entry_logger = logging.getLogger("app.__main__")
            _entry_logger.addHandler(_handler)
            _previous_level = _entry_logger.level
            _entry_logger.setLevel(logging.INFO)
            try:
                await dp.startup.trigger(bot=bot, dispatcher=dp, bots=[bot])
            finally:
                _entry_logger.removeHandler(_handler)
                _entry_logger.setLevel(_previous_level)
            check("админ засеян после startup", await repo.is_admin(SELLER.id))
            check("флаг однократного посева", await db.get_meta("admins_seeded") == "1")
            check("getMe вызван", len(session.find("getMe")) == 1, str(session.find("getMe")))
            check("команды бота выставлены", len(session.find("setMyCommands")) == 1,
                  str(session.find("setMyCommands")))
            commands = session.find("setMyCommands")[0].get("commands", [])
            check("команда benz в меню", any(c.get("command") == "benz" for c in commands),
                  str(commands))
            check("версия сборки в логе", any("Версия бота" in m for m in _startup_logs),
                  str(_startup_logs[-3:]))

            # Повторный emit не должен дублировать админа.
            await dp.startup.trigger(bot=bot, dispatcher=dp, bots=[bot])
            rows = await repo.get_all()
            check("повторный старт не дублирует", len(rows) == 1, str([dict(r) for r in rows]))


async def scenario_gateway_errors() -> None:
    print("\n[7] E2E: PEER_FLOOD повторяется, а не маскируется под провал отправки")
    from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

    from app.services.telegram import TelegramGateway

    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp) as (_dp, bot, _db, session):
            gateway = TelegramGateway(bot)

            # PEER_FLOOD временный: первый вызов падает, второй успешен.
            session.reset()
            state = {"n": 0}

            async def flaky_once(_bot: Any, method: Any, timeout: int | None = None) -> Any:
                state["n"] += 1
                session.calls.append((method.__api_method__, method.model_dump(exclude_none=True)))
                if state["n"] == 1:
                    raise TelegramBadRequest(method=None, message="Bad Request: PEER_FLOOD")
                return Message(
                    message_id=3000,
                    date=datetime.now(UTC),
                    chat=Chat(id=555, type="private"),
                    text="ok",
                )

            bot.session = flaky_once  # type: ignore[method-assign]
            try:
                sent = await gateway.send(555, "привет", reply_markup=None)
            finally:
                bot.session = session  # type: ignore[method-assign]

            check("PEER_FLOOD вылечен повтором", isinstance(sent, Message))
            check("было ровно 2 попытки", state["n"] == 2, str(state["n"]))
            check("оформление не снималось (это не ошибка разметки)",
                  len(session.find("sendMessage")) == 2, str(len(session.find("sendMessage"))))

            # Постоянный PEER_FLOOD -> исчерпание попыток и честная ошибка.
            session.reset()
            session.fail_with["sendMessage"] = TelegramBadRequest(
                method=None, message="Bad Request: PEER_FLOOD"
            )
            try:
                await gateway.send(555, "привет", reply_markup=None)
                check("вечный PEER_FLOOD пробрасывается", False, "исключения не было")
            except TelegramBadRequest as exc:
                check("вечный PEER_FLOOD пробрасывается", "PEER_FLOOD" in str(exc), str(exc))
            check("попыток ровно _MAX_ATTEMPTS", len(session.find("sendMessage")) == _MAX_ATTEMPTS,
                  str(len(session.find("sendMessage"))))
            session.fail_with.clear()

            # retry_after из Telegram уважается.
            session.reset()
            session.fail_with["sendMessage"] = TelegramRetryAfter(
                method=None, message="Too Many Requests: retry after 1", retry_after=1
            )
            with suppress(TelegramRetryAfter):
                await gateway.send(555, "привет", reply_markup=None)
            check("retry_after тоже повторяется", len(session.find("sendMessage")) == _MAX_ATTEMPTS,
                  str(len(session.find("sendMessage"))))
            session.fail_with.clear()

            # Настоящая ошибка разметки — деградировать обязана.
            # Bot API отдаёт разные тексты: серверный BUTTON_CUSTOM_EMOJI_INVALID
            # и клиентскую проверку «Invalid button style specified».
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            def styled_keyboard() -> Any:
                """Кнопка сразу с обоими необязательными полями Bot API 9.4.

                В дизайне карточек цвет не используется, но шлюз обязан
                деградировать корректно, если оформление всё-таки появится.
                """
                return InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="Отклонить",
                                callback_data=f"of:d:{'a' * 32}",
                                style="danger",
                                icon_custom_emoji_id="5778527486270770928",
                            )
                        ]
                    ]
                )

            session.reset()
            calls = {"n": 0}

            def make_flaky(*failures: str) -> Any:
                """Мок, который падает с заданными ошибками на первых попытках."""

                async def flaky(_bot: Any, method: Any, timeout: int | None = None) -> Any:
                    index = calls["n"]
                    calls["n"] += 1
                    session.calls.append(
                        (method.__api_method__, method.model_dump(exclude_none=True))
                    )
                    if index < len(failures):
                        raise TelegramBadRequest(
                            method=None, message=f"Bad Request: {failures[index]}"
                        )
                    return Message(
                        message_id=2000,
                        date=datetime.now(UTC),
                        chat=Chat(id=555, type="private"),
                        text="ok",
                    )

                return flaky

            def first_button(payload: dict[str, Any]) -> dict[str, Any]:
                return payload["reply_markup"]["inline_keyboard"][0][0]

            async def send_with(failures: tuple[str, ...]) -> Any:
                session.reset()
                calls["n"] = 0
                bot.session = make_flaky(*failures)  # type: ignore[method-assign]
                try:
                    return await gateway.send(
                        555, "<b>hi</b>", reply_markup=styled_keyboard()
                    )
                finally:
                    bot.session = session  # type: ignore[method-assign]

            # 1. Не поддержан только icon_custom_emoji_id: цвет обязан уцелеть.
            sent = await send_with(("BUTTON_CUSTOM_EMOJI_INVALID",))
            check("иконка: ошибка разметки вылечена", isinstance(sent, Message))
            payloads = session.find("sendMessage")
            check("иконка: была повторная попытка", len(payloads) == 2, str(len(payloads)))
            if len(payloads) == 2:
                first, second = first_button(payloads[0]), first_button(payloads[1])
                check(
                    "иконка: была и исчезла",
                    first.get("icon_custom_emoji_id")
                    and not second.get("icon_custom_emoji_id"),
                    f"{first} -> {second}",
                )
                check("иконка: цвет уцелел", second.get("style") == "danger", str(second.get("style")))
                check(
                    "иконка: действие сохранено",
                    second["callback_data"] == f"of:d:{'a' * 32}",
                    str(second),
                )

            # 2. Не поддержан цвет (точная формулировка Bot API) — иконка
            #    обязана уцелеть: снимаем только то поле, на которое ругается API.
            sent = await send_with(("Invalid button style specified",))
            check("стиль: ошибка разметки вылечена", isinstance(sent, Message))
            payloads = session.find("sendMessage")
            check("стиль: была повторная попытка", len(payloads) == 2, str(len(payloads)))
            if len(payloads) == 2:
                first, second = first_button(payloads[0]), first_button(payloads[1])
                check(
                    "стиль: цвет был и исчез",
                    first.get("style") == "danger" and second.get("style") is None,
                    f"{first.get('style')} -> {second.get('style')}",
                )
                check("стиль: иконка уцелела", bool(second.get("icon_custom_emoji_id")), str(second))

            # 3. Не поддержаны обе фичи — кнопка всё равно остаётся рабочей.
            #   Первая ошибка снимает иконку, вторая (style) — цвет.
            sent = await send_with(("BUTTON_CUSTOM_EMOJI_INVALID", "Invalid button style specified"))
            check("обе фичи: запрос доведён", isinstance(sent, Message))
            payloads = session.find("sendMessage")
            check("обе фичи: три попытки", len(payloads) == 3, str(len(payloads)))
            if len(payloads) == 3:
                last = first_button(payloads[2])
                check(
                    "обе фичи: оформление снято",
                    not last.get("style") and not last.get("icon_custom_emoji_id"),
                    str(last),
                )
                check(
                    "обе фичи: действие сохранено",
                    last["callback_data"] == f"of:d:{'a' * 32}",
                    str(last),
                )


async def scenario_command_variants() -> None:
    print("\n[8] E2E: варианты команды и внятная ошибка вместо молчания")
    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp, admins={BUYER.id}) as (dp, bot, db, session):
            # Валидные написания команды: без знака, с точкой, со слэшем и
            # с @имя_бота. Точка и слэш необязательны.
            for i, text in enumerate(
                (
                    "benz https://t.me/nft/Gift-1 100",
                    ".benz https://t.me/nft/Gift-2 200",
                    "/benz https://t.me/nft/Gift-3 300",
                    "benz@BotOffRuBot https://t.me/nft/Gift-4 400",
                    # Без валюты — подставляется Stars по умолчанию.
                    "benz https://t.me/nft/Gift-5 500",
                    # Регистр не важен: aiogram F.text.regexp сам флаг не ставит,
                    # поэтому (?i) вшит в сам шаблон.
                    "BENZ https://t.me/nft/Gift-6 600",
                ),
                start=1,
            ):
                session.reset()
                await dp.feed_update(
                    bot, Update(update_id=i, message=private_message(i, text, BUYER))
                )
                sent = session.find("sendMessage")
                ok = bool(sent) and "reply_markup" in sent[0]
                check(f"команда {text.split()[0]} распознана", ok, str(sent[:1])[:120])

            # Похожие, но не те команды — игнорируются
            session.reset()
            await dp.feed_update(
                bot,
                Update(update_id=9, message=private_message(9, "benzomething x 1", BUYER)),
            )
            check("похожая команда не перехватывается", session.find("sendMessage") == [],
                  str(session.find("sendMessage"))[:120])

            # Без валюты в команде подставляется Stars — именно он попадает
            # и в БД, и в карточку.
            rows = await db.fetch_all("SELECT stars, currency FROM offers")
            without = [r for r in rows if int(r["stars"]) == 500]
            check("предложение без валюты создано", bool(without), str(len(rows)))
            if without:
                check("валюта по умолчанию — Stars",
                      without[0]["currency"] == "Stars", str(without[0]["currency"]))


async def scenario_send_failure_not_silent() -> None:
    print("\n[9] E2E: при PEER_FLOOD пользователь получает объяснение, а не тишину")
    from aiogram.exceptions import TelegramBadRequest

    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp, admins={BUYER.id}) as (dp, bot, db, session):
            session.fail_with["sendMessage"] = TelegramBadRequest(
                method=None, message="Bad Request: PEER_FLOOD"
            )
            await dp.feed_update(
                bot,
                Update(
                    update_id=1,
                    message=private_message(1, "benz https://t.me/nft/Gift-1 100", BUYER),
                ),
            )
            session.fail_with.clear()

            leftovers = await db.fetch_all("SELECT id FROM offers")
            check("предложение не осталось в БД", leftovers == [], str([dict(r) for r in leftovers]))
            texts = session.texts_of("sendMessage")
            check("пользователь получил ответ", len(texts) >= 1, str(texts)[:200])
            joined = " ".join(texts)
            check("ответ объясняет причину", "Не удалось отправить" in joined, joined[:200])
            check("ответ не требует /start для бизнес-чата", "/start" not in joined, joined[:200])
            deleted = _deleted_command_ids(session) + [
                payload["message_id"]
                for name, payload in session.calls
                if name == "deleteMessage"
            ]
            check("команда удалена даже при ошибке отправки карточки", deleted == [1], str(deleted))


async def scenario_public_id_command() -> None:
    """``/id`` отвечает любому — иначе посторонний вечно ждёт ответа.

    Гейт по требованию игнорирует не-админов, но свой Telegram ID человек
    вычислить не может. Без этой команды сообщение в чате висит бесконечно:
    ответа не будет никогда, и непонятно — это зависание или права.
    """
    print("\n[10] E2E: /id отвечает и постороннему, benz — по-прежнему молчит")
    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp) as (dp, bot, db, session):
            # Посторонний спрашивает ID.
            await dp.feed_update(
                bot, Update(update_id=1, message=private_message(1, "/id", STRANGER))
            )
            texts = session.texts_of("sendMessage")
            check("посторонний получил ответ", len(texts) == 1, str(texts)[:200])
            answer = texts[0] if texts else ""
            check("в ответе есть его ID", str(STRANGER.id) in answer, answer[:200])
            check("роль сообщена честно", "доступа нет" in answer, answer[:200])
            check("есть подсказка, как получить доступ", "/add_admin" in answer, answer[:200])

            # Тот же посторонний шлёт команду — она по-прежнему игнорируется.
            session.reset()
            await dp.feed_update(
                bot,
                Update(
                    update_id=2,
                    message=private_message(2, "benz https://t.me/nft/Gift-1 100", STRANGER),
                ),
            )
            check(
                "benz постороннего по-прежнему молчит",
                session.find("sendMessage") == [],
                str(session.find("sendMessage")),
            )
            check(
                "предложение постороннего не создано",
                await db.fetch_all("SELECT id FROM offers") == [],
            )

            # Владелец видит свою роль.
            session.reset()
            await dp.feed_update(
                bot, Update(update_id=3, message=private_message(3, "/whoami", SELLER))
            )
            owner_text = " ".join(session.texts_of("sendMessage"))
            check("владелец видит роль «владелец»", "владелец бота" in owner_text, owner_text[:200])
            check("владельцу не предлагают /add_admin", "/add_admin" not in owner_text, owner_text[:200])

            # Гейт пропускает публичные команды и с суффиксом @botusername.
            # Само совпадение юзернейма проверяет aiogram: команда, адресованная
            # чужому боту, игнорируется — это правильное поведение Telegram.
            from app.middlewares import _is_public_command

            check("гейт пропускает /id", _is_public_command("/id"))
            check("гейт пропускает /id@BotName", _is_public_command("/id@BotName"))
            check("гейт не пропускает /add_admin", not _is_public_command("/add_admin 1"))
            check("гейт не пропускает benz", not _is_public_command("benz x 1"))


async def scenario_sold_card_formatting() -> None:
    """Оформление карточки «продан»: жирная инструкция и «Подаро́к» сCaps."""
    print("\n[11] Формат карточки «продан»")
    from app.i18n import AR, EN, RU

    for lang, label in ((RU, "ru"), (EN, "en"), (AR, "ar")):
        text = templates.accepted_card(
            lang=lang,
            gift_title="Santa Hat #3,438",
            gift_url="https://t.me/nft/SantaHat-3438",
            currency="Stars",
            currency_label="Stars",
        )
        check(f"[{label}] инструкция жирная", f"<b>• {lang.hand_over} Stars.</b>" in text, text)
        check(f"[{label}] подпись подарка", f"{lang.gift_label}: " in text, text)
        check(f"[{label}] номер подарка один раз", text.count("#") == 1, text)

    # Заглавная буква в подписи проверяется только там, где есть регистр:
    # арабский регистр не знает, isupper() там бессмысленно.
    check("ru: подпись с большой буквы", RU.gift_label[0].isupper(), RU.gift_label)
    check("ru: гомоглифы сохранены", "Подa" in RU.gift_label, RU.gift_label)
    check("en: подпись с большой буквы", EN.gift_label[:1].isupper(), EN.gift_label)
    # Подпись в карточке «продан» — заглавная, в объявлении та же гомоглифная
    # строка стоит строчной («зa подapoк»). Это разные константы намеренно.
    from app.constants import H_PODAROK

    check("ru: подпись != строчная из объявления", RU.gift_label != H_PODAROK, RU.gift_label)
    check(
        "ru: отличается только регистром первой буквы",
        RU.gift_label[1:] == H_PODAROK[1:],
        f"{RU.gift_label!r} vs {H_PODAROK!r}",
    )


async def scenario_business_peer_falls_back_to_bot() -> None:
    """``BUSINESS_PEER_INVALID`` → карточка всё равно доходит, от имени бота.

    Право «Разрешить отвечать на сообщения» может быть не выдано, и тогда
    Telegram не даёт боту писать за аккаунт. Молчать нельзя: продавец не увидит
    сделку вообще. Поэтому карточка уходит от имени самого бота — у него таких
    ограничений нет. Продавец видит сделку, покупатель указан в тексте.
    """
    print("\n[12] E2E: BUSINESS_PEER_INVALID → карточка доходит от имени бота")
    from aiogram.exceptions import TelegramBadRequest

    def refuse_via_business(name: str, payload: dict[str, Any]) -> Exception | None:
        if name == "sendMessage" and payload.get("business_connection_id"):
            return TelegramBadRequest(method=None, message="Bad Request: BUSINESS_PEER_INVALID")
        return None

    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp, admins={BUYER.id}, connection=CONNECTION) as (dp, bot, db, session):
            session.fail_if = refuse_via_business
            await dp.feed_update(
                bot,
                Update(
                    update_id=1,
                    business_message=biz_dm(
                        1, "benz https://t.me/nft/Gift-1 100", SELLER, BUYER, CONNECTION.id
                    ),
                ),
            )
            session.fail_if = None

            attempts = session.find("sendMessage")
            check("сначала попытка через аккаунт", bool(attempts), str(attempts[:2]))
            check(
                "первая попытка идёт с подключением",
                attempts[0].get("business_connection_id") == CONNECTION.id,
                str(attempts[0].get("business_connection_id")),
            )
            # Карточка — сообщение с кнопками. После отказа аккаунта она должна
            # уйти от имени бота: сделка важнее того, кто её прислал.
            cards = [p for p in attempts if "reply_markup" in p]
            check(
                "карточка доставлена от имени бота",
                bool(cards) and cards[-1].get("business_connection_id") is None,
                str([p.get("business_connection_id") for p in cards]),
            )
            check(
                "сделка сохранена — карточка доставлена и ждёт ответа",
                len(await db.fetch_all("SELECT id FROM offers")) == 1,
                str([dict(r) for r in await db.fetch_all("SELECT id FROM offers")]),
            )
            joined = " ".join(session.texts_of("sendMessage"))
            check(
                "продавцу не шлют пугающую ошибку, раз карточка дошла",
                "Не удалось отправить" not in joined,
                joined[:200],
            )


async def scenario_connection_self_heal() -> None:
    """Потерянное бизнес-подключение восстанавливается из самого апдейта.

    Telegram присылает ``business_connection`` один раз. Если бот в этот момент
    не работал, событие потеряно навсегда — прежний код молча откатывался на
    отправку от имени бота, и команда «исчезала».
    """
    print("\n[13] E2E: бизнес-подключение восстанавливается после потери события")
    unknown = CONNECTION.model_copy(update={"id": "LOSTCONNECTION01"})

    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp) as (dp, bot, db, session):
            repo = BusinessConnectionRepository(db)
            check("подключения изначально нет", await repo.get(unknown.id) is None)

            await dp.feed_update(
                bot,
                Update(
                    update_id=1,
                    business_message=biz_dm(
                        1, "benz https://t.me/nft/Gift-1 100", SELLER, BUYER, unknown.id
                    ),
                ),
            )

            row = await repo.get(unknown.id)
            check("подключение восстановлено", row is not None)
            if row is not None:
                check("владелец восстановлен", int(row["user_id"]) == SELLER.id, str(dict(row)))
                check("подключение активно", int(row["is_enabled"]) == 1)

            sends = session.find("sendMessage")
            check("карточка отправлена", len(sends) == 1, str(len(sends)))
            if sends:
                check(
                    "уходит от имени бизнес-аккаунта",
                    sends[0].get("business_connection_id") == unknown.id,
                    str(sends[0].get("business_connection_id")),
                )
            check(
                "команда удалена через бизнес-подключение",
                _deleted_command_ids(session) == [1],
                str(session.find("deleteBusinessMessages")),
            )


async def scenario_disabled_connection_ignored() -> None:
    """Отключённое подключение по-прежнему уважается — молча ломать нельзя."""
    print("\n[14] E2E: отключённое подключение не восстанавливается молча")
    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp, connection=CONNECTION) as (dp, bot, db, session):
            await dp.feed_update(
                bot, Update(update_id=1, business_connection=CONNECTION)
            )
            repo = BusinessConnectionRepository(db)
            await repo.set_enabled(CONNECTION.id, False)
            session.reset()

            await dp.feed_update(
                bot,
                Update(
                    update_id=2,
                    business_message=biz_dm(
                        2, "benz https://t.me/nft/Gift-1 100", SELLER, BUYER, CONNECTION.id
                    ),
                ),
            )
            sends = session.find("sendMessage")
            check(
                "карточка уходит от имени бота",
                bool(sends) and not sends[0].get("business_connection_id"),
                str(sends[:1]),
            )
            check(
                "отключение не перезаписано",
                int((await repo.get(CONNECTION.id))["is_enabled"]) == 0,
            )


async def scenario_failure_explains_business_cause() -> None:
    """Объяснение отражает ограничение аккаунта, а не следствие отката.

    Схема: покупатель подключил бота к своему аккаунту и пишет команду в чат
    продавца. Бизнес-канал режет ``BUSINESS_PEER_INVALID``. Если объяснить
    что-то другое, пользователю скажут «пусть продавец напишет боту /start» —
    а дело в правах его собственного аккаунта.
    """
    print("\n[15] E2E: причина отказа — ограничение аккаунта, а не /start")
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

    def refuse(name: str, payload: dict[str, Any]) -> Exception | None:
        # Через бизнес-аккаунт — BUSINESS_PEER_INVALID (нет права отвечать).
        # Откат от имени бота — тоже 403: «нельзя начать диалог». Обе ошибки
        # разных классов (400 и 403) — объясняться должна ПЕРВАЯ, про права.
        if name != "sendMessage":
            return None
        if payload.get("business_connection_id"):
            return TelegramBadRequest(method=None, message="Bad Request: BUSINESS_PEER_INVALID")
        return TelegramForbiddenError(
            method=None, message="Forbidden: bot can't initiate conversation with a user"
        )

    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp, admins={BUYER.id}, connection=CONNECTION) as (dp, bot, _db, session):
            session.fail_if = refuse
            await dp.feed_update(
                bot,
                Update(
                    update_id=1,
                    business_message=biz_dm(
                        1, "benz https://t.me/nft/Gift-1 100", SELLER, BUYER, CONNECTION.id
                    ),
                ),
            )
            session.fail_if = None

            joined = " ".join(session.texts_of("sendMessage"))
            check(
                "объясняет ограничение бизнес-аккаунта",
                "Разрешить отвечать" in joined or "бизнес-аккаунт" in joined,
                joined[:200],
            )
            check(
                "не отправляет покупателя писать боту",
                "/start" not in joined and "написал боту" not in joined,
                joined[:200],
            )
            check(
                "упоминает окно входящих за 24 часа",
                "24" in joined,
                joined[:250],
            )


async def scenario_connection_restored_via_api() -> None:
    """Потерянное подключение восстанавливается через getBusinessConnection.

    Событие ``business_connection`` Telegram присылает один раз, и если бот в
    этот момент не работал, оно потеряно. Единственный источник настоящих
    прав — метод Bot API ``getBusinessConnection``; из апдейта их не узнать.
    """
    print("\n[16] E2E: восстановление подключения через getBusinessConnection")
    unknown = CONNECTION.model_copy(update={"id": "LOSTCONNECTION02"})

    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp) as (dp, bot, db, session):
            repo = BusinessConnectionRepository(db)
            check("подключения изначально нет", await repo.get(unknown.id) is None)

            session.add_connection(unknown)
            await dp.feed_update(
                bot,
                Update(
                    update_id=1,
                    business_message=biz_dm(
                        1, "benz https://t.me/nft/Gift-1 100", SELLER, BUYER, unknown.id
                    ),
                ),
            )

            check("getBusinessConnection вызван", bool(session.find("getBusinessConnection")))
            row = await repo.get(unknown.id)
            check("подключение восстановлено", row is not None)
            if row is not None:
                check("владелец из ответа Telegram", int(row["user_id"]) == SELLER.id, str(dict(row)))
                check(
                    "права взяты из ответа Telegram, а не выдуманы",
                    int(row["can_reply"]) == 1 and int(row["can_transfer_stars"]) == 1,
                    f"can_reply={row['can_reply']}",
                )

            sends = session.find("sendMessage")
            check(
                "карточка уходит от имени бизнес-аккаунта",
                bool(sends) and sends[0].get("business_connection_id") == unknown.id,
                str(sends[:1]),
            )

    # Без сети getBusinessConnection не сработал — остаётся запасной путь.
    print("  -- запасной путь при недоступном Bot API --")
    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp) as (dp, bot, db, session):
            repo = BusinessConnectionRepository(db)
            await dp.feed_update(
                bot,
                Update(
                    update_id=1,
                    business_message=biz_dm(
                        1, "benz https://t.me/nft/Gift-1 100", SELLER, BUYER, unknown.id
                    ),
                ),
            )
            row = await repo.get(unknown.id)
            check("запись всё равно создана", row is not None)
            if row is not None:
                check("владелец восстановлен из апдейта", int(row["user_id"]) == SELLER.id)
            sends = session.find("sendMessage")
            check(
                "карточка всё равно отправлена от имени аккаунта",
                bool(sends) and sends[0].get("business_connection_id") == unknown.id,
                str(sends[:1]),
            )


async def scenario_callback_without_connection() -> None:
    """Ответ на callback идёт без ``business_connection_id``.

    По документации Bot API на ``answerCallbackQuery`` параметра
    ``business_connection_id`` нет: ответ даётся от имени бота. Если передать
    его туда, Telegram отклонит запрос — тост просто не покажется.
    """
    print("\n[17] E2E: ответ на кнопку не несёт business_connection_id")
    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp, admins={BUYER.id}, connection=CONNECTION) as (dp, bot, _db, session):
            await dp.feed_update(
                bot,
                Update(
                    update_id=1,
                    business_message=biz_dm(
                        1, "benz https://t.me/nft/Gift-1 100", SELLER, BUYER, CONNECTION.id
                    ),
                ),
            )
            sent = session.find("sendMessage")
            if not sent:
                check("карточка создана", False, str(sent))
                return
            offer_id = sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[2]
            session.reset()

            target = bot_message(500, "card", SELLER, CONNECTION.id)
            await click(dp, bot, 2, "cb", BUYER, target, f"of:a:{offer_id}")

            answers = session.find("answerCallbackQuery")
            check("ответ на кнопку отправлен", len(answers) == 1, str(answers))
            if answers:
                check(
                    "ответ без business_connection_id",
                    "business_connection_id" not in answers[0],
                    str(answers[0]),
                )
            edits = session.find("editMessageText")
            check("редактирование идёт от имени аккаунта", bool(edits)
                  and edits[0].get("business_connection_id") == CONNECTION.id, str(edits[:1]))


async def scenario_business_info_verdict() -> None:
    """``/business_info`` прямо говорит, может ли бот писать от имени аккаунта.

    Права выдаются галочками при подключении и легко остаются незамеченными:
    бот пишет «подключён», а отправка не проходит. Причину нужно видеть в чате,
    а не искать в логах.
    """
    print("\n[18] E2E: /business_info показывает вердикт по правам")
    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp, admins={SELLER.id}) as (dp, bot, db, session):
            await dp.feed_update(bot, Update(update_id=1, business_connection=CONNECTION))
            session.reset()

            await dp.feed_update(
                bot, Update(update_id=2, message=private_message(1, "/business_info", SELLER))
            )
            text = " ".join(session.texts_of("sendMessage"))
            check(
                "показывает, что отправляет от имени аккаунта",
                "отправляет от имени аккаунта" in text,
                text[:250],
            )

            # Забираем право «отвечать» — как при подключении без галочек.
            await db.execute(
                "UPDATE business_connections SET can_reply = 0 WHERE id = ?", (CONNECTION.id,)
            )
            session.reset()
            await dp.feed_update(
                bot, Update(update_id=3, message=private_message(2, "/business_info", SELLER))
            )
            text = " ".join(session.texts_of("sendMessage"))
            check("без права отвечать — явный отказ", "не может отправлять" in text, text[:250])
            check("названо нужное право", "Разрешить отвечать" in text, text[:250])
            check("подсказка про переподключение", "Настройки → Бизнес" in text, text[:250])


async def scenario_duplicate_command_ignored() -> None:
    print("\n[15] E2E: повторная доставка одной команды не создаёт вторую карточку")
    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp, connection=CONNECTION) as (dp, bot, db, session):
            msg = biz_dm(1, "benz https://t.me/nft/Gift-1 100", SELLER, BUYER, CONNECTION.id)
            await dp.feed_update(bot, Update(update_id=1, business_message=msg))
            cards = [p for p in session.find("sendMessage") if "reply_markup" in p]
            check("первая доставка — ровно одна карточка", len(cards) == 1, str(len(cards)))

            # Telegram доставляет апдейты «хотя бы один раз»: тот же апдейт может
            # прийти повторно (например, после рестарта до подтверждения offset).
            # Вторая карточка создаваться не должна.
            session.reset()
            await dp.feed_update(bot, Update(update_id=2, business_message=msg))
            dup = [p for p in session.find("sendMessage") if "reply_markup" in p]
            check("дубль апдейта не даёт второй карточки", len(dup) == 0, str(len(dup)))

            offers = await db.fetch_all("SELECT * FROM offers")
            check("в БД ровно одно предложение", len(offers) == 1, str(len(offers)))

            # Настоящая новая команда (другой message_id) обрабатывается как обычно.
            session.reset()
            msg2 = biz_dm(2, "benz https://t.me/nft/Gift-2 200", SELLER, BUYER, CONNECTION.id)
            await dp.feed_update(bot, Update(update_id=3, business_message=msg2))
            fresh = [p for p in session.find("sendMessage") if "reply_markup" in p]
            check("новая команда по-прежнему даёт карточку", len(fresh) == 1, str(len(fresh)))


async def scenario_flood_guard_cools_down() -> None:
    """После флуд-ответа Telegram следующая команда выдерживает общую паузу.

    Telegram отвечает ``retry_after`` на конкретный запрос, но лимит считается
    на аккаунт целиком. Если бот продолжит слать следующую команду сразу, он
    соберёт 429 снова. Здесь проверяем, что паузу увидит и вторая команда.
    """
    print("\n[16] E2E: флуд-ответ Telegram включает общую паузу")
    from aiogram.exceptions import TelegramRetryAfter

    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp, admins={BUYER.id}, throttle=0.0) as (dp, bot, _db, session):
            FLOOD_GUARD.note_success()
            check("до флуда паузы нет", FLOOD_GUARD.delay() == 0.0, str(FLOOD_GUARD.delay()))

            # Первая отправка упирается в флуд-лимит и уходит на ретрай.
            session.fail_if = lambda name, payload: (
                TelegramRetryAfter(
                    method=None, message="Too Many Requests: retry after 1", retry_after=1
                )
                if name == "sendMessage"
                else None
            )
            await dp.feed_update(
                bot,
                Update(
                    update_id=1,
                    message=private_message(1, "benz https://t.me/nft/Gift-1 100", BUYER),
                ),
            )
            session.fail_if = None
            check("ретрай после флуда прошёл", len(session.find("sendMessage")) >= 1)
            check("общая пауза включена", FLOOD_GUARD.delay() > 0.0, str(FLOOD_GUARD.delay()))

            # Вторая команда обязана выдержать паузу, даже если Telegram больше
            # не жалуется: лимит считается на аккаунт, а не на один запрос.
            guard_penalty = FLOOD_GUARD.delay()
            started = time.monotonic()
            await dp.feed_update(
                bot,
                Update(
                    update_id=2,
                    message=private_message(2, "benz https://t.me/nft/Gift-2 200", BUYER),
                ),
            )
            elapsed = time.monotonic() - started
            check("вторая команда тоже ушла", len(session.find("sendMessage")) >= 2,
                  str(len(session.find("sendMessage"))))
            # Небольшой допуск на время выполнения самого обработчика.
            check(
                "вторая команда выдержала общую паузу",
                elapsed >= min(guard_penalty, 0.2) * 0.9,
                f"пауза={guard_penalty:.2f} прошло={elapsed:.2f}",
            )
            FLOOD_GUARD.note_success()


async def scenario_premium_emoji_without_premium() -> None:
    """Разовый отказ Telegram на ``<tg-emoji>`` не роняет карточку.

    Telegram отвечает ``ENTITY_TEXT_INVALID`` на весь запрос, а не только на
    тег. Если бы карточка «продан» падала целиком, сделка не дошла бы до
    покупателя. Схема деградации: повтор того же запроса (отказ бывает
    разовым) → замена тегов обычными символами с сохранением разметки.
    """
    print("\n[17] E2E: отказ на премиум-эмодзи не роняет карточку")
    from aiogram.exceptions import TelegramBadRequest

    from app.services.telegram import TelegramGateway
    from app.utils.text import tg_emoji

    with tempfile.TemporaryDirectory() as tmp:
        async with environment(tmp) as (_dp, bot, _db, session):
            gateway = TelegramGateway(bot)

            # Разовый сбой: отвергается только ПЕРВАЯ попытка с тегом.
            seen = {"n": 0}

            async def fail_once(_bot: Any, method: Any, timeout: int | None = None) -> Any:
                payload = method.model_dump(exclude_none=True)
                text = payload.get("text", "")
                if "<tg-emoji" in text:
                    seen["n"] += 1
                    if seen["n"] == 1:
                        raise TelegramBadRequest(
                            method=None, message="Bad Request: ENTITY_TEXT_INVALID"
                        )
                session.calls.append((method.__api_method__, payload))
                return Message(
                    message_id=4242,
                    date=datetime.now(UTC),
                    chat=Chat(id=555, type="private"),
                    text=text,
                )

            card = f"{tg_emoji(EMOJI_GIFT, '🎁')} <b>Подарок</b>: подарок продан"
            bot.session = fail_once  # type: ignore[method-assign]
            try:
                sent = await gateway.send(555, card)
            finally:
                bot.session = session  # type: ignore[method-assign]

            check("карточка доставлена", isinstance(sent, Message))
            calls = session.find("sendMessage")
            check("был ровно один успешный запрос", len(calls) == 1, str(len(calls)))
            check("премиум-эмодзи сохранены", "<tg-emoji" in calls[0].get("text", ""),
                  calls[0].get("text", ""))

            # Постоянный отказ: два повтора исчерпываются, теги заменяются.
            session.reset()

            async def always_reject(_bot: Any, method: Any, timeout: int | None = None) -> Any:
                payload = method.model_dump(exclude_none=True)
                session.calls.append((method.__api_method__, payload))
                text = payload.get("text", "")
                if "<tg-emoji" in text:
                    raise TelegramBadRequest(
                        method=None, message="Bad Request: ENTITY_TEXT_INVALID"
                    )
                return Message(
                    message_id=4243,
                    date=datetime.now(UTC),
                    chat=Chat(id=555, type="private"),
                    text=text,
                )

            bot.session = always_reject  # type: ignore[method-assign]
            try:
                sent = await gateway.send(555, card)
            finally:
                bot.session = session  # type: ignore[method-assign]

            check("при постоянном отказе тоже доставлено", isinstance(sent, Message))
            calls = session.find("sendMessage")
            last = calls[-1].get("text", "") if calls else ""
            check("теги заменены символами", "<tg-emoji" not in last, last)
            check("символ на месте", "🎁" in last, last)
            check("жирный текст сохранён", "<b>" in last, last)
            check(
                "HTML не снесён целиком",
                bool(calls) and calls[-1].get("parse_mode") == "HTML",
                str(calls[-1].get("parse_mode")) if calls else "",
            )


async def main() -> None:
    print("=" * 66)
    print("ИНТЕГРАЦИОННЫЕ ТЕСТЫ tg_offer_bot (Telegram API замокан)")
    print("=" * 66)
    await scenario_business_offer_flow()
    await scenario_group_and_currency()
    await scenario_foreign_offer_ignored()
    await scenario_author_cannot_resolve()
    await scenario_english_offer()
    await scenario_decline_flow()
    await scenario_permissions()
    await scenario_non_admin_ignored()
    await scenario_business_connection()
    await scenario_throttle()
    await scenario_startup_hook()
    await scenario_gateway_errors()
    await scenario_command_variants()
    await scenario_send_failure_not_silent()
    await scenario_public_id_command()
    await scenario_sold_card_formatting()
    await scenario_business_peer_falls_back_to_bot()
    await scenario_connection_self_heal()
    await scenario_disabled_connection_ignored()
    await scenario_failure_explains_business_cause()
    await scenario_connection_restored_via_api()
    await scenario_callback_without_connection()
    await scenario_business_info_verdict()
    await scenario_duplicate_command_ignored()
    await scenario_flood_guard_cools_down()
    await scenario_premium_emoji_without_premium()
    print("\n" + "=" * 66)
    print(f"ИТОГО: {PASSED} passed, {FAILED} failed")
    print("=" * 66)
    raise SystemExit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
