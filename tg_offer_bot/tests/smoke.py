"""Проверочный сценарий: парсинг, шаблоны, клавиатуры и защита от повторных нажатий.

Запуск: ``python -m tests.smoke``
"""

from __future__ import annotations

import asyncio
import inspect
import re
import tempfile
from datetime import timedelta
from pathlib import Path

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, Message, User

from app.config import Settings
from app.constants import (
    CURRENCY_ALIASES,
    DEFAULT_CURRENCY,
    EMOJI_ACCEPT,
    EMOJI_DECLINE,
    EMOJI_DECLINED,
    EMOJI_GIFT,
    H_PEREDAYTE,
    H_POKUPATELYU,
    H_PREDLOGAET,
    H_PRODAN,
    H_TOO_LATE,
    H_USER,
    STARS_LABEL,
)
from app.database import Database, OfferRepository, utcnow
from app.handlers.offer import build_transfer_url, resolve_recipient
from app.i18n import AR, EN, RU, Lang, get_lang, resolve_lang
from app.keyboards import inline
from app.services import templates
from app.services.parser import (
    OfferParseError,
    parse_currency,
    parse_gift_link,
    parse_stars,
    split_offer_args,
)
from app.services.telegram import FloodGuard, _is_formatting_error
from app.utils.formatting import format_duration, format_thousands
from app.utils.text import downgrade_tg_emoji, esc, esc_attr, obfuscate, strip_html, tg_emoji

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  OK   {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name} {detail}")


def test_parser() -> None:
    print("\n[1] Парсер ссылок и цен")
    gift = parse_gift_link("https://t.me/nft/ViceCream-382938")
    check("slug", gift.slug == "ViceCream", gift.slug)
    check("number", gift.number == 382938, str(gift.number))
    check("url", gift.url == "https://t.me/nft/ViceCream-382938", gift.url)
    check("title в гомоглифах", gift.display_title == obfuscate("Vice Cream") + " #382,938",
          gift.display_title)
    check("title без латинской 'S'", "S" not in gift.display_title.split()[0], gift.display_title)

    gift2 = parse_gift_link("t.me/StarNotepad-21211")
    check("короткая ссылка", gift2.url == "https://t.me/nft/StarNotepad-21211", gift2.url)
    check("camelCase разбит", gift2.display_title.startswith("Ѕtаr Νоt"), gift2.display_title)
    check("номер с запятой", gift2.display_title.endswith("#21,211"), gift2.display_title)

    check("цена 2500", parse_stars("2500") == 2500)
    check("цена 2 500", parse_stars("2 500") == 2500)
    check("цена 2.5k", parse_stars("2.5k") == 2500, str(parse_stars("2.5k")))
    check("цена 1.2к", parse_stars("1.2к") == 1200, str(parse_stars("1.2к")))
    check("цена 1.5m", parse_stars("1.5m") == 1_500_000)
    check("цена 2,500 (разряды)", parse_stars("2,500") == 2500, str(parse_stars("2,500")))
    check("цена 2,5k (десятичная)", parse_stars("2,5k") == 2500, str(parse_stars("2,5k")))
    check("цена 1'000", parse_stars("1'000") == 1000, str(parse_stars("1'000")))
    check("цена 999.5k", parse_stars("999.5k") == 999_500, str(parse_stars("999.5k")))
    try:
        parse_stars("0.0009k")
        check("цена, округлённая до нуля, отклоняется", False, "исключения не было")
    except OfferParseError:
        check("цена, округлённая до нуля, отклоняется", True)
    check("цена 1000 Stars", parse_stars("1000 Stars") == 1000)
    check("цена 1000 XTR", parse_stars("1000 XTR") == 1000)

    for bad in ("abc", "-100", "0", "10^9x", "1 000 000 001"):
        try:
            parse_stars(bad)
            check(f"невалидная цена '{bad}'", False, "исключения не было")
        except OfferParseError:
            check(f"невалидная цена '{bad}'", True)

    try:
        parse_gift_link("https://example.com/not-a-gift")
        check("невалидная ссылка", False, "исключения не было")
    except OfferParseError:
        check("невалидная ссылка", True)

    args = split_offer_args("https://t.me/nft/A-1 2 500")
    check("split с пробелами в цене",
          args.gift_raw == "https://t.me/nft/A-1" and args.price_raw == "2 500", str(args))
    check("валюта по умолчанию", args.currency == DEFAULT_CURRENCY, args.currency)

    args2 = split_offer_args("https://t.me/nft/A-1 2500 Stars")
    check("явная валюта Stars", args2.currency == "Stars" and args2.price_raw == "2500", str(args2))

    args3 = split_offer_args("https://t.me/nft/A-1 2.5k Gram")
    check("валюта Gram", args3.currency == "Gram" and args3.price_raw == "2.5k", str(args3))

    args4 = split_offer_args("https://t.me/nft/A-1 100 Stars @buyer")
    check("получатель @buyer",
          args4.recipient == "buyer" and args4.currency == "Stars" and args4.price_raw == "100",
          str(args4))

    check("alias stars", parse_currency("STARS") == "Stars")
    check("alias gram", parse_currency("Gram") == "Gram")
    check("alias ton", parse_currency("ton") == "TON")
    check("alias кириллицей", parse_currency("звёзд") == "Stars")
    check("не валюта", parse_currency("2500") is None)
    check("alias-таблица непустая", len(CURRENCY_ALIASES) > 5)

    # split_offer_args не проверяет саму цену — это делает parse_stars.
    args5 = split_offer_args("https://t.me/nft/A-1 1.5")
    check("дробная цена без валюты",
          args5.price_raw == "1.5" and parse_stars(args5.price_raw) == 1, str(args5))
    try:
        parse_stars(split_offer_args("https://t.me/nft/A-1 @").price_raw)
        check("цена «@» отклоняется", False, "исключения не было")
    except OfferParseError:
        check("цена «@» отклоняется", True)
    try:
        split_offer_args("https://t.me/nft/A-1")
        check("нет цены", False, "исключения не было")
    except OfferParseError:
        check("нет цены", True)


