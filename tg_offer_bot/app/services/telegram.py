"""Шлюз к Telegram API с точечными fallback-ами.

Telegram отклоняет запрос из-за оформления кнопок (``style`` /
``icon_custom_emoji_id``) или из-за HTML-разметки. Вместо «мёртвой» кнопки
методы ниже автоматически упрощают запрос.

Важно: деградация применяется ТОЛЬКО к ошибкам форматирования. Ошибки вроде
``PEER_FLOOD``, ``CHAT_FORBIDDEN``, ``USER_IS_BLOCKED`` не имеют ничего общего
с разметкой — повторять такой запрос бессмысленно и опасно: это добивает
флуд-лимит и маскирует реальную проблему за «успешной» отправкой.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.utils.text import downgrade_tg_emoji, strip_html

logger = logging.getLogger(__name__)


class BusinessFallbackFailed(TelegramBadRequest):
    """Отправка не вышла ни от имени аккаунта, ни от имени бота.

    Нужен, чтобы обработчик объяснил **первую** причину (``BUSINESS_PEER_INVALID``
    — ограничение на стороне бизнес-аккаунта), а не вторую, возникшую уже при
    откатной отправке. Без этого пользователю советуют «написать боту /start»,
    хотя дело в правах его аккаунта.
    """

    def __init__(self, original: TelegramBadRequest, fallback: TelegramBadRequest) -> None:
        super().__init__(
            method=getattr(original, "method", None),
            message=f"{original.message} (после отправки от имени бота: {fallback.message})",
        )
        self.original = original
        self.fallback = fallback


_MAX_ATTEMPTS = 4
_MAX_RETRY_DELAY = 5.0

# Паузы между повторами при флуд-лимите: 0.4 → 1.0 → 2.0 с.
#
# Раньше паузы росли экспоненциально (3 → 9 → 27 с). Очередь апдейтов у
# бота общая, поэтому каждое ожидание здесь — это задержка следующей команды
# для всех. Короткие повторы почти всегда укладываются в лимит Telegram, а
# если нет, апдейт лучше обработать позже целиком, чем «зависнуть» на 27 с.
_RETRY_LADDER: tuple[float, ...] = (0.4, 1.0, 2.0)


class FloodGuard:
    """Общий тормоз после флуд-лимита Telegram.

    Пейсинг команд работает в пределах одного пользователя: пачка от десяти
    разных людей всё равно уходит в Telegram залпом и получает 429. Здесь
    нужен тормоз на всех сразу — как только Telegram сказал «подожди», любая
    следующая команда выдерживает паузу, независимо от её автора.

    Пауза всегда конечна и определяется самим Telegram (``retry_after``), а не
    растёт без предела: цель — не «переждать» лимит, а не ловить его снова.
    """

    def __init__(self, cap: float = _MAX_RETRY_DELAY * 2) -> None:
        self._cap = cap
        self._penalty_until = 0.0
        self._streak = 0

    def note_flood(self, delay: float) -> None:
        """Включить общую паузу после флуд-ответа Telegram."""
        self._streak += 1
        # Пока флуд повторяется, паузу приходится наращивать; потолок не даёт
        # боту встать колом надолго.
        pause = min(delay * min(2 ** (self._streak - 1), 4), self._cap)
        self._penalty_until = max(self._penalty_until, time.monotonic() + pause)
        logger.warning("Общая пауза %.1f с после флуд-лимита Telegram", pause)

    def note_success(self) -> None:
        """Успешная отправка сбрасывает счётчик нарастания паузы."""
        self._streak = 0

    def delay(self) -> float:
        """Сколько ещё ждать перед следующей командой (0 — можно отправлять)."""
        return max(0.0, self._penalty_until - time.monotonic())


FLOOD_GUARD = FloodGuard()

# Ошибки, которые лечатся упрощением запроса.
# ``invalid button style specified`` — точная формулировка Bot API
# (источник: ``Client::get_button_style``). Ручные маркеры вида
# ``button_style_invalid`` оставлены на случай серверного ответа.
_FORMATTING_MARKERS = (
    "button_custom_emoji_invalid",
    "button_style_invalid",
    "invalid button style",
    "button_type_invalid",
    "button_url_invalid",
    "can't parse entities",
    "unsupported start tag",
    "can't find end tag",
    "entities are unsupported",
    # Кастомные эмодзи в тексте требуют Telegram Premium у владельца бота.
    # Без подписки Telegram отвергает весь запрос, поэтому это тоже ошибка
    # разметки: запрос надо упростить, а не показывать пользователю.
    "entity_text_invalid",
)

# Временные ограничения: запрос имеет смысл повторить.
_FLOOD_MARKERS = ("peer_flood", "too many requests", "retry after")

# Ситуации, когда бизнес-аккаунт не может написать конкретному человеку.
#
# ``BUSINESS_PEER_INVALID`` — не выдано право «Разрешить отвечать на сообщения»
# либо покупатель ни разу не писал с этого аккаунта в этот чат. Telegram не даёт
# боту писать за аккаунт. Это не поломка подключения и повторять запрос
# бессмысленно — единственный способ доставить карточку, пока права не выданы,
# отправить её от имени самого бота: у него таких ограничений нет.
_BUSINESS_PEER_MARKERS = (
    "business_peer_invalid",
    "business_connection_invalid",
    "peer_flood",
)

# Сообщение Telegram при попытке отредактировать текст на тот же самый.
_NOT_MODIFIED_MARKERS = ("message is not modified", "message can't be edited")


def _describe(exc: Exception) -> str:
    message = str(exc)
    description = getattr(exc, "description", "")
    return f"{message} | {description}".lower()


def _is_formatting_error(exc: TelegramBadRequest) -> bool:
    """Ошибка вызвана разметкой/оформлением, а не логикой запроса."""
    text = _describe(exc)
    return any(marker in text for marker in _FORMATTING_MARKERS)


def _is_flood_error(exc: Exception) -> bool:
    """Временное ограничение Telegram — запрос нужно повторить позже."""
    if isinstance(exc, TelegramRetryAfter):
        return True
    return isinstance(exc, TelegramBadRequest) and any(
        marker in _describe(exc) for marker in _FLOOD_MARKERS
    )


def _is_business_peer_error(exc: Exception) -> bool:
    """Бизнес-аккаунт не может написать этому чату."""
    return isinstance(exc, TelegramAPIError) and any(
        marker in _describe(exc) for marker in _BUSINESS_PEER_MARKERS
    )


# Необязательные поля кнопки (Bot API 9.4+).
# ``icon_custom_emoji_id`` требует Telegram Premium у владельца бота, поэтому
# поддержка может отвалиться отдельно от цветов. Снимая по одному полу, мы
# сохраняем ``style``, если не поддержан только ``icon_custom_emoji_id``.
_BUTTON_EXTRAS: tuple[str, ...] = ("icon_custom_emoji_id", "style")


def _field_to_strip(error_text: str) -> str | None:
    """По тексту ошибки определяет, какое поле кнопки виновато.

    Telegram ругается конкретно на поле, поэтому снимать надо именно его, а не
    «первое попавшееся»: иначе, поймав ``Invalid button style specified``, мы
    зря потеряли бы ещё и иконку.
    """
    if "custom_emoji" in error_text or "custom emoji" in error_text:
        return "icon_custom_emoji_id"
    if "style" in error_text:
        return "style"
    return None


def _degrade_markup(
    markup: InlineKeyboardMarkup | None,
    prefer: str | None = None,
) -> InlineKeyboardMarkup | None:
    """Снимает с кнопок необязательное поле, не трогая действие кнопки.

    Сначала пробует поле, на которое указывает ``prefer``, затем остальные по
    порядку. Возвращает ``None``, если снимать уже нечего.
    """
    if markup is None:
        return None

    order = [prefer] if prefer in _BUTTON_EXTRAS else []
    order += [field for field in _BUTTON_EXTRAS if field != prefer]

    for field in order:
        if not any(
            getattr(button, field, None) is not None
            for row in markup.inline_keyboard
            for button in row
        ):
            continue

        def rebuild(button: InlineKeyboardButton, *, drop: str = field) -> InlineKeyboardButton:
            return InlineKeyboardButton(
                text=button.text,
                url=button.url,
                callback_data=button.callback_data,
                icon_custom_emoji_id=(
                    None if drop == "icon_custom_emoji_id" else button.icon_custom_emoji_id
                ),
                style=None if drop == "style" else button.style,
            )

        return InlineKeyboardMarkup(
            inline_keyboard=[[rebuild(button) for button in row] for row in markup.inline_keyboard]
        )
    return None


class TelegramGateway:
    """Обёртка над ``Bot`` для отправки и редактирования сообщений бота."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    @property
    def bot(self) -> Bot:
        return self._bot

    async def _execute(
        self,
        call: Callable[..., Coroutine[Any, Any, Any]],
        *,
        text: str,
        markup: InlineKeyboardMarkup | None,
        business_connection_id: str | None,
        **kwargs: Any,
    ) -> Message | bool:
        current_text: str = text
        current_markup: InlineKeyboardMarkup | None = markup
        current_connection: str | None = business_connection_id
        parse_mode: str | None = "HTML"
        downgraded = False
        downgrade_reason: TelegramBadRequest | None = None
        # Премиум-эмодзи Telegram может отклонить разово: сначала повторяем
        # тот же запрос, и только потом заменяем теги обычными символами.
        premium_retried = False

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                result = await call(
                    text=current_text,
                    reply_markup=current_markup,
                    business_connection_id=current_connection,
                    parse_mode=parse_mode,
                    **kwargs,
                )
                FLOOD_GUARD.note_success()
                return result
            # Ловим любую ошибку Bot API: откат может упасть как 400
            # (``BUSINESS_PEER_INVALID``), так и 403 (``bot_access_forbidden``,
            # «бот не может начать диалог»). Если поймать только 400, ошибка
            # отката уйдёт наружу необработанной, и пользователю покажут
            # следствие («пусть напишет боту /start») вместо причины — прав
            # аккаунта.
            except TelegramAPIError as exc:
                # 0. Бизнес-аккаунт не может написать в этот чат: не выдано
                #    право «Разрешить отвечать на сообщения» либо покупатель ни
                #    разу не писал с этого аккаунта. Повторять бессмысленно —
                #    Telegram откажет снова. Молчать тоже нельзя: карточка
                #    потеряется. Отправляем от имени самого бота — карточка
                #    дойдёт и сделка состоится. Понижение делаем один раз.
                if current_connection and not downgraded and _is_business_peer_error(exc):
                    downgraded = True
                    downgrade_reason = exc
                    logger.warning(
                        "Бизнес-аккаунт не может написать (%s) — отправляю от имени бота",
                        _describe(exc),
                    )
                    current_connection = None
                    continue

                # 1. Временный флуд-лимит: ждём и повторяем тот же запрос.
                #    PEER_FLOOD не означает блокировку — через пару секунд
                #    Telegram снова принимает сообщение.
                if _is_flood_error(exc):
                    if attempt >= _MAX_ATTEMPTS:
                        raise
                    delay = float(getattr(exc, "retry_after", 0) or 0) or _RETRY_LADDER[
                        attempt - 1
                    ]
                    delay = min(delay, _MAX_RETRY_DELAY)
                    # Пауза одного запроса не спасает остальных: после флуда
                    # Telegram ждёт весь бот, а не только этот вызов.
                    FLOOD_GUARD.note_flood(delay)
                    logger.warning(
                        "Флуд-лимит Telegram (попытка %s/%s), повтор через %.1f с",
                        attempt,
                        _MAX_ATTEMPTS,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                # 2. Ошибки форматирования — упрощаем запрос по шагам:
                #    сначала иконка кнопки, потом её цвет, и только потом
                #    отказ от HTML. Действие кнопки не теряется никогда.
                if isinstance(exc, TelegramBadRequest) and _is_formatting_error(exc):
                    field = _field_to_strip(_describe(exc))

                    # Кастомные эмодзи в тексте Telegram отклоняет с
                    # ``ENTITY_TEXT_INVALID`` — и отвергает весь запрос, а не
                    # только тег, поэтому карточка «продан» могла бы не
                    # дойти вовсе. Разовый сбой (наблюдается на практике)
                    # лечится повтором того же запроса; если отказ повторился,
                    # теги заменяются обычными символами — вся остальная
                    # разметка при этом сохраняется.
                    if "entity_text_invalid" in _describe(exc):
                        if not premium_retried:
                            premium_retried = True
                            logger.warning(
                                "Telegram отклонил премиум-эмодзи (попытка %s) — "
                                "повторяю тот же запрос",
                                attempt,
                            )
                            continue
                        simplified = downgrade_tg_emoji(current_text)
                        if simplified != current_text:
                            logger.warning(
                                "Премиум-эмодзи заменены обычными символами "
                                "(попытка %s). Если нужны именно премиум-иконки, "
                                "проверьте подписку Telegram Premium у владельца бота",
                                attempt,
                            )
                            current_text = simplified
                            continue

                    degraded = _degrade_markup(current_markup, field)
                    if degraded is not None:
                        logger.warning(
                            "Кнопки упрощены: снято %s (попытка %s): %s",
                            field or "первое лишнее поле",
                            attempt,
                            _describe(exc),
                        )
                        current_markup = degraded
                        continue

                    if parse_mode is not None:
                        logger.warning(
                            "HTML не распознан, отправляю plain text (попытка %s): %s",
                            attempt,
                            _describe(exc),
                        )
                        current_text = strip_html(current_text)
                        parse_mode = None
                        continue

                # 3. Всё остальное (CHAT_FORBIDDEN, BUSINESS_PEER_INVALID при
                #    откате, BAD_REQUEST на логику) — упрощением не лечится.
                if downgraded and downgrade_reason is not None:
                    # Откатная отправка тоже не вышла. Сохраняем ПЕРВУЮ причину:
                    # именно она объясняет, что мешает бизнес-аккаунту, а вторая
                    # лишь следствие отката.
                    raise BusinessFallbackFailed(downgrade_reason, exc) from exc
                raise

        raise RuntimeError("TelegramGateway: исчерпаны попытки отправки")

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        business_connection_id: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        **kwargs: Any,
    ) -> Message:
        result = await self._execute(
            self._bot.send_message,
            text=text,
            markup=reply_markup,
            business_connection_id=business_connection_id,
            chat_id=chat_id,
            **kwargs,
        )
        if not isinstance(result, Message):
            raise TypeError(f"sendMessage вернул неожиданный тип: {type(result)!r}")
        return result

    async def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        business_connection_id: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        **kwargs: Any,
    ) -> Message | bool:
        try:
            return await self._execute(
                self._bot.edit_message_text,
                text=text,
                markup=reply_markup,
                business_connection_id=business_connection_id,
                chat_id=chat_id,
                message_id=message_id,
                **kwargs,
            )
        except TelegramBadRequest as exc:
            # aiogram не имеет отдельного исключения для «message is not modified»,
            # оно приходит как BadRequest. Это не ошибка сценария: сообщение
            # уже актуально, повторное редактирование не нужно.
            if any(marker in _describe(exc) for marker in _NOT_MODIFIED_MARKERS):
                logger.debug("Сообщение %s не изменилось, пропускаю редактирование", message_id)
                return True
            raise

    async def delete(
        self,
        chat_id: int,
        message_id: int,
        business_connection_id: str | None = None,
    ) -> bool:
        """Удаляет сообщение. Отсутствие прав не считается ошибкой сценария.

        Сообщения бизнес-аккаунта удаляются через ``deleteBusinessMessages``:
        параметр ``business_connection_id`` есть только у ``deleteMessage``, и
        только начиная с Bot API 9.0. Пока библиотека его не поддерживает,
        удалить собственное сообщение аккаунта бот может единственным
        способом — через бизнес-подключение. Без него работаем по правам на чат
        (``can_delete_sent_messages`` / ``can_delete_all_messages``).

        Практическая разница видна сразу: без подключения удалить команду не
        удастся (у бота нет доступа к чужому личному диалогу), и пользователь
        видит «команда проигнорирована» вместо карточки.
        """
        try:
            if business_connection_id:
                return await self._bot.delete_business_messages(
                    business_connection_id=business_connection_id,
                    message_ids=[message_id],
                )
            return await self._bot.delete_message(chat_id=chat_id, message_id=message_id)
        except (TelegramBadRequest, TelegramRetryAfter) as exc:
            logger.info(
                "Не удалось удалить сообщение %s в чате %s: %s",
                message_id,
                chat_id,
                _describe(exc),
            )
            return False
