"""Точка входа: ``python -m app``."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramConflictError,
    TelegramMigrateToChat,
    TelegramNetworkError,
)
from aiogram.fsm.storage.memory import MemoryStorage

from app import __version__
from app.config import Settings, get_settings
from app.constants import BOT_COMMANDS
from app.database import AdminRepository, BusinessConnectionRepository, Database
from app.handlers import register_routers
from app.logging_config import setup_logging
from app.middlewares import (
    AdminContextMiddleware,
    AdminGateMiddleware,
    BusinessContextMiddleware,
    DatabaseMiddleware,
    GatewayMiddleware,
    ThrottleMiddleware,
)
from app.services import business
from app.services.telegram import TelegramGateway

logger = logging.getLogger(__name__)

# Апдейты, необходимые для работы бизнес-функций.
ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
    "business_connection",
    "callback_query",
    "my_chat_member",
]


def build_dispatcher(db: Database, settings: Settings, gateway: TelegramGateway) -> Dispatcher:
    """Собирает Dispatcher со всеми middlewares и роутерами."""
    dispatcher = Dispatcher(storage=MemoryStorage())

    # Порядок важен: сначала инфраструктура (БД), затем производные от неё данные.
    # Гейт админов идёт последним в цепочке outer-middleware: он видит
    # вычисленный is_admin и обрывает обработку для всех, кто не админ.
    dispatcher.update.middleware(DatabaseMiddleware(db))
    dispatcher.update.middleware(GatewayMiddleware(gateway))
    dispatcher.update.middleware(AdminContextMiddleware(settings))
    dispatcher.update.middleware(AdminGateMiddleware())
    dispatcher.update.middleware(BusinessContextMiddleware(settings))

    # Антифлуд живёт только внутри роутера предложений и не трогает callback'и.
    register_routers(dispatcher, ThrottleMiddleware(rate=settings.offer_throttle_seconds))
    return dispatcher


async def on_startup(bot: Bot, db: Database, settings: Settings) -> None:
    # Версия в логе — единственный быстрый способ отличить новую сборку от
    # старой: если на сервере видны кнопки в старом оформлении, значит запущен
    # процесс с прошлой версией кода (или с необновлёнными файлами).
    logger.info("Версия бота: %s (%s)", __version__, Path(__file__).resolve().parent)

    admin_repo = AdminRepository(db)
    seeded = await admin_repo.seed_once(set(settings.admin_id_set))
    if seeded:
        logger.info("Добавлено стартовых админов: %s", sorted(settings.admin_id_set))

    try:
        me = await bot.get_me()
    except TelegramNetworkError as exc:
        # Не фатально: polling переподключится сам, когда сеть вернётся.
        logger.warning("Не удалось получить профиль бота (сеть недоступна): %s", exc)
    else:
        logger.info("Бот запущен: @%s (id=%s)", me.username, me.id)

    if settings.admin_id_set:
        logger.info("Админы из конфигурации: %s", sorted(settings.admin_id_set))

    # Владельцы видны в логе сразу при старте. Бот по умолчанию полностью
    # игнорирует всех, кроме владельца и админов, и выглядит это как «зависшая
    # карточка» — без этой строки причину приходится угадывать по апдейтам.
    if settings.owner_id_set:
        logger.info("Владельцы (OWNER_IDS): %s", sorted(settings.owner_id_set))
    else:
        logger.warning(
            "OWNER_IDS пуст: выдавать и снимать права админов будет некому. "
            "Прописать хотя бы один ID в .env."
        )
    logger.info(
        "Остальные пользователи игнорируются; узнать свой ID и роль — команда /id"
    )

    await _sync_business_connections(bot, db)

    try:
        await bot.set_my_commands(
            [
                {"command": command, "description": description}
                for command, description in BOT_COMMANDS
            ]
        )
    except (TelegramAPIError, TelegramNetworkError) as exc:
        logger.warning("Не удалось обновить список команд: %s", exc)


async def _sync_business_connections(bot: Bot, db: Database) -> None:
    """Синхронизирует бизнес-подключения с Telegram при запуске.

    Событие ``business_connection`` приходит один раз, поэтому база могла
    остаться с устаревшим или вовсе не записанным подключением. Здесь бот
    сам переспрашивает состояние и права — иначе «отправляю от имени аккаунта»
    выглядит как загадка, пока карточка уходит от имени бота.
    """
    repository = BusinessConnectionRepository(db)
    rows = await repository.list_all()
    if not rows:
        logger.warning(
            "Бизнес-подключений в базе нет. Подключите бота в Telegram → "
            "Настройки → Бизнес; после этого карточки будут уходить от имени аккаунта."
        )
        return

    for row in rows:
        connection_id = str(row["id"])
        connection = await business.refresh(bot, repository, connection_id)
        if connection is None:
            logger.warning(
                "Бизнес-подключение %s (@%s) недоступно: проверьте, что бот не "
                "отключён от аккаунта",
                connection_id,
                row["username"] or row["user_id"],
            )
            continue
        if not connection.is_enabled:
            logger.warning(
                "Бизнес-подключение %s отключено в Telegram — карточки пойдут от имени бота",
                connection_id,
            )


def register_startup(dispatcher: Dispatcher, bot: Bot, db: Database, settings: Settings) -> None:
    """Регистрирует startup-хук.

    Обёртка обязана быть async-функцией: если зарегистрировать
    ``lambda **_: on_startup(...)``, aiogram не признает её корутиной
    (``iscoroutinefunction == False``) и вызовет без ``await`` —
    хук молча не отработает.
    """

    async def _startup(**_: Any) -> None:
        await on_startup(bot, db, settings)

    dispatcher.startup.register(_startup)


async def poll_forever(
    dispatcher: Dispatcher,
    bot: Bot,
    stop_event: asyncio.Event,
) -> None:
    """Polling с бесконечными переподключениями.

    ``start_polling`` внутри вызывает ``bot.me()`` и падает с
    ``TelegramNetworkError``, если Telegram недоступен. Без обёртки бот
    умирал при первом же обрыве сети — здесь он переподключается сам.
    """
    delay = 5.0
    while not stop_event.is_set():
        try:
            await dispatcher.start_polling(
                bot,
                allowed_updates=ALLOWED_UPDATES,
                handle_signals=False,
                # Каждый апдейт — своя задача (так aiogram работает по
                # умолчанию), но без лимита пачка команд запускает сотни задач,
                # и все они разом бьют в Telegram одной очереди — отсюда 429 и
                # «карточки с задержкой». Ограничиваем степень параллелизма;
                # ровный поток уже обеспечивает ThrottleMiddleware.
                handle_as_tasks=True,
                tasks_concurrency_limit=24,
            )
            return
        except TelegramNetworkError as exc:
            logger.warning("Сеть недоступна (%s). Повтор через %.0f с", exc, delay)
        except TelegramConflictError as exc:
            logger.warning("Бот уже запущен в другом процессе (%s). Повтор через 30 с", exc)
            delay = 30.0
        except TelegramMigrateToChat as exc:
            logger.critical("Бот переехал в чат %s — обновите токен", exc.migrate_to_chat_id)
            return
        except Exception:
            logger.exception("Polling завершился с непредвиденной ошибкой")
            return

        with suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        delay = min(delay * 2, 60.0)


def _build_session(settings: Settings) -> AiohttpSession | None:
    """Сессия с прокси, если он задан в конфигурации.

    aiogram по умолчанию НЕ читает системный прокси Windows и переменные
    ``HTTP_PROXY``/``HTTPS_PROXY``: соединение идёт напрямую. Если Telegram
    заблокирован (DPI, VPN в режиме «системного прокси»), бот молча падает с
    ``RECORD_LAYER_FAILURE``, хотя в браузере всё работает. Поэтому адрес
    прокси задаётся явно через ``TELEGRAM_PROXY``.
    """
    proxy = settings.telegram_proxy
    if not proxy:
        return None
    logger.info("Соединение с Telegram через прокси %s", proxy)
    return AiohttpSession(proxy=proxy)


async def run() -> None:
    settings = get_settings()
    setup_logging(settings)

    db = Database(settings.db_path)
    await db.connect()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=_build_session(settings),
    )
    gateway = TelegramGateway(bot)
    dispatcher = build_dispatcher(db, settings, gateway)
    register_startup(dispatcher, bot, db, settings)

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Получен сигнал остановки")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except (TelegramAPIError, TelegramNetworkError) as exc:
        logger.warning("Не удалось снять webhook (продолжаю polling): %s", exc)

    try:
        await poll_forever(dispatcher, bot, stop_event)
    except asyncio.CancelledError:
        logger.info("Polling отменён")
    finally:
        await bot.session.close()
        await db.close()
        logger.info("Бот остановлен")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:  # pragma: no cover
        pass
    except Exception as exc:
        logging.getLogger(__name__).critical("Критическая ошибка запуска", exc_info=exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