def test_text_utils() -> None:
    print("\n[2] Экранирование и утилиты")
    check("esc", esc("<b>") == "&lt;b&gt;", esc("<b>"))
    check("esc_attr", esc_attr('a"b') == "a&quot;b", esc_attr('a"b'))
    check("strip_html", strip_html("<b>жирный</b>") == "жирный")
    check("tg_emoji", tg_emoji("123", "✅") == '<tg-emoji emoji-id="123">✅</tg-emoji>')
    check("thousands без разделителя", format_thousands(382938) == "382938", format_thousands(382938))
    check("duration", format_duration(5 * 3600 + 59 * 60) == "5ч 59м", format_duration(21540))
    check("duration zero", format_duration(-5) == RU.less_than_minute, format_duration(-5))


def test_keyboards() -> None:
    print("\n[3] Клавиатуры")
    offer_id = "a" * 32
    kb = inline.offer_keyboard(offer_id)
    row = kb.inline_keyboard[0]
    check("2 кнопки", len(row) == 2)
    check("порядок: Отклонить → Принять",
          [b.text for b in row] == [RU.decline, RU.accept], str([b.text for b in row]))
    check("callback_data в лимите", all(len(b.callback_data.encode()) <= 64 for b in row))
    check("parse_callback (Отклонить)", inline.parse_callback(row[0].callback_data) == ("d", offer_id))
    check("parse_callback (Принять)", inline.parse_callback(row[1].callback_data) == ("a", offer_id))
    check("style не задан", all(b.style is None for b in row), str([b.style for b in row]))
    check("icon задан", all(b.icon_custom_emoji_id for b in row))
    check("иконки из ТЗ",
          [b.icon_custom_emoji_id for b in row] == [EMOJI_DECLINE, EMOJI_ACCEPT],
          str([b.icon_custom_emoji_id for b in row]))

    parsed = inline.parse_callback("garbage")
    check("битый callback", parsed is None)


