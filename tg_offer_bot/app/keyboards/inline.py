"""Инлайн-клавиатуры.

Важно: текст кнопки не поддерживает HTML-разметку. Premium-эмодзи на кнопке
задаются полем ``icon_custom_emoji_id``, а цвет — полем ``style``
(``danger`` / ``success`` / ``primary``, Bot API 9.0+).

По дизайну карточки предложения цвет не используется: он читается как
«системный диалог» и перетягивает внимание с текста предложения. Оформление
кнопок остаётся только в виде премиум-иконок ✅/❌.

Подписи кнопок переведены (:mod:`app.i18n`), потому что их нажимает покупатель —
у него может быть другой язык, чем у продавца.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.constants import (
    CALLBACK_PREFIX,
    CB_ACCEPT,
    CB_DECLINE,
    EMOJI_ACCEPT,
    EMOJI_DECLINE,
)
from app.i18n import RU, Lang


def _callback(action: str, offer_id: str) -> str:
    # Ограничение Telegram — 1..64 байта.
    return f"{CALLBACK_PREFIX}:{action}:{offer_id}"


def parse_callback(data: str) -> tuple[str, str] | None:
    """Разбирает ``of:<action>:<offer_id>`` -> ``(action, offer_id)``."""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        return None
    return parts[1], parts[2]


def offer_keyboard(offer_id: str, lang: Lang = RU) -> InlineKeyboardMarkup:
    """Кнопки под карточкой нового предложения: «Отклонить» слева, «Принять» справа.

    Иконки премиум-эмодзи остаются (✅/❌), а ``style`` убран: цветная кнопка
    читается как «системный диалог», а здесь нужно спокойное действие в чате.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=lang.decline,
                    callback_data=_callback(CB_DECLINE, offer_id),
                    icon_custom_emoji_id=EMOJI_DECLINE,
                ),
                InlineKeyboardButton(
                    text=lang.accept,
                    callback_data=_callback(CB_ACCEPT, offer_id),
                    icon_custom_emoji_id=EMOJI_ACCEPT,
                ),
            ],
        ],
    )


def transfer_keyboard(
    transfer_url: str,
    lang: Lang = RU,
    how_to_url: str | None = None,
) -> InlineKeyboardMarkup:
    """Кнопки после принятия предложения.

    Основная — «Передать»: ведёт на ``tg://send_gift?to=<username>``,
    то есть сразу в интерфейс передачи подарка. Кнопка намеренно
    оставлена без цвета и иконки: карточка уже показывает предпросмотр
    подарка, а лишнее оформление отвлекает от главного действия.
    Кнопка «Как передать» появляется только если заполнен ``how_to_url``.
    """
    row = [InlineKeyboardButton(text=lang.transfer, url=transfer_url)]
    if how_to_url:
        row.append(InlineKeyboardButton(text=lang.how_to_transfer, url=how_to_url))
    return InlineKeyboardMarkup(inline_keyboard=[row])