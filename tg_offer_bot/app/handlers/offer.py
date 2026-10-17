"""Команда ``benz`` и обработка inline-кнопок предложения.

Формат команды
--------------
``benz <ссылка на NFT-подарок> <цена> <валюта> [@username] [язык]``

* валюта — ``Stars`` (по умолчанию), ``Gram``, ``TON``, ``USDT``;
* ``@username`` — необязательный получатель подарка для кнопки «Передать»;
* язык — ``EU``/``en`` (английский), ``ar`` (арабский), ``ru`` (русский).
  Без суффикса берётся ``DEFAULT_LANGUAGE``.

Язык сохраняется в предложении, поэтому карточка «продан», тосты и
уведомление продавцу всегда на том же языке, на котором была карточка.

Команда работает в любом чате: личном диалоге, группе, супергруппе — везде,
где бот подключён к бизнес-аккаунту.

Логика владения предложением
----------------------------
* ``seller_*`` — автор команды (``message.from_user``);
* ``buyer_user_id`` — адресат карточки. В личном чате это ``message.chat.id``:
  автор пишет из аккаунта, подключённого к боту, поэтому карточка адресована
  собеседнику, и именно он жмёт «Принять». В группах адресата заранее
  не известно, поэтому ``buyer_user_id = 0`` и кнопку может нажать любой
  участник чата (см. ``ACCEPT_POLICY``).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta

from aiogram import F, Router
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

from app.config import Settings
from app.constants import (
    CALLBACK_PREFIX,
    CB_ACCEPT,
    CB_DECLINE,
    STATUS_ACCEPTED,
    STATUS_DECLINED,
    STATUS_PENDING,
)
from app.database import BusinessConnectionRepository, Database, OfferRepository, utcnow
from app.i18n import Lang, get_lang
from app.keyboards import inline
from app.services import templates
from app.services.parser import OfferParseError, parse_gift_link, parse_stars, split_offer_args
from app.services.telegram import (
    _BUSINESS_PEER_MARKERS,
    BusinessFallbackFailed,
    TelegramGateway,
)

logger = logging.getLogger(__name__)

# Команда пишется без точки: ``benz <ссылка> <цена> [валюта]``.
# Точка и слэш остались необязательными (``benz``, ``.benz``, ``/benz``) — так
# привычнее тем, кто раньше набирал команду со знаком. В т.ч. с @имя_бота.
# ``\b`` не даёт сработать на «benzsomething».
#
# Флаг ``(?i)`` встроен в шаблон, а не в регистрацию роутера: aiogram
# ``F.text.regexp`` компилирует выражение сам и регистр не игнорирует, а
# «Benz …» в чате набирают часто.
OFFER_PATTERN = r"(?i)^(?:[./])?benz(?:@\w+)?\b(?:\s+(?P<args>.+))?$"

# Типы чатов, где предложение видно всем участникам.
GROUP_CHAT_TYPES = frozenset({"group", "supergroup"})

# Живые фоновые задачи (уведомление продавцу). Нужны, чтобы сборщик мусора
# не отменил их на полпути.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


def build_transfer_url(template: str, username: str | None) -> str:
    """Подставляет ``{username}`` в шаблон deep link."""
    return template.replace("{username}", username or "")


def resolve_recipient(
    *,
    explicit: str | None,
    accepter_username: str | None,
    settings: Settings,
) -> str | None:
    """Определяет, кому предлагается передать подарок.

    Порядок: явный ``@username`` из команды → username того, кто нажал
    «Принять» (это и есть покупатель) → значение из ``.env``.
    """
    if explicit:
        return explicit
    if accepter_username:
        return accepter_username
    return settings.transfer_recipient_username


def build_command_key(business_connection_id: str | None, message: Message) -> str:
    """Стабильный идентификатор сообщения с командой.

    Одно и то же сообщение, доставленное повторно, даёт один и тот же ключ,
    поэтому по нему можно пресечь создание второй карточки.
    """
    if business_connection_id:
        return f"b:{business_connection_id}:{message.chat.id}:{message.message_id}"
    return f"m:{message.chat.id}:{message.message_id}"


async def _handle_offer_command(
    message: Message,
    db: Database,
    settings: Settings,
    gateway: TelegramGateway,
    business_connection_id: str | None,
) -> None:
    author = message.from_user
    chat = message.chat
    if author is None or author.is_bot:
        return
    # Любой чат: личный, группа, супергруппа.
    if chat.type not in {"private", *GROUP_CHAT_TYPES}:
        return

    # В группе команду может набрать кто угодно, а ответ уходит от имени
    # бизнес-аккаунта. Чужие команды не обрабатываем.
    if business_connection_id and settings.only_business_owner_commands:
        connection = await BusinessConnectionRepository(db).get_enabled(business_connection_id)
        if connection is not None and int(connection["user_id"]) != author.id:
            logger.info(
                "benz отклонена: %s не владелец бизнес-подключения %s",
                author.id,
                business_connection_id,
            )
            return

    # Язык разбираем раньше остального: по нему будут и подсказки об ошибках.
    draft_lang = _detect_lang(message.text or message.caption or "", settings)

    command_text = (message.text or message.caption or "").strip()
    parts = command_text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await gateway.send(
            chat.id,
            draft_lang.usage,
            business_connection_id=business_connection_id,
            reply_to_message_id=message.message_id,
        )
        return

    try:
        args = split_offer_args(parts[1], draft_lang)
        gift = parse_gift_link(args.gift_raw, draft_lang)
        price = parse_stars(args.price_raw, draft_lang)
    except OfferParseError as exc:
        await gateway.send(
            chat.id,
            f"⚠️ {exc}",
            business_connection_id=business_connection_id,
            reply_to_message_id=message.message_id,
        )
        return

    # Суффикс языка в команде важнее языка по умолчанию.
    lang = get_lang(args.language or settings.default_language)

    now = utcnow()
    expires_at = now + timedelta(seconds=settings.offer_ttl_seconds)
    offer_id = uuid.uuid4().hex
    title = gift.display_title
    currency = args.currency
    currency_label = settings.currency_label(currency)
    repository = OfferRepository(db)

    # В личном чате карточка адресована собеседнику, в группе — всем сразу.
    is_group = chat.type in GROUP_CHAT_TYPES
    buyer_user_id = 0 if is_group else chat.id

    # Идемпотентность: одна команда = одна карточка. Пока предложение не
    # зафиксировано в БД, повторная доставка того же апдейта (Telegram
    # гарантирует «хотя бы один раз» и может повторить после рестарта) или
    # второй инстанс бота создали бы дубль-карточку.
    command_key = build_command_key(business_connection_id, message)
    offer, inserted = await repository.create_if_unique(
        {
            "id": offer_id,
            "command_key": command_key,
            "business_connection_id": business_connection_id,
            "chat_id": chat.id,
            "chat_type": chat.type,
            "message_id": None,
            "buyer_user_id": buyer_user_id,
            "buyer_username": None if is_group else author.username,
            "seller_user_id": author.id,
            "seller_username": author.username,
            "seller_name": author.full_name,
            "recipient_username": args.recipient,
            "gift_slug": gift.slug,
            "gift_number": gift.number,
            "gift_title": title,
            "gift_url": gift.url,
            "stars": price,
            "currency": currency,
            "language": lang.code,
            "status": STATUS_PENDING,
            "created_at": now.isoformat(timespec="seconds"),
            "expires_at": expires_at.isoformat(timespec="seconds"),
        },
    )

    # Текст карточки берём из строки БД: он нужен и свежей сделке, и дублю,
    # у которого первая доставка могла не дойти (message_id пустой).
    card = templates.offer_card(
        lang=lang,
        # В карточке показывается ник из профиля, username — только запасной
        # вариант для аккаунтов без имени.
        seller_username=offer["seller_username"],
        seller_name=offer["seller_name"] or offer["seller_username"],
        gift_title=offer["gift_title"],
        gift_url=offer["gift_url"],
        price=offer["stars"],
        currency=offer["currency"],
        currency_label=currency_label,
        remaining_seconds=settings.offer_ttl_seconds,
    )

    if not inserted:
        # Команда уже обработана (дубль апдейта / второй инстанс). Если
        # первая карточка не успела уйти — досылаем её с тем же предложением,
        # иначе тихо пропускаем: вторая карточка не создаётся.
        if offer["message_id"] is None and offer["status"] == STATUS_PENDING:
            logger.warning(
                "Дубль команды %s: карточка %s не была доставлена, досылаю",
                command_key,
                offer["id"],
            )
            try:
                sent = await gateway.send(
                    chat.id,
                    card,
                    business_connection_id=business_connection_id,
                    reply_markup=inline.offer_keyboard(offer["id"], lang),
                    link_preview_options=LinkPreviewOptions(show_above_text=True),
                )
            except TelegramAPIError as exc:
                logger.warning("Дослать карточку %s не вышло: %s", offer["id"], exc)
                return
            await repository.set_message_id(offer["id"], sent.message_id)
        else:
            logger.info(
                "Дубль команды %s пропущен (карточка %s уже доставлена)",
                command_key,
                offer["id"],
            )
        return

    logger.info(
        "Новое предложение %s [%s]: %s за %s %s (чат=%s/%s, business=%s)",
        offer_id,
        lang.code,
        title,
        price,
        currency,
        chat.id,
        chat.type,
        business_connection_id or "-",
    )

    # Команда удаляется ДО отправки карточки: так в чате не мелькает сырой
    # текст «benz ...». Раньше порядок был обратным (сначала карточка, потом
    # удаление) — это нужно было только ради reply_to_message_id, который
    # больше не используется.
    #
    # Если удаление не вышло (нет прав) — не страшно: карточка всё равно
    # отправится, а команда останется в чате.
    if settings.delete_command_message:
        deleted = await gateway.delete(
            chat.id, message.message_id, business_connection_id=business_connection_id
        )
        if not deleted:
            logger.info("Не удалось удалить команду %s, отправляю карточку", message.message_id)

    try:
        # Карточка уходит отдельным сообщением: reply_to_message_id не
        # проставляем — команда удалена, и «ответ на удалённое сообщение»
        # только портит вид чата.
        # Ссылка на подарок единственная в тексте, поэтому Telegram рисует
        # предпросмотр; show_above_text ставит его над текстом.
        sent = await gateway.send(
            chat.id,
            card,
            business_connection_id=business_connection_id,
            reply_markup=inline.offer_keyboard(offer_id, lang),
            link_preview_options=LinkPreviewOptions(show_above_text=True),
        )
    except TelegramAPIError as exc:
        await repository.delete(offer_id)
        logger.warning(
            "Не удалось отправить карточку %s (чат=%s, business=%s): %s",
            offer_id,
            chat.id,
            business_connection_id or "-",
            exc,
        )
        await _explain_send_failure(
            gateway,
            chat.id,
            business_connection_id,
            None,
            exc,
            lang,
            notify_user_id=author.id,
        )
        return
    except Exception:
        # Не оставляем «висящее» предложение, если отправка упала с ошибкой.
        await repository.delete(offer_id)
        raise

    await repository.set_message_id(offer_id, sent.message_id)

    # Telegram возвращает business_connection_id в отправленном сообщении —
    # это надёжный признак того, что карточка ушла от имени аккаунта, а не
    # бота. Без этой строки откатную отправку видно только по косвенным
    # признакам, а разница для продавца принципиальна.
    if business_connection_id and not sent.business_connection_id:
        logger.warning(
            "Карточка %s ушла ОТ ИМЕНИ БОТА, а не бизнес-аккаунта: "
            "Telegram не дал аккаунту написать в чат %s. Карточка получена, но подпись "
            "будет от бота — переподключите бота с правами «Разрешить отвечать "
            "на сообщения», чтобы отправлять от своего имени.",
            offer_id,
            chat.id,
        )
    else:
        logger.info(
            "Карточка %s отправлена от имени бизнес-аккаунта (чат=%s)", offer_id, chat.id
        )


def _detect_lang(text: str, settings: Settings) -> Lang:
    """Язык по суффиксу в конце команды; без него — из настроек.

    Нужен до разбора остальных аргументов, чтобы подсказки об ошибках
    были на том же языке, что и запрошенная карточка.
    """
    from app.i18n import resolve_lang  # локальный импорт: без цикла

    for token in reversed(text.split()):
        code = resolve_lang(token)
        if code is not None:
            return get_lang(code)
    return get_lang(settings.default_language)


async def _may_resolve(
    offer: dict,
    callback: CallbackQuery,
    settings: Settings,
    db: Database,
) -> bool:
    """Проверяет, может ли нажавший пользователь закрыть предложение."""
    user_id = callback.from_user.id
    policy = settings.accept_policy
    buyer_id = int(offer["buyer_user_id"])
    is_author = user_id == int(offer["seller_user_id"])

    if is_author:
        # Автору кнопки не реагируют: карточка адресована покупателю.
        # Исключение — диалог с самим ботом, где карточка пришла тому же
        # человеку, который её создал (``buyer_user_id`` совпадает с автором).
        if policy != "author":
            return buyer_id == user_id
        return True

    if policy == "business_owner":
        connection_id = offer["business_connection_id"]
        if not connection_id:
            return False
        connection = await BusinessConnectionRepository(db).get(connection_id)
        return connection is not None and int(connection["user_id"]) == user_id

    # В личном чате карточка адресована конкретному собеседнику,
    # в группе — любому участнику.
    return buyer_id == 0 or user_id == buyer_id


async def on_offer_callback(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    gateway: TelegramGateway,
) -> None:
    """Нажатие «Принять» / «Отклонить».

    Защита от повторного нажатия — атомарный ``UPDATE ... WHERE status='pending'``
    в :meth:`OfferRepository.claim`: завершить предложение может ровно один
    обработчик, все остальные получают ``None`` и показывают тост без правок.
    """
    message = callback.message
    parsed = inline.parse_callback(callback.data or "") if callback.data else None
    if message is None or parsed is None:
        lang = get_lang(settings.default_language)
        await callback.answer(lang.not_found, show_alert=True)
        return

    action, offer_id = parsed
    if action not in (CB_ACCEPT, CB_DECLINE):
        await callback.answer(get_lang(settings.default_language).not_found, show_alert=True)
        return

    repository = OfferRepository(db)
    offer = await repository.get(offer_id)
    if offer is None:
        await callback.answer(get_lang(settings.default_language).not_found, show_alert=True)
        return

    # Карточка, кнопку которой нажали, была отрисована на языке продавца.
    lang = get_lang(offer["language"])

    if not await _may_resolve(offer, callback, settings, db):
        own = callback.from_user.id == int(offer["seller_user_id"])
        await callback.answer(lang.own_offer if own else lang.not_your_offer, show_alert=True)
        if own:
            logger.info(
                "Автор %s нажал кнопки своего предложения %s — игнорирую",
                callback.from_user.id,
                offer_id,
            )
        return

    target_status = STATUS_ACCEPTED if action == CB_ACCEPT else STATUS_DECLINED
    claimed = await repository.claim(offer_id, target_status, callback.from_user.id)
    if claimed is None:
        current = await repository.get(offer_id)
        still_pending = current is not None and current["status"] == STATUS_PENDING
        await callback.answer(
            lang.expired if still_pending else lang.already_resolved,
            show_alert=True,
        )
        logger.info(
            "Дублирующее нажатие по предложению %s (текущий статус: %s)",
            offer_id,
            current["status"] if current else "unknown",
        )
        return

    # Обязателен для редактирования сообщений, отправленных бизнес-аккаунтом.
    business_connection_id = message.business_connection_id or offer["business_connection_id"]
    currency_label = settings.currency_label(offer["currency"])
    recipient: str | None = None

    if target_status == STATUS_ACCEPTED:
        recipient = resolve_recipient(
            explicit=offer["recipient_username"],
            accepter_username=callback.from_user.username,
            settings=settings,
        )
        await repository.set_recipient(offer_id, recipient)

        text = templates.accepted_card(
            lang=lang,
            gift_title=offer["gift_title"],
            gift_url=offer["gift_url"],
            currency=offer["currency"],
            currency_label=currency_label,
        )
        # tg://send_gift?to=<username> открывает интерфейс передачи подарка.
        # Если username неизвестен — ведём на страницу самого подарка.
        transfer_url = (
            build_transfer_url(settings.transfer_deep_link, recipient)
            if recipient
            else offer["gift_url"]
        )
        keyboard: InlineKeyboardMarkup | None = inline.transfer_keyboard(
            transfer_url=transfer_url,
            lang=lang,
            how_to_url=settings.how_to_transfer_url,
        )
        toast = lang.toast_sold
        # Ссылка на подарок — единственная в карточке, поэтому Telegram рисует
        # предпросмотр. show_above_text ставит его над текстом, а не под ним.
        preview = LinkPreviewOptions(show_above_text=True)
    else:
        text = templates.declined_card(lang)
        keyboard = None
        toast = lang.toast_declined
        preview = LinkPreviewOptions(is_disabled=True)

    # Тост отправляем ДО редактирования: клиент перестаёт крутить спиннер
    # сразу, не дожидаясь round-trip к Telegram за текстом карточки.
    await callback.answer(toast)

    try:
        await gateway.edit(
            message.chat.id,
            message.message_id,
            text,
            business_connection_id=business_connection_id,
            reply_markup=keyboard,
            link_preview_options=preview,
        )
    except TelegramBadRequest:
        # Сообщение слишком старое или редактирование недоступно. Вместо тишины
        # отправляем карточку заново — покупатель увидит результат в любом случае.
        logger.warning(
            "Редактирование карточки %s не удалось (%s), отправляю заново",
            offer_id,
            lang.edit_failed,
        )
        try:
            await gateway.send(
                message.chat.id,
                text,
                business_connection_id=business_connection_id,
                reply_markup=keyboard,
                link_preview_options=preview,
            )
        except TelegramAPIError:
            logger.exception("Не удалось отправить карточку %s повторно", offer_id)
            return

    # Уведомление продавцу не должно задерживать редактирование: оно уходит
    # отдельной задачей и не влияет на то, что увидит покупатель.
    if target_status == STATUS_ACCEPTED:
        # Ссылку держим: иначе задача может быть собрана сборщиком мусора
        # до завершения, и уведомление молча потеряется.
        task = asyncio.create_task(_notify_seller_safely(offer, settings, gateway, callback, lang))
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

    logger.info(
        "Предложение %s -> %s [%s] (решение: %s, получатель: %s)",
        offer_id,
        target_status,
        lang.code,
        callback.from_user.id,
        recipient or "-",
    )


async def _notify_seller_safely(
    offer: dict,
    settings: Settings,
    gateway: TelegramGateway,
    callback: CallbackQuery,
    lang: Lang,
) -> None:
    """Фоновая обёртка: исключение задачи не должно будить event loop."""
    try:
        await _notify_seller(offer, settings, gateway, callback, lang)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Фоновое уведомление продавцу не удалось")


async def _notify_seller(
    offer: dict,
    settings: Settings,
    gateway: TelegramGateway,
    callback: CallbackQuery,
    lang: Lang,
) -> None:
    """Личное уведомление продавцу: кому и за сколько продали."""
    seller_id = int(offer["seller_user_id"])
    if seller_id == callback.from_user.id:
        return
    try:
        await gateway.send(
            seller_id,
            templates.seller_notification(
                lang=lang,
                gift_title=offer["gift_title"],
                gift_url=offer["gift_url"],
                price=int(offer["stars"]),
                currency=offer["currency"],
                currency_label=settings.currency_label(offer["currency"]),
                buyer_username=callback.from_user.username,
            ),
        )
    except TelegramAPIError:
        # Продавец мог не начать диалог с ботом — это не повод ронять сценарий.
        logger.info("Не удалось уведомить продавца %s", seller_id)


def _failure_hint(exc: TelegramAPIError, lang: Lang) -> str:
    """Человеческое объяснение ошибки отправки вместо тишины.

    ``BUSINESS_PEER_INVALID`` проверяется первым: это ограничение на стороне
    аккаунта, и совет «написать боту /start» здесь бессмысленен — карточку
    должен отправить аккаунт, а не бот.
    """
    text = str(exc).lower()
    if isinstance(exc, BusinessFallbackFailed) or any(
        marker in text for marker in _BUSINESS_PEER_MARKERS
    ):
        # Самая частая и самая «тихая» поломка: сообщение не доходит, потому
        # что у подключения нет права «отвечать» либо в диалоге не было
        # входящих за сутки. Без этой ветки пользователь видит только лог.
        return lang.hint_business_peer
    if "retry after" in text or isinstance(exc, TelegramRetryAfter):
        seconds = getattr(exc, "retry_after", 0)
        return lang.hint_retry.format(seconds=seconds)
    if "peer_flood" in text or "too many requests" in text:
        return lang.hint_flood
    if "can't initiate conversation" in text or "bot_access_forbidden" in text:
        # Актуально только если писать от имени самого бота: для сообщений
        # через business_connection_id этого ограничения нет.
        return lang.hint_chat
    if "not enough rights" in text or "forbidden" in text:
        return lang.hint_rights
    if "business_connection" in text:
        return lang.hint_connection
    return lang.hint_generic


async def _explain_send_failure(
    gateway: TelegramGateway,
    chat_id: int,
    business_connection_id: str | None,
    reply_to_message_id: int | None,
    exc: TelegramAPIError,
    lang: Lang,
    notify_user_id: int | None = None,
) -> None:
    """Сообщает пользователю причину отказа. Молчание — худший вариант.

    Если в чат написать не удалось, объяснение уходит продавцу: сначала от его
    имени аккаунта (он активнее всех, туда бот точно достучится), затем от
    имени самого бота. Без этого продавец видит только «команда
    проигнорирована» и ищет причину в боте, а она в настройках аккаунта.
    """
    text = f"⚠️ {lang.hint_send_failed}\n\n{_failure_hint(exc, lang)}"

    attempts: list[tuple[str, int, str | None]] = [("чат", chat_id, business_connection_id)]
    if notify_user_id is not None and notify_user_id != chat_id:
        attempts.append(("аккаунт продавца", notify_user_id, business_connection_id))
        attempts.append(("бот → продавец", notify_user_id, None))

    failures: list[str] = []
    for label, target, connection in attempts:
        try:
            await gateway.send(
                target,
                text,
                business_connection_id=connection,
                **(
                    {}
                    if reply_to_message_id is None or target != chat_id
                    else {"reply_to_message_id": reply_to_message_id}
                ),
            )
        except TelegramAPIError as inner:
            failures.append(f"{label}: {inner}")
            continue
        logger.info("Причина отказа отправлена через %s", label)
        return

    logger.error(
        "Не удалось сообщить об ошибке отправки ни одним каналом (%s)",
        "; ".join(failures) or "нет каналов",
    )


def create_offer_router(throttle: BaseMiddleware | None = None) -> Router:
    """Фабрика роутера предложений: новый экземпляр на каждый Dispatcher.

    ``throttle`` навешивается только на текстовые команды этого роутера,
    чтобы не влиять на нажатия inline-кнопок.
    """

    async def on_offer_in_bot_chat(
        message: Message,
        db: Database,
        settings: Settings,
        gateway: TelegramGateway,
        business_connection_id: str | None,
    ) -> None:
        """``benz`` в обычном диалоге с ботом."""
        await _handle_offer_command(message, db, settings, gateway, business_connection_id)

    async def on_offer_in_business_chat(
        message: Message,
        db: Database,
        settings: Settings,
        gateway: TelegramGateway,
        business_connection_id: str | None,
    ) -> None:
        """``benz`` в любом чате от имени бизнес-аккаунта.

        ``business_connection_id`` уже подставлен middleware из апдейта,
        поэтому ответ уходит от имени аккаунта.
        """
        await _handle_offer_command(message, db, settings, gateway, business_connection_id)

    router = Router(name="offer")
    if throttle is not None:
        router.message.middleware(throttle)
        router.business_message.middleware(throttle)
    router.message.register(on_offer_in_bot_chat, F.text.regexp(OFFER_PATTERN))
    router.business_message.register(on_offer_in_business_chat, F.text.regexp(OFFER_PATTERN))
    router.callback_query.register(on_offer_callback, F.data.startswith(f"{CALLBACK_PREFIX}:"))
    return router