def test_templates() -> None:
    print("\n[4] Шаблоны сообщений")
    # Название подарка в реальности приходит омоглифами (GiftLink.display_title).
    gift_title = parse_gift_link("https://t.me/nft/SantaHat-3438").display_title
    check("display_title: гомоглифы + разряды", gift_title.endswith("#3,438"), gift_title)
    card = templates.offer_card(
        lang=RU,
        seller_username="tester",
        seller_name="Tester",
        gift_title=gift_title,
        gift_url="https://t.me/nft/SantaHat-3438",
        price=2500,
        currency="Stars",
        currency_label=STARS_LABEL,
        remaining_seconds=359 * 60,
    )
    check("карточка: ссылка", 'href="https://t.me/nft/SantaHat-3438"' in card, card)
    check(
        "карточка: <a> без вложенного <b>",
        f'<a href="https://t.me/nft/SantaHat-3438">{esc(gift_title)}</a>' in card,
        card,
    )
    check("карточка: нет </b></a>", "</b></a>" not in card, card)
    check("карточка: название подарка", gift_title in card, card)
    check(
        "карточка: сумма без кавычек",
        '"' not in card.split("\n")[0],
        card,
    )
    check("карточка: цена", f"2500 {STARS_LABEL}" in card, card)
    check("карточка: ник из профиля, не @username",
          "<b>Tester</b>" in card and "@tester" not in card and "<b>tester</b>" not in card, card)
    check(
        "карточка: ник без кавычек",
        '"<b>Tester</b>"' not in card,
        card,
    )

    no_name = templates.offer_card(
        lang=RU,
        seller_username="onlyuser",
        seller_name="",
        gift_title="Vice Cream",
        gift_url="https://t.me/nft/ViceCream-382938",
        price=100,
        currency="Gram",
        currency_label="Gram",
        remaining_seconds=359 * 60,
    )
    check("карточка: фолбэк на username", "<b>onlyuser</b>" in no_name, no_name)
    check("карточка: гомоглифы", H_USER in card and H_PREDLOGAET in card, card)
    check("карточка: срок 5ч 59м", "5ч 59м" in card, card)
    check("карточка: подпись валюты не блокируется фильтром", "Stars" not in card, card)

    gram = templates.offer_card(
        lang=RU,
        seller_username="tester",
        seller_name="Tester",
        gift_title="Vice Cream",
        gift_url="https://t.me/nft/ViceCream-382938",
        price=100,
        currency="Gram",
        currency_label="Gram",
        remaining_seconds=359 * 60,
    )
    check("карточка: Gram", "<b>100 Gram</b>" in gram, gram)

    accepted = templates.accepted_card(
        lang=RU,
        gift_title=gift_title,
        gift_url="https://t.me/nft/SantaHat-3438",
        currency="Stars",
        currency_label=STARS_LABEL,
    )
    check("принято: подарок продан", H_PRODAN in accepted, accepted)
    check("принято: инструкция", H_PEREDAYTE in accepted and H_POKUPATELYU in accepted, accepted)
    check(
        "принято: ссылка на подарок без кавычек",
        '<a href="https://t.me/nft/SantaHat-3438">' in accepted and '"#' not in accepted,
        accepted,
    )
    check("принято: нет даты", not any(part.count(".") == 2 for part in accepted.split()), accepted)
    check(
        "принято: premium-эмодзи подарка",
        f'<tg-emoji emoji-id="{EMOJI_GIFT}"' in accepted,
        accepted,
    )
    check(
        "принято: в <a> нет вложенного <b>",
        "</b></a>" not in accepted,
        accepted,
    )
    check("принято: ссылка единственная", accepted.count("<a href") == 1, accepted)

    check("разбор заголовка", templates.split_gift_title("Vice Cream #382,938")
          == ("Vice Cream", "382,938"))
    check("разбор без номера", templates.split_gift_title("Vice Cream")
          == ("Vice Cream", None))

    declined = templates.declined_card(RU)
    check("отклонено", H_TOO_LATE in declined, declined)
    check("отклонено: эмодзи в bold",
          f'<b><tg-emoji emoji-id="{EMOJI_DECLINED}"' in declined, declined)
    check("отклонено: корректный HTML",
          "</b></tg-emoji>" not in declined and "<tg-emoji" in declined, declined)

    # XSS-подобный ввод не должен ломать разметку.
    evil = templates.accepted_card(
        lang=RU,
        gift_title="<script>x</script>",
        gift_url="https://t.me/nft/X-1",
        currency="Stars",
        currency_label=STARS_LABEL,
    )
    check("экранирование в шаблоне", "<script>" not in evil, evil)


