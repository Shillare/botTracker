# -*- coding: utf-8 -*-
"""Добавление MTProto-аккаунтов для парсинга — сколько угодно за один запуск.

    py -3.14 login.py

Скрипт в цикле спрашивает номер телефона (имя сессии подставится само)
или имя уже существующей сессии. Закончить — Ctrl+C (или Enter на пустом
вводе). api_id/api_hash берутся из кода (DEFAULT_API_ID/DEFAULT_API_HASH),
дополнительно спрашивать ничего не нужно.

В конце печатается готовый блок ACCOUNTS для main.py.

ВХОД ЧЕРЕЗ ПРОКСИ (важно!): Telegram почти не присылает коды входа
с дата-центровых IP. Скрипт сам ищет живой SOCKS5 из портов
10808/10810/10812/10814/10815 (те же, что PROXY_LIST в main.py) и
показывает, с какого IP пойдёт вход. Форсировать конкретный:

    GIFT_PROXY=127.0.0.1:10810 python3 login.py

Заходить надо через тот же прокси, который потом использует бот, иначе
сессия рождается на одном IP, а работает на другом.

Аргументы тоже поддерживаются (для неинтерактивного добавления или
другой пары ключей):
    py -3.14 login.py <номер_или_имя> [api_id] [api_hash]
"""
import asyncio
import json
import os
import re
import socket
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from telethon import TelegramClient

try:
    # так же, как в main.py
    from python_socks import ProxyType
except ImportError:
    try:
        from telethon.socks import ProxyType
    except ImportError:
        ProxyType = None

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_API_ID = os.environ.get("ACCOUNTS_API_ID") or "33598185"
DEFAULT_API_HASH = (os.environ.get("ACCOUNTS_API_HASH")
                    or "5fe98611b35ac10864382cac60f55916")

# Локальные SOCKS-порты Xray — те же, что в PROXY_LIST в main.py
PROXY_PORTS = [10808, 10810, 10812, 10814, 10815]
PROXY_PORT = None          # порт, через который реально пошёл вход
ACCOUNTS_FILE = os.path.join(HERE, "accounts.json")

BANNER = (
    "Добавление аккаунтов.\n"
    "  Введи НОМЕР ТЕЛЕФОНА (+7...) — имя сессии подставится само;\n"
    "  или имя сессии (acc2 / auto), если аккаунт уже был заведён.\n"
    "  Enter или Ctrl+C — выход."
)
added = []


