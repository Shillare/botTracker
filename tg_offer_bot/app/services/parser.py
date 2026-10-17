"""Парсер и валидатор вводных данных команды ``benz``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from app.constants import CURRENCY_ALIASES, DEFAULT_CURRENCY
from app.i18n import RU, Lang, resolve_lang
from app.utils.formatting import THOUSANDS_SEPARATOR
from app.utils.text import obfuscate

# t.me/nft/ViceCream-382938 | t.me/ViceCream-382938 | https://t.me/nft/StarNotepad-21211
_GIFT_LINK_RE = re.compile(
    r"^(?:https?://)?(?:t|telegram)\.me/(?:nft/)?"
    r"(?P<slug>[A-Za-z][A-Za-z0-9]{1,48})"
    r"(?:-(?P<number>\d{1,12}))?/?$",
    re.IGNORECASE,
)

# @username — необязательный получатель подарка для кнопки «Передать».
_RECIPIENT_RE = re.compile(r"^@(?P<username>[A-Za-z][A-Za-z0-9_]{3,31})$")

# «2500», «2 500», «2500 Stars», «2.5k», «1,2к», «1.5m»
_STARS_RE = re.compile(
    r"^(?P<number>\d[\d\s\u00a0\u202f.,']*)(?P<mult>[kкmм])?\s*(?:stars|star|звёзд|звезд|xtr)?$",
    re.IGNORECASE | re.UNICODE,
)

_MULTIPLIERS = {"k": 1_000, "к": 1_000, "m": 1_000_000, "м": 1_000_000}

MAX_STARS = 1_000_000_000


@dataclass(frozen=True, slots=True)
class GiftLink:
    """Разобранная ссылка на NFT-подарок."""

    slug: str
    number: int | None
    url: str
    lang: Lang = RU

    @property
    def display_title(self) -> str:
        """``StarNotepad-21211`` -> ``Ѕtar Νοtepad #21,211``.

        Название переводится в гомоглифы, чтобы не выделяться внутри
        предложения и не попадать под строковые фильтры Telegram.
        """
        words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", self.slug)
        words = re.sub(r"(?<=[A-Za-z])(?=[0-9])", " ", words)
        words = re.sub(r"\s+", " ", words).strip()
        # Омоглифы нужны только русской карточке: они обходят строковый
        # антиспам-фильтр Telegram. Английскому и арабскому читателю они
        # только мешают.
        words = obfuscate(words) if self.lang.code == "ru" else words
        if self.number is None:
            return words
        return f"{words} #{self.number:,}".replace(",", THOUSANDS_SEPARATOR)


@dataclass(frozen=True, slots=True)
class OfferArgs:
    """Аргументы команды ``benz`` после разбора строки."""

    gift_raw: str
    price_raw: str
    currency: str
    recipient: str | None = None
    language: str | None = None


class OfferParseError(ValueError):
    """Ошибка валидации аргументов команды ``benz``."""


def parse_currency(raw: str) -> str | None:
    """Нормализует название валюты. ``None`` — токен не похож на валюту."""
    key = raw.strip().strip(".").lower()
    if not key:
        return None
    return CURRENCY_ALIASES.get(key)


def normalize_username(raw: str) -> str | None:
    """``@User`` -> ``User``; ``None``, если это не похоже на username."""
    match = _RECIPIENT_RE.match(raw.strip())
    return match.group("username") if match else None


def parse_gift_link(raw: str, lang: Lang = RU) -> GiftLink:
    """Парсит и нормализует ссылку на подарок.

    :raises OfferParseError: если ссылка не соответствует формату ``t.me/nft/Name-123``.
    """
    candidate = raw.strip().rstrip(".,;:!?)(")
    match = _GIFT_LINK_RE.match(candidate)
    if match is None:
        raise OfferParseError(lang.err_bad_link)

    slug = match.group("slug")
    number_raw = match.group("number")
    number = int(number_raw) if number_raw else None
    suffix = f"-{number_raw}" if number_raw else ""
    url = f"https://t.me/nft/{slug}{suffix}"
    return GiftLink(slug=slug, number=number, url=url, lang=lang)


def _normalize_number(raw: str) -> Decimal:
    """Приводит пользовательское число к ``Decimal``.

    Работает с разделителями разрядов (``2 500``, ``2,500``) и десятичными
    (``2.5``, ``1,5``). Если после разделителя ровно три цифры, он трактуется
    как разделитель разрядов — это конвенция большинства локализаций.
    """
    cleaned = re.sub(r"[\s\u00a0\u202f']", "", raw)

    if "," in cleaned and "." in cleaned:
        decimal_sep = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        cleaned = cleaned.replace(thousands_sep, "").replace(decimal_sep, ".")
    else:
        sep = "," if "," in cleaned else ("." if "." in cleaned else None)
        if sep is not None:
            head, _, tail = cleaned.partition(sep)
            if not tail.isdigit():
                cleaned = head + tail
            elif len(tail) == 3:
                # 2.500 -> 2500 (разделитель разрядов)
                cleaned = head + tail
            else:
                # 2.5 -> 2.5 (десятичный разделитель)
                cleaned = f"{head}.{tail}"
    return Decimal(cleaned)


def parse_stars(raw: str, lang: Lang = RU) -> int:
    """Парсит цену в Stars: ``2500``, ``2 500``, ``2.5k``, ``1.2к``, ``1000 Stars``.

    :raises OfferParseError: если значение некорректно или вне допустимого диапазона.
    """
    candidate = raw.strip()
    match = _STARS_RE.match(candidate)
    if match is None:
        raise OfferParseError(lang.err_bad_price)

    try:
        number = _normalize_number(match.group("number"))
    except (InvalidOperation, ArithmeticError) as exc:
        raise OfferParseError(lang.err_price_not_number) from exc

    if not number.is_finite():
        raise OfferParseError(lang.err_price_not_finite)

    multiplier = _MULTIPLIERS.get((match.group("mult") or "").lower(), 1)
    value = int((number * multiplier).to_integral_value(rounding=ROUND_DOWN))

    if value <= 0:
        raise OfferParseError(lang.err_price_zero)
    if value > MAX_STARS:
        raise OfferParseError(lang.err_price_too_big)
    return value


def split_offer_args(text: str, lang: Lang = RU) -> OfferArgs:
    """Делит аргументы команды на ссылку, цену, валюту и получателя.

    Формат: ``<ссылка> <цена> [валюта] [@username] [язык]``.

    Валюта, ``@username`` и язык необязательны: без валюты подставляется Stars.
    Цена может содержать пробелы (``2 500``), поэтому токены собираются
    справа налево: сначала отрезаются валюта и получатель, затем цена.
    """
    tokens = text.strip().split()
    if not tokens:
        raise OfferParseError(lang.err_no_args)
    if len(tokens) == 1:
        raise OfferParseError(lang.err_no_price)

    gift_raw = tokens[0]
    rest = tokens[1:]

    recipient: str | None = None
    currency: str | None = None
    language: str | None = None

    # Необязательные хвостовые аргументы.
    tail: list[str] = []
    for token in rest:
        name = normalize_username(token)
        if name is not None:
            recipient = name
            continue
        name = parse_currency(token)
        if name is not None:
            currency = name
            continue
        code = resolve_lang(token)
        if code is not None:
            language = code
            continue
        tail.append(token)

    if not tail:
        raise OfferParseError(lang.err_no_price)
    if currency is None:
        # Цена могла прийти слипленной с валютой: «2500 Stars».
        currency = parse_currency(tail[-1])
        if currency is not None:
            tail.pop()

    return OfferArgs(
        gift_raw=gift_raw,
        price_raw=" ".join(tail),
        currency=currency or DEFAULT_CURRENCY,
        recipient=recipient,
        language=language,
    )