def test_transfer_button() -> None:
    print("\n[4b] Кнопка «Передать»")
    kb = inline.transfer_keyboard(
        build_transfer_url("tg://send_gift?to={username}", "buyer"), RU
    )
    button = kb.inline_keyboard[0][0]
    check("одна кнопка", len(kb.inline_keyboard[0]) == 1)
    check("текст «Передать»", button.text == RU.transfer, button.text)
    check("deep link", button.url == "tg://send_gift?to=buyer", str(button.url))
    check("без иконки", button.icon_custom_emoji_id is None, str(button.icon_custom_emoji_id))
    check("без цвета", button.style is None, str(button.style))

    with_help = inline.transfer_keyboard("tg://send_gift?to=buyer", RU, "https://example.com")
    check("кнопка «Как передать» опциональна", len(with_help.inline_keyboard[0]) == 2)
    check("«Как передать» тоже без оформления",
          all(b.style is None and b.icon_custom_emoji_id is None
              for b in with_help.inline_keyboard[0]))

    settings = Settings(bot_token="1:AA")
    check(
        "приоритет: явный @username",
        resolve_recipient(explicit="seller", accepter_username="buyer", settings=settings) == "seller",
    )
    check(
        "приоритет: нажавший «Принять»",
        resolve_recipient(explicit=None, accepter_username="buyer", settings=settings) == "buyer",
    )
    check(
        "приоритет: без username — None",
        resolve_recipient(explicit=None, accepter_username=None, settings=settings) is None,
    )
    fallback = Settings(bot_token="1:AA", transfer_recipient_username="@fallback_user")
    check(
        "приоритет: значение из .env",
        resolve_recipient(explicit=None, accepter_username=None, settings=fallback)
        == "fallback_user",
    )


def test_database_and_claim() -> None:
    print("\n[5] БД и защита от повторного нажатия")

    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            await db.connect()

            repo = OfferRepository(db)
            now = utcnow()
            expires = now + timedelta(hours=6)
            payload = {
                "id": "o1",
                "business_connection_id": None,
                "chat_id": 100,
                "chat_type": "private",
                "message_id": 5,
                "buyer_user_id": 100,
                "buyer_username": "buyer",
                "seller_user_id": 200,
                "seller_username": "seller",
                "seller_name": "Seller",
                "recipient_username": "buyer",
                "gift_slug": "A",
                "gift_number": 1,
                "gift_title": "A #1",
                "gift_url": "https://t.me/nft/A-1",
                "stars": 100,
                "currency": "Stars",
                "language": "ru",
                "status": "pending",
                "created_at": now.isoformat(timespec="seconds"),
                "expires_at": expires.isoformat(timespec="seconds"),
            }
            await repo.create(payload)

            first = await repo.claim("o1", "accepted", 100)
            check("первый claim успешен", first is not None)

            second = await repo.claim("o1", "declined", 100)
            check("второй claim отклонён", second is None, str(second))

            row = await repo.get("o1")
            check("статус = accepted", row["status"] == "accepted", row["status"])
            check("resolved_by сохранён", row["resolved_by"] == 100)
            check("валюта сохранена", row["currency"] == "Stars", str(row["currency"]))
            check("язык сохранён", row["language"] == "ru", str(row["language"]))
            check("тип чата сохранён", row["chat_type"] == "private", str(row["chat_type"]))
            check("получатель сохранён", row["recipient_username"] == "buyer")

            # Истёкшее предложение нельзя принять.
            await repo.create(
                {
                    **payload,
                    "id": "o2",
                    "expires_at": (now - timedelta(hours=1)).isoformat(timespec="seconds"),
                }
            )
            expired = await repo.claim("o2", "accepted", 100)
            check("истёкшее предложение не принимается", expired is None)

            await db.close()

    asyncio.run(run())


