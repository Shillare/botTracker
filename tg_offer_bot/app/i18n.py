"""Языки интерфейса: русский, английский, арабский.

Язык выбирается суффиксом в конце команды ``benz``:

    ``benz https://t.me/nft/StarNotepad-21211 2500 Stars``  -> русский
    ``benz https://t.me/nft/StarNotepad-21211 2500 Stars EU`` -> английский
    ``benz https://t.me/nft/StarNotepad-21211 2500 Stars ar`` -> арабский

Русские строки собираются из констант ``H_*`` (гомоглифы) — они обходят
строковый антиспам-фильтр Telegram. Английский и арабский набраны обычным
текстом: подменять буквы в латинице и арабице незачем.

Подписи кнопок — исключение: они остаются обычной кириллицей. В отличие от
текста карточки подпись видна сразу при нажатии, и подмена бросается в глаза
(«Пpиняtь» вместо «Принять»), а на антиспам-фильтр подпись кнопки не влияет.

Кнопки, тосты и ответы обратной связи тоже переведены, потому что их видит
покупатель, а не только продавец.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.constants import (
    H_DEYSTVUET,
    H_DLYA_ZACHISLENIYA,
    H_ESHCHO,
    H_MENSHE_MINUTY,
    H_PEREDAYTE,
    H_PODAROK,
    H_PODAROK_CAP,
    H_POKUPATELYU,
    H_PREDLOGAET,
    H_PREDLOZHENIE,
    H_PRODAN,
    H_TOO_LATE,
    H_USER,
    H_VAM,
    H_ZA,
)


@dataclass(frozen=True, slots=True)
class Lang:
    """Набор строк одного языка.

    Поля разбиты по месту использования: карточка предложения, карточка
    «продан», кнопки, ответы на нажатия, уведомление продавцу и ошибки
    разбора команды.
    """

    code: str

    # --- Карточка предложения ---
    user: str
    offers_you: str
    for_gift: str
    valid_for: str

    # --- Карточка «продан» ---
    less_than_minute: str
    gift_label: str
    sold: str
    hand_over: str
    declined: str

    # --- Кнопки ---
    accept: str
    decline: str
    transfer: str
    how_to_transfer: str

    # --- Ответы на нажатия ---
    toast_sold: str
    toast_declined: str
    own_offer: str
    not_your_offer: str
    already_resolved: str
    expired: str
    not_found: str
    edit_failed: str

    # --- Уведомление продавцу ---
    notice_title: str
    buyer_label: str
    price_label: str

    # --- Подсказки при сбое отправки ---
    hint_send_failed: str
    hint_retry: str
    hint_flood: str
    hint_chat: str
    hint_rights: str
    hint_business_peer: str
    hint_connection: str
    hint_generic: str

    # --- Ошибки разбора команды ---
    err_no_args: str
    err_no_price: str
    err_bad_link: str
    err_bad_price: str
    err_price_not_number: str
    err_price_not_finite: str
    err_price_zero: str
    err_price_too_big: str

    # --- Подсказка по формату ---
    usage: str


RU = Lang(
    code="ru",
    user=H_USER,
    offers_you=f"{H_PREDLOGAET} {H_VAM}",
    for_gift=f"{H_ZA} {H_PODAROK}",
    valid_for=f"{H_PREDLOZHENIE} {H_DEYSTVUET} {H_ESHCHO}",
    less_than_minute=H_MENSHE_MINUTY,
    gift_label=H_PODAROK_CAP,
    sold=H_PRODAN,
    hand_over=f"{H_PEREDAYTE} {H_PODAROK} {H_POKUPATELYU} {H_DLYA_ZACHISLENIYA}",
    declined=H_TOO_LATE,
    accept="Принять",
    decline="Отклонить",
    transfer="Передать",
    how_to_transfer="Как передать",
    toast_sold="✅ Подарок успешно продан",
    toast_declined="Предложение отклонено",
    own_offer=(
        "Это ваше предложение.\n"
        "Кнопки «Принять» и «Отклонить» работают у покупателя."
    ),
    not_your_offer="Это предложение адресовано другому пользователю.",
    already_resolved="Это предложение уже обработано.",
    expired="Срок действия предложения истёк.",
    not_found="Предложение не найдено.",
    edit_failed="Не удалось обновить сообщение",
    hint_send_failed="Не удалось отправить предложение.",
    hint_retry="Telegram временно ограничил отправку. Повторите через <b>{seconds} с</b>.",
    hint_flood=(
        "Telegram временно ограничил отправку, бот уже повторил попытку. "
        "Подождите минуту и отправьте команду ещё раз."
    ),
    hint_chat=(
        "Этот пользователь ещё не писал боту напрямую, поэтому Telegram не "
        "разрешает писать ему от имени бота. Пусть он один раз напишет боту "
        "<code>/start</code> — либо отправьте команду в диалоге с бизнес-аккаунтом."
    ),
    hint_rights=(
        "Недостаточно прав. При подключении бизнес-аккаунта выдайте боту права "
        "«Отправлять сообщения» и «Удалять все сообщения»."
    ),
    hint_business_peer=(
        "Telegram не разрешил боту написать в этот чат от имени бизнес-аккаунта. "
        "Команда отправлена верно — ограничение на стороне аккаунта:\n"
        "• при подключении бота не выдано право «Разрешить отвечать на сообщения» "
        "— переподключите бота в Telegram → Настройки → Бизнес и отметьте его;\n"
        "• в этом диалоге не было входящих сообщений последние 24 часа: Telegram "
        "разрешает боту писать от имени аккаунта только в активные личные чаты."
    ),
    hint_connection=(
        "Бизнес-подключение недоступно. Проверьте, что бот подключён к аккаунту "
        "и не отключён от него."
    ),
    hint_generic="Техническая ошибка Telegram. Попробуйте ещё раз через минуту.",
    notice_title="✅ Ваше предложение приняли!",
    buyer_label="Покупатель",
    price_label="Цена",
    err_no_args="Укажите ссылку на подарок, цену и валюту.",
    err_no_price="Укажите цену.",
    err_bad_link=(
        "Не удалось распознать ссылку на подарок.\n"
        "Поддерживаемый формат: <code>https://t.me/nft/StarNotepad-21211</code>"
    ),
    err_bad_price=(
        "Не удалось распознать цену.\n"
        "Поддерживаемый формат: <code>2500</code>, <code>2 500</code>, "
        "<code>2.5k</code>, <code>1000 Stars</code>"
    ),
    err_price_not_number="Цена должна быть числом.",
    err_price_not_finite="Цена должна быть конечным числом.",
    err_price_zero="Цена должна быть больше нуля.",
    err_price_too_big="Слишком большая цена.",
    usage=(
        "<b>Формат команды:</b>\n"
        "<code>benz &lt;ссылка на NFT&gt; &lt;цена&gt; &lt;валюта&gt; [язык]</code>\n\n"
        "<b>Примеры:</b>\n"
        "<code>benz https://t.me/nft/StarNotepad-21211 2500 Stars</code>\n"
        "<code>benz https://t.me/nft/StarNotepad-21211 2.5k Gram</code>\n\n"
        "<b>Валюты:</b> <code>Stars</code>, <code>Gram</code>, <code>TON</code>, "
        "<code>USDT</code>.\n"
        "<b>Цена:</b> <code>2500</code>, <code>2 500</code>, <code>2.5k</code>.\n\n"
        "<b>Язык карточки</b> — последний аргумент: <code>EU</code> (английский), "
        "<code>ar</code> (арабский). Без него карточка на русском.\n"
        "<b>Получатель подарка</b> — необязательный <code>@username</code> перед "
        "языком."
    ),
)

EN = Lang(
    code="en",
    user="User",
    offers_you="offers you",
    for_gift="for the gift",
    valid_for="This offer is valid for another",
    less_than_minute="less than a minute",
    gift_label="Gift",
    sold="has been sold",
    hand_over="Send the gift to the buyer to credit",
    declined="Offer declined",
    accept="Accept",
    decline="Decline",
    transfer="Transfer",
    how_to_transfer="How to send",
    toast_sold="✅ Gift sold successfully",
    toast_declined="Offer declined",
    own_offer=(
        "This is your offer.\n"
        "The Accept and Decline buttons work for the buyer."
    ),
    not_your_offer="This offer is addressed to someone else.",
    already_resolved="This offer has already been processed.",
    expired="This offer has expired.",
    not_found="Offer not found.",
    edit_failed="Could not update the message",
    hint_send_failed="Could not send the offer.",
    hint_retry="Telegram rate-limited the send. Retry in <b>{seconds} s</b>.",
    hint_flood=(
        "Telegram rate-limited the send, the bot already retried. "
        "Wait a minute and send the command again."
    ),
    hint_chat=(
        "This user has never written to the bot, so Telegram does not allow "
        "writing to them on the bot's behalf. Ask them to send "
        "<code>/start</code> once — or send the command in the business chat."
    ),
    hint_rights=(
        "Not enough rights. When connecting the business account, grant the bot "
        "the “Send messages” and “Delete all messages” permissions."
    ),
    hint_business_peer=(
        "Telegram did not let the bot post into this chat on behalf of the "
        "business account. The command itself is fine — the limit comes from the "
        "account:\n"
        "• the \u201ccan reply\u201d right was not granted when connecting the bot "
        "— reconnect in Telegram → Settings → Business and tick it;\n"
        "• no incoming messages in this chat for the last 24 hours: Telegram lets "
        "a bot write on behalf of an account only in active private chats."
    ),
    hint_connection=(
        "Business connection is unavailable. Check that the bot is still "
        "connected to the account."
    ),
    hint_generic="Telegram error. Please try again in a minute.",
    notice_title="✅ Your offer was accepted!",
    buyer_label="Buyer",
    price_label="Price",
    err_no_args="Specify the gift link, price and currency.",
    err_no_price="Specify the price.",
    err_bad_link=(
        "Could not recognise the gift link.\n"
        "Supported format: <code>https://t.me/nft/StarNotepad-21211</code>"
    ),
    err_bad_price=(
        "Could not recognise the price.\n"
        "Supported formats: <code>2500</code>, <code>2 500</code>, "
        "<code>2.5k</code>, <code>1000 Stars</code>"
    ),
    err_price_not_number="The price must be a number.",
    err_price_not_finite="The price must be a finite number.",
    err_price_zero="The price must be greater than zero.",
    err_price_too_big="The price is too large.",
    usage=(
        "<b>Command format:</b>\n"
        "<code>benz &lt;NFT link&gt; &lt;price&gt; &lt;currency&gt; [language]</code>\n\n"
        "<b>Examples:</b>\n"
        "<code>benz https://t.me/nft/StarNotepad-21211 2500 Stars</code>\n"
        "<code>benz https://t.me/nft/StarNotepad-21211 2.5k Gram</code>\n\n"
        "<b>Currencies:</b> <code>Stars</code>, <code>Gram</code>, <code>TON</code>, "
        "<code>USDT</code>.\n"
        "<b>Price:</b> <code>2500</code>, <code>2 500</code>, <code>2.5k</code>.\n\n"
        "<b>Card language</b> is the last argument: <code>EU</code> (English), "
        "<code>ar</code> (Arabic). Without it the card is in Russian.\n"
        "<b>Gift recipient</b> is an optional <code>@username</code> before the language."
    ),
)

AR = Lang(
    code="ar",
    user="مستخدم",
    offers_you="يعرض عليك",
    for_gift="مقابل هدية",
    valid_for="العرض صالح لمدة أخرى",
    less_than_minute="أقل من دقيقة",
    gift_label="هدية",
    sold="تم بيعها بنجاح",
    hand_over="أرسل الهدية إلى المشتري لتحويل",
    declined="تم رفض العرض",
    accept="قبول",
    decline="رفض",
    transfer="تحويل",
    how_to_transfer="طريقة الإرسال",
    toast_sold="✅ تم بيع الهدية بنجاح",
    toast_declined="تم رفض العرض",
    own_offer="هذا عرضك.\nزرّا «قبول» و«رفض» يعملان لدى المشتري.",
    not_your_offer="هذا العرض موجّه إلى مستخدم آخر.",
    already_resolved="تمت معالجة هذا العرض بالفعل.",
    expired="انتهت صلاحية هذا العرض.",
    not_found="العرض غير موجود.",
    edit_failed="تعذّر تحديث الرسالة",
    hint_send_failed="تعذّر إرسال العرض.",
    hint_retry="قيّد Telegram الإرسال مؤقتًا. أعد المحاولة بعد <b>{seconds} ثانية</b>.",
    hint_flood=(
        "قيّد Telegram الإرسال مؤقتًا، وقد أعاد البوت المحاولة. "
        "انتظر دقيقة ثم أعد إرسال الأمر."
    ),
    hint_chat=(
        "لم يكتب هذا المستخدم للبوت من قبل، لذلك لا يسمح Telegram بالكتابة "
        "له نيابة عن البوت. اطلب منه إرسال <code>/start</code> مرة واحدة — "
        "أو أرسل الأمر في محادثة الحساب التجاري."
    ),
    hint_rights=(
        "الأذونات غير كافية. عند ربط الحساب التجاري امنح البوت إذني "
        "«إرسال الرسائل» و«حذف كل الرسائل»."
    ),
    hint_business_peer=(
        "لم يسمح Telegram للبوت بالكتابة في هذه المحادثة نيابةً عن الحساب التجاري. "
        "الأمر نفسه صحيح، والحد يأتي من الحساب:\n"
        "• لم يُمنح إذن «الرد على الرسائل» عند ربط البوت — أعد الربط من إعدادات "
        "Telegram ← الأعمال وحدِّد هذا الإذن؛\n"
        "• لا توجد رسائل واردة في هذه المحادثة خلال 24 ساعة: لا يسمح Telegram "
        "للبوت بالكتابة نيابةً عن الحساب إلا في المحادثات النشطة."
    ),
    hint_connection=(
        "اتصال الحساب التجاري غير متاح. تحقق من أن البوت ما زال متصلًا بالحساب."
    ),
    hint_generic="خطأ في Telegram. حاول مرة أخرى بعد دقيقة.",
    notice_title="✅ تم قبول عرضك!",
    buyer_label="المشتري",
    price_label="السعر",
    err_no_args="حدّد رابط الهدية والسعر والعملة.",
    err_no_price="حدّد السعر.",
    err_bad_link=(
        "تعذّر التعرّف على رابط الهدية.\n"
        "الصيغة المدعومة: <code>https://t.me/nft/StarNotepad-21211</code>"
    ),
    err_bad_price=(
        "تعذّر التعرّف على السعر.\n"
        "الصيغ المدعومة: <code>2500</code>، <code>2 500</code>، "
        "<code>2.5k</code>، <code>1000 Stars</code>"
    ),
    err_price_not_number="يجب أن يكون السعر رقمًا.",
    err_price_not_finite="يجب أن يكون السعر رقمًا منتهيًا.",
    err_price_zero="يجب أن يكون السعر أكبر من صفر.",
    err_price_too_big="السعر كبير جدًا.",
    usage=(
        "<b>صيغة الأمر:</b>\n"
        "<code>benz &lt;رابط NFT&gt; &lt;السعر&gt; &lt;العملة&gt; [اللغة]</code>\n\n"
        "<b>أمثلة:</b>\n"
        "<code>benz https://t.me/nft/StarNotepad-21211 2500 Stars</code>\n"
        "<code>benz https://t.me/nft/StarNotepad-21211 2.5k Gram</code>\n\n"
        "<b>العملات:</b> <code>Stars</code>، <code>Gram</code>، <code>TON</code>، "
        "<code>USDT</code>.\n"
        "<b>السعر:</b> <code>2500</code>، <code>2 500</code>، <code>2.5k</code>.\n\n"
        "<b>لغة البطاقة</b> هي الوسيط الأخير: <code>EU</code> (إنجليزي)، "
        "<code>ar</code> (عربي). بدونه تكون البطاقة بالروسية.\n"
        "<b>مستلم الهدية</b> هو <code>@username</code> اختياريًا قبل اللغة."
    ),
)

LANGUAGES: dict[str, Lang] = {lang.code: lang for lang in (RU, EN, AR)}

DEFAULT_LANGUAGE: str = "ru"

# Что пользователь может написать в конце команды.
# ``EU`` — принятое у нас обозначение английского (по ТЗ заказчика),
# ``en``/``eng``/``english`` — синонимы для удобства.
LANGUAGE_ALIASES: dict[str, str] = {
    "ru": "ru",
    "rus": "ru",
    "рус": "ru",
    "en": "en",
    "eng": "en",
    "eu": "en",
    "english": "en",
    "англ": "en",
    "ar": "ar",
    "arb": "ar",
    "arabic": "ar",
    "араб": "ar",
}


def resolve_lang(token: str | None) -> str | None:
    """``"EU"`` -> ``"en"``. ``None``, если токен не язык."""
    if token is None:
        return None
    return LANGUAGE_ALIASES.get(token.strip().strip(".").lower())


def get_lang(code: str | None) -> Lang:
    """Код языка -> набор строк; неизвестный код тихо падает на русский."""
    return LANGUAGES.get((code or DEFAULT_LANGUAGE).lower(), RU)