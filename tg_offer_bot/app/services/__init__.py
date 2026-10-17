"""Сервисный слой."""

from app.services.parser import (
    GiftLink,
    OfferArgs,
    OfferParseError,
    parse_currency,
    parse_gift_link,
    parse_stars,
    split_offer_args,
)
from app.services.telegram import TelegramGateway

__all__ = (
    "GiftLink",
    "OfferArgs",
    "OfferParseError",
    "TelegramGateway",
    "parse_currency",
    "parse_gift_link",
    "parse_stars",
    "split_offer_args",
)