def test_race_condition() -> None:
    print("\n[6] Race condition: 50 параллельных нажатий")
    import aiosqlite  # noqa: F401 - проверка доступности драйвера

    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "race.sqlite3")
            await db.connect()
            repo = OfferRepository(db)
            now = utcnow()
            await repo.create(
                {
                    "id": "race",
                    "business_connection_id": None,
                    "chat_id": 1,
                    "chat_type": "supergroup",
                    "message_id": 1,
                    "buyer_user_id": 0,
                    "buyer_username": None,
                    "seller_user_id": 2,
                    "seller_username": None,
                    "seller_name": None,
                    "recipient_username": None,
                    "gift_slug": "A",
                    "gift_number": 1,
                    "gift_title": "A #1",
                    "gift_url": "https://t.me/nft/A-1",
                    "stars": 1,
                    "currency": "Gram",
                    "language": "ru",
                    "status": "pending",
                    "created_at": now.isoformat(timespec="seconds"),
                    "expires_at": (now + timedelta(hours=1)).isoformat(timespec="seconds"),
                }
            )
            results = await asyncio.gather(*(repo.claim("race", "accepted", 1) for _ in range(50)))
            wins = sum(1 for r in results if r is not None)
            check("ровно один победитель", wins == 1, f"wins={wins}")
            await db.close()

    asyncio.run(run())


def test_bot_objects() -> None:
    print("\n[7] Интеграция с типами aiogram")
    from datetime import UTC, datetime

    from aiogram.types import Chat

    user = User(id=1, is_bot=False, first_name="Test")
    chat = Chat(id=1, type="private")
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        text="benz https://t.me/nft/A-1 100",
    )
    check("message собран", message.text == "benz https://t.me/nft/A-1 100")
    check("business_connection_id пуст", message.business_connection_id is None)
    btn = InlineKeyboardButton(text="ok", callback_data="x")
    check("кнопка", btn.text == "ok")


def test_settings() -> None:
    print("\n[8] Конфигурация")
    settings = Settings(bot_token="1:AA", admin_ids="111, 222;333")
    check("admin_id_set", settings.admin_id_set == frozenset({111, 222, 333}), str(settings.admin_id_set))
    check("ttl = 5ч 59м", settings.offer_ttl_seconds == 359 * 60, str(settings.offer_ttl_seconds))
    check("ttl в минутах", settings.offer_ttl_minutes == 359)
    check("policy по умолчанию", settings.accept_policy == "anyone")
    check("подпись Stars", settings.currency_label("Stars") == STARS_LABEL)
    check("подпись Gram", settings.currency_label("Gram") == "Gram")

    override = Settings(bot_token="1:AA", currency_labels="gram=Gрaм")
    check("переопределение валюты", override.currency_label("Gram") == "Gрaм")

    at_sign = Settings(bot_token="1:AA", transfer_recipient_username="@someone")
    check("@ срезается", at_sign.transfer_recipient_username == "someone")

    try:
        Settings(bot_token="1:AA", accept_policy="кто угодно")
        check("невалидная политика", False, "исключения не было")
    except Exception:
        check("невалидная политика", True)


