"""Форматирование чисел и времени.

Длительность локализована: «5ч 59м», «5h 59m», «5 س 59 د».
"""

from __future__ import annotations

from datetime import datetime

from app.i18n import RU, Lang

# Разделитель разрядов: запятая — так же, как Telegram показывает номера
# подарков (Star Notepad #21,211).
THOUSANDS_SEPARATOR = ","


def format_thousands(value: int) -> str:
    """Цена без разделителя разрядов: ``2500``, а не ``2,500``.

    Разделитель в цене визуально неотличим от номера подарка, а в карточке
    рядом стоит «#94,942» — покупатель читает цену как часть названия. Номер
    подарка разделитель сохраняет: так его показывает сам Telegram
    (``THOUSANDS_SEPARATOR``).
    """
    return str(int(value))


# Суффиксы единиц времени по языкам: (дни, часы, минуты).
_DURATION_UNITS: dict[str, tuple[str, str, str]] = {
    "ru": ("д", "ч", "м"),
    "en": ("d", "h", "m"),
    "ar": ("ي", "س", "د"),
}


def format_duration(seconds: int, lang: Lang = RU) -> str:
    """Компактный остаток времени на языке карточки.

    ``5ч 59м`` / ``5h 59m`` / ``5 س 59 د``
    """
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    day_unit, hour_unit, minute_unit = _DURATION_UNITS.get(lang.code, _DURATION_UNITS["ru"])

    if days:
        return f"{days}{day_unit} {hours}{hour_unit}"
    if hours:
        return f"{hours}{hour_unit} {minutes}{minute_unit}" if minutes else f"{hours}{hour_unit}"
    if minutes:
        return f"{minutes}{minute_unit}"
    return lang.less_than_minute


def expires_in_seconds(expires_at: datetime, now: datetime) -> int:
    return int((expires_at - now).total_seconds())