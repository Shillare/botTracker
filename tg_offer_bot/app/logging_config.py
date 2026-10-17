"""Настройка логирования: цветной вывод в консоль + ротация в файле."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from app.config import Settings

_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[1;31m",
}
_RESET = "\033[0m"

_CONSOLE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_FILE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(filename)s:%(lineno)d | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class ColorFormatter(logging.Formatter):
    """Formatter, добавляющий ANSI-цвет к уровню логирования."""

    def __init__(self, fmt: str, datefmt: str) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._fmt = fmt

    def format(self, record: logging.LogRecord) -> str:
        color = _COLORS.get(record.levelname, "")
        base = super().format(record)
        return f"{color}{base}{_RESET}" if color else base


def setup_logging(settings: Settings) -> None:
    """Конфигурирует корневой логгер приложения. Вызывается один раз при старте."""
    root = logging.getLogger()
    root.setLevel(settings.log_level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(stream=sys.stdout)
    formatter: logging.Formatter = (
        ColorFormatter(_CONSOLE_FORMAT, _DATE_FORMAT)
        if sys.stdout.isatty()
        else logging.Formatter(_CONSOLE_FORMAT, _DATE_FORMAT)
    )
    console.setFormatter(formatter)
    root.addHandler(console)

    if settings.log_file is not None:
        log_path: Path = settings.log_file
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            filename=log_path,
            maxBytes=settings.log_rotation_bytes,
            backupCount=settings.log_rotation_backups,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, _DATE_FORMAT))
        root.addHandler(file_handler)

    logging.getLogger("aiogram").setLevel(logging.INFO)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