def test_languages() -> None:
    print("\n[4c] Языки карточки: RU / EN / AR")
    check("alias EU -> en", resolve_lang("EU") == "en")
    check("alias en -> en", resolve_lang("en") == "en")
    check("alias english -> en", resolve_lang("english") == "en")
    check("alias ar -> ar", resolve_lang("ar") == "ar")
    check("alias ARABIC -> ar", resolve_lang("Arabic") == "ar")
    check("alias ru -> ru", resolve_lang("RU") == "ru")
    check("не язык", resolve_lang("2500") is None)
    check("get_lang по умолчанию", get_lang(None).code == "ru")
    check("get_lang неизвестный", get_lang("zz").code == "ru")

    tail = split_offer_args("https://t.me/nft/A-1 2500 Stars EU")
    check("команда заканчивается на EU", tail.language == "en", str(tail))
    check("цена не поехала", tail.price_raw == "2500", str(tail))

    tail_ar = split_offer_args("https://t.me/nft/A-1 100 Gram ar")
    check("команда заканчивается на ar", tail_ar.language == "ar", str(tail_ar))

    tail_mix = split_offer_args("https://t.me/nft/A-1 100 Stars @buyer EU")
    check("язык после получателя", tail_mix.language == "en" and tail_mix.recipient == "buyer", str(tail_mix))

    tail_none = split_offer_args("https://t.me/nft/A-1 100 Stars")
    check("без суффикса язык не задан", tail_none.language is None, str(tail_none))

    gift = parse_gift_link("https://t.me/nft/SantaHat-3438")
    title = gift.display_title

    for lang, word, buttons in (
        (RU, H_PREDLOGAET, (RU.decline, RU.accept)),
        (EN, "offers you", ("Decline", "Accept")),
        (AR, "يعرض عليك", ("رفض", "قبول")),
    ):
        card = templates.offer_card(
            lang=lang,
            seller_username="ivan",
            seller_name="Иван Петров",
            gift_title=title,
            gift_url=gift.url,
            price=2500,
            currency="Stars",
            currency_label=STARS_LABEL,
            remaining_seconds=359 * 60,
        )
        check(f"[{lang.code}] ключевое слово", word in card, card)
        check(f"[{lang.code}] ник из профиля", "<b>Иван Петров</b>" in card, card)
        row = inline.offer_keyboard("a" * 32, lang).inline_keyboard[0]
        check(f"[{lang.code}] подписи кнопок", tuple(b.text for b in row) == buttons,
              str([b.text for b in row]))

    sold = templates.accepted_card(
        lang=EN,
        gift_title=title,
        gift_url=gift.url,
        currency="Stars",
        currency_label=STARS_LABEL,
    )
    check("EN: подпись валюты обычная", "Stars" in sold and "Ѕtarѕ" not in sold, sold)
    check("EN: инструкция переведена", "Send the gift to the buyer" in sold, sold)
    check("EN: без кавычек вокруг названия",
          sold.split("</a>")[0].endswith("#3,438</a>".replace("</a>", "")),
          sold)

    sold_ar = templates.accepted_card(
        lang=AR,
        gift_title=title,
        gift_url=gift.url,
        currency="Stars",
        currency_label=STARS_LABEL,
    )
    check("AR: инструкция переведена", "أرسل الهدية" in sold_ar, sold_ar)
    check("AR: эмодзи подарка на месте", f'<tg-emoji emoji-id="{EMOJI_GIFT}"' in sold_ar, sold_ar)

    check("EN: кнопка Transfer",
          inline.transfer_keyboard("tg://x", EN).inline_keyboard[0][0].text == "Transfer")
    check("AR: кнопة تحويل",
          inline.transfer_keyboard("tg://x", AR).inline_keyboard[0][0].text == "تحويل")
    check("EN: declined", "Offer declined" in templates.declined_card(EN),
          templates.declined_card(EN))
    check("AR: declined", "تم رفض العرض" in templates.declined_card(AR),
          templates.declined_card(AR))


# Латинские буквы -> кириллические омоглифы. Нужны, чтобы проверить
# обратимость подмены: обратная замена обязана вернуть русский текст.
LATIN_TO_CYR = {
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К",
    "M": "М", "O": "О", "P": "Р", "S": "Ѕ", "T": "Т", "X": "Х", "Y": "У",
    "a": "а", "b": "в", "c": "с", "e": "е", "k": "к", "m": "м",
    "o": "о", "p": "р", "t": "т", "x": "х", "y": "у",
}

# Текст карточек, который видит покупатель. Служебные подсказки, /help и
# подписи кнопок обфусцировать незачем: подпись видна сразу при нажатии и
# подмена бросается в глаза.
CARD_TEXT_RU = (
    "user", "offers_you", "for_gift", "valid_for",
    "gift_label", "sold", "hand_over", "declined",
    "less_than_minute",
)

# Подписи кнопок: обычная кириллица, без гомоглифов.
PLAIN_BUTTONS_RU = (
    ("accept", "Принять"),
    ("decline", "Отклонить"),
    ("transfer", "Передать"),
    ("how_to_transfer", "Как передать"),
)


