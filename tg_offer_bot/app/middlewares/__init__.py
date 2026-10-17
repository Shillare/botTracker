"""Middlewares: внедрение зависимостей и бизнес-контекста."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import (
    BusinessConnection,
    CallbackQuery,
    Message,
    TelegramObject,
    Update,
    User,
)

from app.config import Settings
from app.database import AdminRepository, BusinessConnectionRepository, Database
from app.services import business
from app.services.telegram import FLOOD_GUARD, TelegramGateway

logger = logging.getLogger(__name__)

# Команды, на которые бот отвечает **любому**, включая посторонних.
#
# Исключение из правила «не-админы полностью игнорируются» сделано намеренно
# и только для одной команды: свой Telegram ID невозможно вычислить в уме, а
# без него посторонний не понимает, что его игнорируют — сообщение висит с
# часиками, и человек ждёт ответа, которого не будет никогда. ``/id`` превращает
# бесконечное ожидание в один короткий ответ с готовым решением (передать ID
# владельцу). Само существование бота тут не раскрывается: посторонний узнаёт
# лишь свой ID и отсутствие доступа — ровно то, ради чего команда нужна.
PUBLIC_COMMANDS = frozenset({"/id", "/whoami"})


def _is_public_command(text: str | None) -> bool:
    """``/id@BotName`` тоже считается публичной командой."""
    if not text:
        return False
    head = text.strip().partition(" ")[0].partition("@")[0].lower()
    return head in PUBLIC_COMMANDS


class GatewayMiddleware(BaseMiddleware):
    """Кладёт готовый :class:`TelegramGateway` в ``data`` для всех хендлеров."""

    def __init__(self, gateway: TelegramGateway) -> None:
        self._gateway = gateway

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["gateway"] = self._gateway
        return await handler(event, data)


class DatabaseMiddleware(BaseMiddleware):
    """Кладёт ``Database`` в ``data`` для всех хендлеров."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["db"] = self._db
        return await handler(event, data)


class AdminContextMiddleware(BaseMiddleware):
    """Проставляет ``data["is_admin"]`` и ``data["is_owner"]``.

    * ``is_owner`` — ID пользователя есть в ``OWNER_IDS`` (``.env``).
      Только владелец управляет списком админов.
    * ``is_admin`` — владелец **всегда** админ, даже без записи в таблице
      ``admins``; остальные админы читаются из БД.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        is_owner = self._settings.is_owner(user.id if user else None)

        db: Database | None = data.get("db")
        if is_owner:
            is_admin = True
        elif db is None or user is None:
            is_admin = False
        else:
            is_admin = await AdminRepository(db).is_admin(user.id)

        data["is_admin"] = is_admin
        data["is_owner"] = is_owner
        return await handler(event, data)


class AdminGateMiddleware(BaseMiddleware):
    """Не-админы не получают ответа на команды и не подключают бизнес-аккаунт.

    Стоит после :class:`AdminContextMiddleware`, поэтому знает ``is_admin``.
    Обработчик просто не вызывается, поэтому апдейт не доходит ни до одного
    роутера: ни ответа в чат, ни записи в БД, ни реакции на подключение.

    Нажатия inline-кнопок гейт **не** трогает: карточку принимает покупатель,
    а он не обязан быть админом. Иначе предложение нельзя было бы закрыть.

    Исключение — команды из :data:`PUBLIC_COMMANDS` (``/id``): их бот отвечает
    любому, иначе посторонний не может узнать свой ID и вечно ждёт ответа,
    которого не будет.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # В outer-middleware aiogram передаёт сырой ``Update``. Реальное
        # событие лежит в ``Update.event`` — по нему и решаем, гейтить ли.
        resolved = event.event if isinstance(event, Update) else event
        if not isinstance(resolved, (Message, BusinessConnection)):
            return await handler(event, data)

        if data.get("is_admin"):
            return await handler(event, data)

        user: User | None = data.get("event_from_user")
        if isinstance(resolved, Message) and _is_public_command(resolved.text):
            return await handler(event, data)

        logger.info(
            "Апдейт от не-админа отброшен: user=%s, тип=%s%s",
            user.id if user else "?",
            type(resolved).__name__,
            "" if user is None else f" — выдать права: /add_admin {user.id}",
        )
        return None


