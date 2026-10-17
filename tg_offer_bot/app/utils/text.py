"""Работа с HTML-разметкой Telegram и custom (premium) эмодзи."""

from __future__ import annotations

import html
import logging
import re

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")

# Латинская буква -> кириллический омоглиф. Выглядит идентично, но для
# строковых фильтров Telegram это другой символ.
# Ключи обязаны быть порядковыми: str.translate игнорирует dict со строковыми.
# Буквы t, r, d намеренно не заменяются — так текст ближе к естественному.
HOMOGLYPH_PAIRS: tuple[tuple[str, str], ...] = (
    ("S", "Ѕ"),  # ѕ-подобная
    ("N", "Ν"),
    ("a", "а"),
    ("c", "с"),
    ("e", "е"),
    ("h", "н"),
    ("k", "к"),
    ("m", "м"),
    ("n", "п"),
    ("o", "о"),
    ("p", "р"),
    ("x", "х"),
    ("y", "у"),
    ("b", "в"),
)

HOMOGLYPH_MAP: dict[int, str] = {ord(src): dst for src, dst in HOMOGLYPH_PAIRS}


def obfuscate(text: str) -> str:
    """Заменяет латинские буквы на кириллические омоглифы.

    Нужна, чтобы валюта и названия в карточке не совпадали со строковыми
    фильтрами Telegram (см. раздел README про PEER_FLOOD).
    """
    return text.translate(HOMOGLYPH_MAP)


def esc(value: object) -> str:
    """Экранирует текст для безопасной вставки в HTML Telegram."""
    return html.escape(str(value), quote=False)


def esc_attr(value: object) -> str:
    """Экранирует значение для вставки в HTML-атрибут (например, ``href``)."""
    return html.escape(str(value), quote=True)


def strip_html(value: str) -> str:
    """Убирает теги — для отправки текста без parse_mode."""
    return html.unescape(_TAG_RE.sub("", value))


_TG_EMOJI_RE = re.compile(r"<tg-emoji\b[^>]*>(.*?)</tg-emoji>", re.DOTALL)


def downgrade_tg_emoji(text: str) -> str:
    """Заменяет ``<tg-emoji emoji-id="...">X</tg-emoji>`` на обычный символ ``X``.

    Нужна, когда Telegram отверг кастомные эмодзи. Сохраняет всю остальную
    разметку — жирный текст и ссылки, — в отличие от отказа от ``parse_mode``
    целиком. Вызывается только после повторной неудачи: разовый сбой на стороне
    Telegram не должен навсегда лишать бота премиум-эмодзи.
    """
    return _TG_EMOJI_RE.sub(r"\1", text)


def tg_emoji(emoji_id: str, fallback: str) -> str:
    """Собирает тег custom emoji с fallback-символом.

    Telegram отдаёт клиенту ``fallback`` (например ``🎁``) для клиентов без
    поддержки premium-эмодзи, поэтому fallback всегда обязателен. Если Telegram
    отвергнет такой тег, шлюз заменит его на ``fallback`` через
    :func:`downgrade_tg_emoji`.
    """
    return f'<tg-emoji emoji-id="{esc_attr(emoji_id)}">{esc(fallback)}</tg-emoji>'


def build_url(url: str) -> str:
    """Нормализует пользовательскую ссылку до схемы https."""
    raw = url.strip()
    if raw.startswith(("http://", "https://", "tg://")):
        return raw
    if raw.startswith("t.me/") or raw.startswith("telegram.me/"):
        return f"https://{raw}"
    return f"https://{raw.lstrip('/')}"


def user_mention(user_id: int, username: str | None, fallback: str) -> str:
    """Ссылка на пользователя: ``tg://user?id=`` работает даже без username.

    Username выводится без символа ``@``.
    """
    if username:
        return f'<a href="tg://user?id={user_id}"><b>{esc(username)}</b></a>'
    return f'<a href="tg://user?id={user_id}"><b>{esc(fallback)}</b></a>'