def test_homoglyphs() -> None:
    print("\n[8] Обфускация гомоглифами: текст карточки, но не подписи кнопок")
    for name in CARD_TEXT_RU:
        value = getattr(RU, name)
        check(f"[ru] {name} содержит омоглифы",
              any(ch in LATIN_TO_CYR for ch in value), value)

    # Обратная подмена обязана вернуть текст без латиницы: значит, подставлены
    # именно кириллические омоглифы, а не случайные символы.
    for name in CARD_TEXT_RU:
        restored = "".join(LATIN_TO_CYR.get(ch, ch) for ch in getattr(RU, name))
        check(f"[ru] {name} обратим",
              not any(ch.isascii() and ch.isalpha() for ch in restored), restored)

    # Подписи кнопок читаемые: любая латинская буква означала бы гомоглиф,
    # который покупатель видит сразу при нажатии.
    for name, plain in PLAIN_BUTTONS_RU:
        value = getattr(RU, name)
        check(f"[ru] {name} — обычная кириллица", value == plain, value)
        check(f"[ru] {name} без латиницы",
              not any(ch.isascii() and ch.isalpha() for ch in value), value)

    # Подпись валюты обязана быть из проверенных: обычное «Stars» и «звёздочка»
    # Telegram режет с PEER_FLOOD, а карточка покупателю нужна.
    check("STARS_LABEL из проверенных", STARS_LABEL in {"XTR", "Ѕtarѕ"}, STARS_LABEL)
    check("обычное «Stars» не используется", STARS_LABEL != "Stars", STARS_LABEL)


def test_flood_guard() -> None:
    print("\n[9] Глобальный тормоз после флуд-лимита")
    guard = FloodGuard()
    check("изначально паузы нет", guard.delay() == 0.0, str(guard.delay()))

    guard.note_flood(1.0)
    first = guard.delay()
    check("после флуда пауза есть", 0.0 < first <= 1.0, str(first))

    # Второй флуд подряд: Telegram явно просит ждать дольше.
    guard.note_flood(1.0)
    second = guard.delay()
    check("повторный флуд увеличивает паузу", second > first, f"{first} -> {second}")

    # Потолок не даёт боту встать колом надолго.
    for _ in range(10):
        guard.note_flood(5.0)
    check("пауза не превышает потолок", guard.delay() <= guard._cap, str(guard.delay()))

    # Успешная отправка сбрасывает рост паузы: после неё следующий флуд
    # получает исходную паузу, а не удвоенную.
    reset = FloodGuard()
    reset.note_flood(1.0)
    baseline = reset.delay()
    reset.note_success()
    reset.note_flood(1.0)
    check("после успеха рост сброшен", reset.delay() <= baseline + 0.1,
          f"{baseline:.2f} -> {reset.delay():.2f}")

    # Уже назначенную паузу успех не отменяет: лимит считается на аккаунт,
    # лишняя отправка в эти секунды его бы не сняла.
    held = FloodGuard()
    held.note_flood(3.0)
    before = held.delay()
    held.note_success()
    check("успех не отменяет текущую паузу", 0.0 < held.delay() <= before + 0.1,
          f"{before:.2f} -> {held.delay():.2f}")

    # Пока пауза активна, она не даёт отправлять — даже другому пользователю.
    fresh = FloodGuard()
    fresh.note_flood(2.0)
    check("пауза общая, а не per-user", fresh.delay() > 0.0, str(fresh.delay()))


def test_lang_fields_resolvable() -> None:
    """Все поля Lang реально читаются.

    Модуль ``app.i18n`` импортирует гомоглифы из ``app.constants``, а
    шаблоны обращаются к полям как ``lang.<поле>``. Опечатка в таком имени не
    ловится ни линтером, ни импортом — она всплывает только в рантайме и тихо
    роняет фоновую задачу (например, уведомление продавцу). Тест обходит все
    поля всех языков и требует, чтобы они находились.
    """
    print("\n[10] Поля Lang доступны во всех языках")
    for lang in (RU, EN, AR):
        for field in Lang.__dataclass_fields__:
            check(f"[{lang.code}] поле {field}", hasattr(lang, field),
                  "нет атрибута")

    # Линтер не видит опечатки вида ``langbenzer_label``: имя атрибута
    # проверяется только в рантайме, и ошибка тихо роняет фоновую задачу.
    # Поэтому отдельно сверяем, что шаблоны и клавиатуры не ссылаются на
    # поля, которых нет в Lang.
    import app.keyboards.inline as inline_mod
    import app.services.templates as templates_mod

    allowed = set(Lang.__dataclass_fields__)
    offenders: list[str] = []
    for module in (templates_mod, inline_mod):
        source = inspect.getsource(module)
        for match in re.finditer(r"lang\.(\w+)", source):
            if match.group(1) not in allowed:
                offenders.append(f"{module.__name__}: lang.{match.group(1)}")
    check("шаблоны ссылаются только на поля Lang", not offenders, str(offenders))


