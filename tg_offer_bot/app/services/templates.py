"""Шаблоны текстов сообщений (HTML parse mode).

Тексты приходят из :mod:`app.i18n` — русские собраны из гомоглифов
(`app/constants.py`, префикс `H_*`), английский и арабский набраны обычным текстом.

Жирным выделено то, на что смотрят в первую очередь: ник продавца, цена с
подписью валюты и срок действия; в уведомлении
продавцу — ник покупателя и цена. Цена без разделителя разрядов: `2500`, а `2,500`.
"""


from __future__ import annotations

from app.constants import EMOJI_ACCEPTED, EMOJI_DECLINED, EMOJI_GIFT
from app.i18n import RU, Lang
from app.utils.formatting import THOUSANDS_SEPARATOR, format_duration, format_thousands
from app.utils.text import esc, esc_attr, tg_emoji

# В русском варианте подпись валюты берётся из настроек (омоглифы обходят
# антиспам), в английском и арабском — обычное имя валюты.
_OBFUSCATED_LANGUAGES = frozenset({RU.code})


def currency_text(currency: str, label: str, lang: Lang) -> str:
    """Подпись валюты для конкретного языка."""
    if lang.code in _OBFUSCATED_LANGUAGES:
        return label
    return currency


def offer_card(
    *,
    lang: Lang,
    seller_name: str,
    seller_username: str | None,
    gift_title: str,
    gift_url: str,
    price: int,
    currency_label: str,
    currency: str,
    remaining_seconds: int,
) -> str:
    """Карточка нового предложения.

    Ник из профиля (``first_name last_name``), а не ``@username``: в тексте
    объявления юзернейм выглядит как служебная метка. Ник и сумма выводятся
    без кавычек — разделяет их сама конструкция «предлагает Вам … за подарок».
    Если у профиля нет имени — откатываемся на username.

    Название подарка — гиперссылка на его страницу, без вложенного ``<b>``:
    разметка внутри ``<a>`` избыточна, а лишние теги попадают в текст ссылки
    у клиентов без поддержки HTML.
    """
    name = seller_name or seller_username or lang.user
    amount = f"{format_thousands(price)} {currency_text(currency, currency_label, lang)}"
    return (
        f"{lang.user} <b>{esc(name)}</b> {lang.offers_you} "
        f"<b>{esc(amount)}</b> {lang.for_gift}\n"
        f'<a href="{esc_attr(gift_url)}">{esc(gift_title)}</a>\n\n'
        f"{lang.valid_for} <b>{esc(format_duration(remaining_seconds, lang))}.</b>"
    )


def split_gift_title(title: str) -> tuple[str, str | None]:
    """``Vice Cream #382,938`` -> ``("Vice Cream", "382,938")``.

    Номер подарка хранится в самом заголовке (его формирует
    ``GiftLink.display_title``), поэтому разбирать его нужно в одном месте —
    иначе легко получить «Название #123 #123» в карточке «продан».
    """
    head, separator, tail = title.rpartition(" #")
    # Номер печатается с разделителем разрядов: «#382,938» — это всё ещё число.
    digits = tail.lstrip("#").replace(THOUSANDS_SEPARATOR, "").replace(" ", "")
    if not separator or not digits.isdigit():
        return title, None
    return head, tail.lstrip("#")


def accepted_card(
    *,
    lang: Lang,
    gift_title: str,
    gift_url: str,
    currency_label: str,
    currency: str,
) -> str:
    """Карточка после принятия предложения.

    ``🎁 Подарок: Santa Hat #3,438 успешно продан!``
    ``• Передайте подарок покупателю для зачисления Stars.``

    Строка-инструкция жирная: это единственное, что продавец обязан сделать
    дальше, и на мобильном её нельзя пропустить.

    Название с номером — единственная ссылка в сообщении, поэтому Telegram
    рисует предпросмотр подарка. Позицию предпросмотра (сверху) выставляет
    обработчик через ``link_preview_options``.
    """
    name, number = split_gift_title(gift_title)
    number_text = number if number is not None else "—"
    money = currency_text(currency, currency_label, lang)
    return (
        f"{tg_emoji(EMOJI_GIFT, '🎁')} {lang.gift_label}: "
        f'<a href="{esc_attr(gift_url)}">{esc(name)} #{esc(number_text)}</a> {lang.sold}!\n\n'
        f"<b>• {lang.hand_over} {esc(money)}.</b>"
    )


def declined_card(lang: Lang) -> str:
    """Карточка после отклонения предложения."""
    return f"<b>{tg_emoji(EMOJI_DECLINED, '❌')} {esc(lang.declined)}</b>"


def seller_notification(
    *,
    lang: Lang,
    gift_title: str,
    gift_url: str,
    price: int,
    currency_label: str,
    currency: str,
    buyer_username: str | None,
) -> str:
    """Уведомление продавцу о том, что его предложение приняли."""
    buyer = f"@{buyer_username}" if buyer_username else lang.buyer_label
    money = currency_text(currency, currency_label, lang)
    # Ник покупателя и цена — самое важное в этом сообщении, поэтому жирные:
    # по нику продавец сразу узнаёт, кому отвечать.
    return (
        f"{lang.notice_title}\n\n"
        f'{lang.gift_label}: <a href="{esc_attr(gift_url)}">{esc(gift_title)}</a>\n'
        f"{lang.buyer_label}: <b>{esc(buyer)}</b>\n"
        f"{lang.price_label}: <b>{format_thousands(price)} {esc(money)}</b>"
    )


__all__ = (
    "EMOJI_ACCEPTED",
    "accepted_card",
    "currency_text",
    "declined_card",
    "offer_card",
    "seller_notification",
    "split_gift_title",
)