def port_alive(port, host="127.0.0.1", timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def detect_proxy():
    """--proxy host:port | GIFT_PROXY=host:port, иначе первый живой порт Xray."""
    arg = os.environ.get("GIFT_PROXY", "").strip()
    if arg and arg not in ("1", "0"):
        host, _, p = arg.rpartition(":")
        try:
            return (host or "127.0.0.1", int(p))
        except ValueError:
            print(f"  ! Не понял GIFT_PROXY={arg!r}, жду host:port")
    for p in PROXY_PORTS:
        if port_alive(p):
            return ("127.0.0.1", p)
    return None


def show_exit_ip(proxy):
    """Показывает, с какого IP пойдёт вход — по нему Telegram шлёт код."""
    try:
        import requests
    except ImportError:
        print("  (requests нет — IP не проверяем)")
        return
    px = None
    if proxy:
        px = {"http": f"socks5h://{proxy[0]}:{proxy[1]}",
              "https": f"socks5h://{proxy[0]}:{proxy[1]}"}
    try:
        ip = requests.get("https://api.ipify.org", timeout=15, proxies=px).text.strip()
        via = f"через прокси {proxy[0]}:{proxy[1]}" if proxy else "НАПРЯМУЮ (без прокси)"
        print(f"  IP входа: {ip}  ({via})")
        if not proxy:
            print("  ! Telegram с дата-центровых IP часто не присылает код. "
                  "Подними Xray и повтори.")
    except Exception as e:
        print(f"  ! Не смог проверить IP: {type(e).__name__}: {e}")


def looks_like_phone(s):
    """+79991234567, 79991234567, +7 (999) 123-45-67 — похоже на номер."""
    return bool(re.fullmatch(r"\+?\d[\d\s\-()]{5,}", s.strip()))


def ask(prompt, default=""):
    """input(), но None при EOF/прерывании, чтобы не падать с трассировкой."""
    try:
        raw = input(prompt).strip()
    except EOFError:
        return None
    return raw or default


def free_session_name(start, used):
    """Следующее свободное accN: нет файла и не добавлено в этом запуске."""
    n = start
    while (n in used
           or os.path.exists(os.path.join(HERE, f"acc{n}.session"))):
        n += 1
    return n, f"acc{n}"


async def add_account(name, api_id, api_hash, phone=None, proxy=None):
    """Логин в один аккаунт. Возвращает запись для ACCOUNTS или None."""
    path = os.path.join(HERE, name)
    if os.path.exists(path + ".session"):
        print(f"  ! {name}.session уже существует — вход будет выполнен "
              f"в сохранённый аккаунт. Если это не то — жми Ctrl+C.")
    kwargs = {}
    if proxy:
        if ProxyType is None:
            print("  ! Нет python-socks — вход через прокси невозможен. "
                  "pip install python-socks")
            return None
        kwargs["proxy"] = (ProxyType.SOCKS5, proxy[0], proxy[1])
    api_id = int(os.environ.get("ACCOUNTS_API_ID") or api_id)
    api_hash = os.environ.get("ACCOUNTS_API_HASH") or api_hash
    client = TelegramClient(path, api_id, api_hash, **kwargs)
    try:
        # phone передаём только когда он есть: иначе перекрывается
        # значение по умолчанию и Telethon падает с
        # "No phone number or bot token provided."
        if phone:
            await client.start(phone=phone)
        else:
            await client.start()
        me = await client.get_me()
        if not me:
            print(f"  ! Не удалось войти в «{name}».")
            return None
        print(f"  Готово: {name} -> {me.first_name} "
              f"@{me.username or 'нет'} id={me.id}")
        return {"name": name, "api_id": api_id,
                "api_hash": api_hash, "session": name}
    finally:
        try:
            await client.disconnect()
        except BaseException:
            pass


def save_account(rec):
    """Дописывает аккаунт в accounts.json — вручную в коде ничего менять не нужно.

    Запись атомарная (через временный файл), чтобы оборванный Ctrl+C не
    оставил accounts.json битым. Перезаписывает запись с тем же session.
    """
    data = []
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                data = []
        except Exception as e:
            bak = ACCOUNTS_FILE + ".bak"
            try:
                os.replace(ACCOUNTS_FILE, bak)
                print(f"  ! accounts.json был бит ({type(e).__name__}), "
                      f"сохранил как {os.path.basename(bak)}")
            except Exception:
                pass
            data = []
    # один Telegram-аккаунт = одна сессия: запись с тем же session
    # заменяем, а не дублируем
    data = [a for a in data
            if not (isinstance(a, dict) and a.get("session") == rec["session"])]
    data.append(rec)
    tmp = ACCOUNTS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            f.write("\n")
        os.replace(tmp, ACCOUNTS_FILE)
    except Exception as e:
        print(f"  ! не смог сохранить accounts.json: {type(e).__name__}: {e}")
        return False
    print(f"  Записан в accounts.json ({os.path.basename(ACCOUNTS_FILE)})")
    return True


async def main():
    # одиночный режим через аргументы
    if len(sys.argv) > 1:
        api_id = int(sys.argv[2]) if len(sys.argv) > 2 else int(DEFAULT_API_ID)
        api_hash = (sys.argv[3] if len(sys.argv) > 3 else DEFAULT_API_HASH)
        rec = await add_account(sys.argv[1], api_id, api_hash)
        if rec:
            added.append(rec)
        return

    print(BANNER)
    proxy = detect_proxy()
    global PROXY_PORT
    if proxy:
        PROXY_PORT = proxy[1]
        print(f"Вход пойдёт через SOCKS5 {proxy[0]}:{proxy[1]}")
    else:
        print("Прокси не найден — вход пойдёт НАПРЯМУЮ с IP сервера.")
    show_exit_ip(proxy)
    api_id = int(DEFAULT_API_ID)
    api_hash = DEFAULT_API_HASH
    n = 1

    while True:
        name = ask(f"\n[добавлено: {len(added)}] "
                   f"Номер телефона или имя сессии "
                   f"(Enter/Ctrl+C — выход): ")
        if not name:                      # Enter или Ctrl+C
            break
        phone = None
        if looks_like_phone(name):
            # Номер сам по себе и есть ответ на первый вопрос Telegram —
            # спрашивать его второй раз незачем.
            phone = name
            used = {int(r["name"][3:]) for r in added
                    if r["name"].startswith("acc") and r["name"][3:].isdigit()}
            n, name = free_session_name(n, used)
            print(f"  Номер {phone} -> сессия {name}")
        elif name.lower() in ("auto", "a"):
            used = {int(r["name"][3:]) for r in added
                    if r["name"].startswith("acc") and r["name"][3:].isdigit()}
            n, name = free_session_name(n, used)
            print(f"  Авто-имя: {name}")

        try:
            rec = await add_account(name, api_id, api_hash, phone=phone, proxy=proxy)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n  Прервано.")
            break
        if rec:
            # сразу в рабочий список — бот подхватит при следующем старте
            if PROXY_PORT:
                rec["proxy_port"] = PROXY_PORT
            added.append(rec)
            save_account(rec)


def print_summary():
    if not added:
        print("\nНичего не добавлено.")
        return
    print(f"\n{'=' * 60}")
    print(f"Добавлено аккаунтов: {len(added)}")
    print(f"Всё уже записано в accounts.json — в коде main.py править "
          f"ничего не нужно.")
    if PROXY_PORT:
        print(f"\nПорт {PROXY_PORT} прописан в proxy_port автоматически: "
              f"IP сессии закреплён, ротация отключена.\n"
              f"Запускай бота с GIFT_PROXY=1, иначе он пойдёт напрямую "
              f"с другого IP.")
    else:
        print("\n!! Вход шёл НАПРЯМУЮ, без прокси — proxy_port не проставлен.\n"
              "   Запускай бота БЕЗ GIFT_PROXY=1, иначе работа пойдёт\n"
              "   через прокси и Telegram отзовёт сессию.")
    print(f"{'=' * 60}")


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nОстановлено.")
finally:
    print_summary()