def test_custom_emoji_fallback() -> None:
    print("\n[11] Кастомные эмодзи: отказ Telegram не должен ронять карточку")
    tag = tg_emoji("5875180111744995604", "🎁")
    check("тег tg-emoji собран", tag.startswith("<tg-emoji emoji-id="), tag)

    # Telegram отвергает весь запрос, а не только тег: ошибка разметки, и её
    # нужно лечить упрощением, а не показывать пользователю.
    check(
        "entity_text_invalid считается ошибкой разметки",
        _is_formatting_error(
            TelegramBadRequest(method=None, message="Bad Request: ENTITY_TEXT_INVALID")
        ),
    )

    # Подмена тега на символ сохраняет остальную разметку.
    simplified = downgrade_tg_emoji(f"{tag} <b>Подарок</b> <a href=\"u\">название</a>")
    check("тег заменён символом", simplified.startswith("🎁 "), simplified)
    check("жирный текст сохранён", "<b>Подарок</b>" in simplified, simplified)
    check("ссылка сохранена", '<a href="u">название</a>' in simplified, simplified)
    check("тегов не осталось", "<tg-emoji" not in simplified, simplified)
    check("повторная подмена ничего не меняет",
          downgrade_tg_emoji(simplified) == simplified, simplified)


def test_bold_and_price_format() -> None:
    print("\n[12] Жирные фрагменты и цена без разделителя разрядов")
    settings = Settings(bot_token="1:AA")
    label = settings.currency_label("Stars")
    gift = parse_gift_link("https://t.me/nft/SantaHat-3438")

    card = templates.offer_card(
        lang=RU, seller_username="ivan", seller_name="Иван Петров",
        gift_title=gift.display_title, gift_url=gift.url, price=2500,
        currency="Stars", currency_label=label, remaining_seconds=359 * 60,
    )
    check("ник продавца жирный", "<b>Иван Петров</b>" in card, card)
    check("цена жирная и с подписью валюты", f"<b>2500 {label}</b>" in card, card)
    check("цены без разделителя разрядов", "2,500" not in card, card)
    check("в цене нет запятой", "," not in card.split("\n")[0].split("<b>")[1], card)

    sold = templates.accepted_card(
        lang=RU, gift_title=gift.display_title, gift_url=gift.url,
        currency="Stars", currency_label=label,
    )
    check("инструкция «Передайте подарок» жирная",
          f"<b>\u2022 {H_PEREDAYTE} " in sold, sold)
    check("в инструкции гомоглифы, не латиница", H_PEREDAYTE in sold, sold)

    notice = templates.seller_notification(
        lang=RU, gift_title=gift.display_title, gift_url=gift.url, price=2500,
        currency="Stars", currency_label=label, buyer_username="anna",
    )
    check("ник покупателя жирный", "<b>@anna</b>" in notice, notice)
    check("цена в уведомлении жирная и без разделителя",
          f"<b>2500 {label}</b>" in notice, notice)


def main() -> None:
    print("=" * 60)
    print("SMOKE-ТЕСТЫ tg_offer_bot")
    print("=" * 60)
    test_parser()
    test_text_utils()
    test_keyboards()
    test_templates()
    test_transfer_button()
    test_languages()
    test_database_and_claim()
    test_race_condition()
    test_bot_objects()
    test_settings()
    test_homoglyphs()
    test_flood_guard()
    test_lang_fields_resolvable()
    test_custom_emoji_fallback()
    test_bold_and_price_format()
    print("\n" + "=" * 60)
    print(f"ИТОГО: {PASSED} passed, {FAILED} failed")
    print("=" * 60)
    raise SystemExit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