class BusinessContextMiddleware(BaseMiddleware):
    """Определяет ``business_connection_id`` для исходящих сообщений.

    Приоритет источников:
      1. ``business_connection_id`` входящего апдейта (business_message /
         callback_query по сообщению бизнес-аккаунта);
      2. активная запись в БД (fallback);
      3. ``DEFAULT_BUSINESS_CONNECTION_ID`` из конфигурации.

    Неизвестный ``business_connection_id`` **не отбрасывается**: событие
    ``business_connection`` Telegram присылает один раз, и если бот в этот
    момент не работал — оно потеряно навсегда. Запись восстанавливается через
    Bot API ``getBusinessConnection`` (:func:`app.services.business.refresh`) —
    это единственный источник, где видны настоящие права подключения. Иначе бот
    молча откатился бы на отправку от своего имени: карточки уходят не от
    бизнес-аккаунта, команду удалить нечем, продавец не понимает почему.

    Если запрос не прошёл (сеть недоступна, id невалиден), остаётся запасной
    путь: запись собирается из самого апдейта. Права при этом неизвестны, они
    помечаются нулевыми и уточняются при следующей синхронизации — на сценарий
    это не влияет, они нужны только для показа в ``/business_info``.

    Значение из ``.env`` тоже проверяется по базе: невалидный идентификатор
    в конфигурации раньше проходил мимо проверки и ломал отправку.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @staticmethod
    def _owner_of(event: TelegramObject) -> User | None:
        """Владелец бизнес-аккаунта, которому принадлежит апдейт.

        ``business_message`` по определению отправлен подключённым аккаунтом,
        поэтому его ``from_user`` — и есть владелец подключения. У обычного
        сообщения такого смысла нет, и подключение не восстанавливается.
        """
        # Outer-middleware получает сырой ``Update``; реальное событие лежит
        # в ``Update.event``. Без разыменования владелец не находится никогда
        # и восстановление не срабатывает.
        resolved = event.event if isinstance(event, Update) else event
        message = resolved.message if isinstance(resolved, CallbackQuery) else resolved
        if isinstance(message, Message) and message.business_connection_id:
            return message.from_user
        return None

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        connection_id: str | None = None

        if isinstance(event, Message):
            connection_id = event.business_connection_id
        elif isinstance(event, CallbackQuery) and event.message is not None:
            connection_id = getattr(event.message, "business_connection_id", None)

        if not connection_id:
            context = data.get("event_context")
            connection_id = getattr(context, "business_connection_id", None)

        # В самих событиях business_connection запись в БД ещё не создана —
        # её создаёт хендлер. Валидировать там нечего.
        is_connection_event = isinstance(event, Update) and event.business_connection is not None

        db: Database | None = data.get("db")
        if connection_id and not is_connection_event and db is not None:
            connection_id = await self._resolve(
                connection_id, event, db, data.get("bot")
            )

        if not connection_id and self._settings.default_business_connection_id:
            configured = self._settings.default_business_connection_id
            if db is None or await BusinessConnectionRepository(db).get_enabled(configured):
                connection_id = configured
            else:
                logger.warning(
                    "DEFAULT_BUSINESS_CONNECTION_ID=%s не найден в базе — игнорирую",
                    configured,
                )

        data["business_connection_id"] = connection_id
        data["settings"] = self._settings
        return await handler(event, data)

    async def _resolve(
        self,
        connection_id: str,
        event: TelegramObject,
        db: Database,
        bot: Bot | None,
    ) -> str | None:
        """Проверяет подключение по базе, восстанавливая потерянное."""
        repository = BusinessConnectionRepository(db)
        if await repository.get_enabled(connection_id) is not None:
            return connection_id

        if await repository.get(connection_id) is not None:
            # Запись есть, но помечена отключённой: пользователь отключил бота
            # осознанно, и молча игнорировать его решение нельзя.
            logger.warning(
                "Бизнес-подключение %s отключено — отправляю от имени бота",
                connection_id,
            )
            return None

        # Записи нет вовсе: событие подключения потеряно. Спрашиваем Telegram.
        if await business.refresh(bot, repository, connection_id) is not None:
            return connection_id

        owner = self._owner_of(event)
        if owner is None:
            logger.warning(
                "business_connection_id %s неизвестен, восстановить не удалось",
                connection_id,
            )
            return None

        await repository.register_from_update(
            connection_id,
            user_id=owner.id,
            user_chat_id=owner.id,
            username=owner.username,
            first_name=owner.first_name,
        )
        logger.info(
            "Бизнес-подключение %s восстановлено из апдейта (владелец @%s, id=%s); "
            "права пока неизвестны и уточнятся при следующей синхронизации",
            connection_id,
            owner.username or "?",
            owner.id,
        )
        return connection_id


class ThrottleMiddleware(BaseMiddleware):
    """Пейсинг команд: не чаще ``rate`` секунд на пользователя.

    Команды НЕ отбрасываются: если новая пришла раньше, чем истёк интервал,
    она дожидается своей очереди и обрабатывается. Отбрасывать опасно —
    «проглоченный» ``benz`` выглядит как проигнорированная команда, хотя
    пользователь её отправил.

    Пауза работает заодно как защита от флуд-лимита Telegram: пачка команд
    уходит не одним залпом (бот получил бы 429 и карточки «догружались» бы
    с паузами до 5 с), а ровным потоком.

    Обращения к inline-кнопкам не пейсятся — иначе пользователь не сможет
    подтвердить предложение, а «задержанные» нажатия выглядят как зависший бот.
    """

    def __init__(self, rate: float = 1.0, max_keys: int = 100_000) -> None:
        self._rate = rate
        self._max_keys = max_keys
        self._locks: dict[int, asyncio.Lock] = {}
        self._last_seen: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, CallbackQuery):
            return await handler(event, data)

        user: User | None = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        # Один замок на пользователя: команды обрабатываются по очереди, а не
        # гонкой параллельных задач. Без этого пачка ``benz`` улетает в
        # Telegram одновременно и ловит 429 — отсюда «одна карточка сразу, а
        # остальные грузятся и доходят с задержкой».
        lock = self._locks.setdefault(user.id, asyncio.Lock())
        async with lock:
            # Общий тормоз: если Telegram только что ответил «флуд», ждём все,
            # а не только автор той команды. Пока флуда не было — паузы нет и
            # обычная скорость не меняется.
            cooldown = FLOOD_GUARD.delay()
            if cooldown > 0:
                logger.warning("Флуд-тормоз: держу команду %.1f с", cooldown)
                await asyncio.sleep(cooldown)

            now = time.monotonic()
            previous = self._last_seen.get(user.id)
            if previous is not None:
                wait = self._rate - (now - previous)
                if wait > 0:
                    logger.debug(
                        "Пейсинг: команду от %s выдерживаю %.2f с", user.id, wait
                    )
                    await asyncio.sleep(wait)
            self._last_seen[user.id] = time.monotonic()

            if len(self._locks) > self._max_keys:
                self._locks = {key: lock for key, lock in self._locks.items() if key in self._last_seen}
                self._last_seen = {
                    key: value
                    for key, value in self._last_seen.items()
                    if now - value < self._rate * 10
                }
            return await handler(event, data)
