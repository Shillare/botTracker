"""Конфигурация приложения. Источники: переменные окружения -> .env -> значения по умолчанию."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.constants import STARS_LABEL
from app.i18n import DEFAULT_LANGUAGE, LANGUAGES


class Settings(BaseSettings):
    """Единая точка конфигурации бота."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram ---------------------------------------------------------------
    bot_token: str = Field(..., description="Токен бота от @BotFather")
    telegram_proxy: str | None = Field(
        default=None,
        description=(
            "Прокси для связи с Telegram, например http://127.0.0.1:10809. "
            "Нужен, если api.telegram.org недоступен напрямую (VPN в режиме "
            "«системного прокси», DPI или блокировка): бот сам системный прокси "
            "Windows не читает. Пусто — подключение напрямую."
        ),
    )

    # --- Права доступа ----------------------------------------------------------
    owner_ids: str = Field(
        default="",
        description=(
            "Telegram ID владельца(ев) бота через запятую. Только они могут "
            "выдавать и снимать права администратора. Владелец всегда админ, "
            "даже если его нет в таблице admins."
        ),
    )
    admin_ids: str = Field(
        default="",
        description=(
            "Telegram ID дополнительных админов через запятую. Первый запуск "
            "добавляет их в БД один раз; выданные админом права управлять "
            "админами не дают."
        ),
    )

    # --- Хранилище --------------------------------------------------------------
    db_path: Path = Field(default=Path("data/bot.sqlite3"), description="Путь к файлу SQLite")

    # --- Бизнес-аккаунт ---------------------------------------------------------
    default_business_connection_id: str | None = Field(
        default=None,
        description=(
            "Принудительно отправлять сообщения от имени этого бизнес-аккаунта, "
            "даже если апдейт пришёл в обычный диалог с ботом. "
            "Обычно не нужно: business_connection_id берётся из входящего апдейта."
        ),
    )

    # --- Предложения ------------------------------------------------------------
    offer_ttl_minutes: int = Field(
        default=359,
        ge=1,
        le=10_080,
        description="Срок действия предложения в минутах (по умолчанию 5ч 59м)",
    )
    offer_throttle_seconds: float = Field(
        default=0.5,
        ge=0.0,
        description=(
            "Антифлуд для команды benz: минимальный интервал между запросами "
            "одного пользователя. Работает и как защита от спама, и как защита "
            "от случайного дубля команды — не делайте его больше 1 с, иначе "
            "второе предложение подряд молча игнорируется."
        ),
    )
    delete_command_message: bool = Field(
        default=True,
        description="Удалять сообщение с командой benz сразу после отправки карточки",
    )
    stars_label: str = Field(
        default=STARS_LABEL,
        description=(
            "Подпись валюты Stars в карточке. Слово «Stars» вместе с внешней ссылкой "
            "блокируется Telegram (PEER_FLOOD) для получателей, которые не писали "
            "боту. Значение по умолчанию подобрано так, чтобы обойти фильтр."
        ),
    )
    currency_labels: str = Field(
        default="",
        description=(
            "Переопределение подписей валют через запятую: «Stars=Stars,Gram=Gram». "
            "Пусто — валюты выводятся как есть."
        ),
    )
    accept_policy: str = Field(
        default="anyone",
        description=(
            "Кто может нажать «Принять»/«Отклонить»: "
            "anyone (кто угодно из чата), author (только автор benz), "
            "business_owner (только владелец бизнес-аккаунта)."
        ),
    )
    only_business_owner_commands: bool = Field(
        default=True,
        description=(
            "Обрабатывать benz только от владельца бизнес-аккаунта. "
            "В группах команду может набрать любой участник, а отвечать бот "
            "будет от имени аккаунта — поэтому чужие команды игнорируются."
        ),
    )
    default_language: str = Field(
        default=DEFAULT_LANGUAGE,
        description=(
            "Язык карточки, если в команде не указан суффикс: "
            "ru / en / ar. Можно переопределить суффиксом в конце команды."
        ),
    )
    transfer_deep_link: str = Field(
        default="tg://send_gift?to={username}",
        description="Deep link кнопки «Передать». Плейсхолдер: {username}",
    )
    transfer_recipient_username: str | None = Field(
        default=None,
        description=(
            "Username получателя подарка по умолчанию (без @). "
            "Используется, если не указан @username в команде и у нажавшего «Принять» "
            "нет публичного username."
        ),
    )
    how_to_transfer_url: str | None = Field(
        default=None,
        description="Ссылка кнопки «Как передать». Пусто — кнопка не показывается.",
    )

    # --- Логи ------------------------------------------------------------------
    log_level: str = Field(default="INFO")
    log_file: Path | None = Field(default=Path("data/logs/bot.log"))
    log_rotation_bytes: int = Field(default=5 * 1024 * 1024)
    log_rotation_backups: int = Field(default=3)

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, value: str) -> str:
        return value.upper()

    @field_validator(
        "default_business_connection_id",
        "log_file",
        "transfer_recipient_username",
        "how_to_transfer_url",
        mode="before",
    )
    @classmethod
    def _empty_string_to_none(cls, value: str | None) -> str | None:
        """Пустая переменная в .env — это None, а не "".

        Иначе ``business_connection_id=""`` уйдёт в Telegram API и запрос
        будет отклонён, а пустой ``log_file`` создаст файл без имени.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("accept_policy")
    @classmethod
    def _check_policy(cls, value: str) -> str:
        allowed = {"anyone", "author", "business_owner"}
        normalized = value.strip().lower()
        if normalized not in allowed:
            raise ValueError(f"ACCEPT_POLICY должен быть одним из {sorted(allowed)}")
        return normalized

    @field_validator("default_language")
    @classmethod
    def _check_language(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in LANGUAGES:
            raise ValueError(f"DEFAULT_LANGUAGE должен быть одним из {sorted(LANGUAGES)}")
        return normalized

    @field_validator("offer_ttl_minutes")
    @classmethod
    def _ttl_bounds(cls, value: int) -> int:
        return max(1, value)

    @field_validator("transfer_recipient_username", mode="before")
    @classmethod
    def _strip_at_sign(cls, value: str | None) -> str | None:
        """``TRANSFER_RECIPIENT_USERNAME=@user`` -> ``user``."""
        if isinstance(value, str):
            stripped = value.strip().lstrip("@").strip()
            return stripped or None
        return value

    @staticmethod
    def _parse_ids(raw: str) -> frozenset[int]:
        """``"123, 456"`` -> ``frozenset({123, 456})``."""
        normalized = raw.replace(",", " ").replace(";", " ")
        result: set[int] = set()
        for chunk in normalized.split():
            chunk = chunk.strip()
            if chunk.lstrip("-").isdigit():
                result.add(int(chunk))
        return frozenset(result)

    @property
    def owner_id_set(self) -> frozenset[int]:
        """Владельцы бота: только они управляют списком админов."""
        return self._parse_ids(self.owner_ids)

    @property
    def admin_id_set(self) -> frozenset[int]:
        """Админы, засеваемые в БД при первом запуске."""
        return self._parse_ids(self.admin_ids)

    def is_owner(self, user_id: int | None) -> bool:
        """Владелец всегда админ — даже без записи в таблице ``admins``."""
        return user_id is not None and user_id in self.owner_id_set

    @property
    def offer_ttl_seconds(self) -> int:
        return self.offer_ttl_minutes * 60

    def currency_label(self, currency: str) -> str:
        """Подпись валюты с учётом переопределений из ``CURRENCY_LABELS``.

        Для Stars по умолчанию берётся ``STARS_LABEL`` — подпись, которая
        не ловит фильтр Telegram. Явное переопределение всегда приоритетнее.
        """
        overrides: dict[str, str] = {}
        for chunk in self.currency_labels.replace(";", ",").split(","):
            if "=" not in chunk:
                continue
            key, _, value = chunk.partition("=")
            overrides[key.strip().lower()] = value.strip()

        explicit = overrides.get(currency.lower())
        if explicit:
            return explicit
        if currency.lower() == "stars":
            return self.stars_label
        return currency


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Кэшированный accessor конфигурации."""
    return Settings()
