"""Сборка роутеров приложения."""

from __future__ import annotations

from aiogram import Router
from aiogram.dispatcher.middlewares.base import BaseMiddleware

from app.handlers.admin import create_admin_router
from app.handlers.business import create_business_router
from app.handlers.commands import create_commands_router
from app.handlers.offer import create_offer_router

__all__ = (
    "create_admin_router",
    "create_business_router",
    "create_commands_router",
    "create_offer_router",
    "register_routers",
)


def register_routers(root: Router, offer_throttle: BaseMiddleware | None = None) -> Router:
    """Подключает роутеры в порядке приоритета: команды -> предложения -> бизнес -> админка.

    Роутеры создаются фабриками: экземпляр ``Router`` в aiogram можно
    подключить только к одному ``Dispatcher``, поэтому общий объект на уровне
    модуля использовать нельзя.
    """
    root.include_router(create_commands_router())
    root.include_router(create_offer_router(offer_throttle))
    root.include_router(create_business_router())
    root.include_router(create_admin_router())
    return root
