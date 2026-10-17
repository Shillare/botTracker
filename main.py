# -*- coding: utf-8 -*-
"""
Парсер маркетплейса коллекционных подарков Telegram (MTProto) — v2.

При появлении НОВОГО листинга (только что выставленного на продажу) бот
присылает его в форум по 6 категориям:
  Разные страны / Черный фон / Дешёвые / Девочки / Дорогие / NFT от 10 ton.

Общие фильтры: только бесплатные сообщения + Open Profile, ≤10 NFT, ≤2 lvl.
Язык: Разные страны — все кроме русских; остальные 5 — ТОЛЬКО русские.

Сначала бот проходит «warmup»: собирает полный снимок текущего рынка
(все страницы всех типов) без отправок. После сходимости снимка начинается
работа — отправляются ТОЛЬКО листинги, которых ещё не было в снимке
(новые). Давно висящие лоты никогда не присылаются.

Запуск:  python main.py
  - первый раз: авторизация аккаунта (номер телефона + код),
  - бот сам определит темы форума по названиям.
"""

import asyncio
import concurrent.futures
import datetime as dt
import html
import json
import os
import sys
import threading
import time
import re
import traceback
import random
import socket
import queue
from collections import deque

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests
from telethon import TelegramClient, errors as tl_errors
from telethon.tl import functions, types
from telethon.tl.functions.users import GetFullUserRequest

# ============================== НАСТРОЙКИ ==============================
# Секреты лежат в .env рядом с main.py (файл НЕ в репозитории).
# Скопируй .env.example -> .env и впиши значения.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass


def _env(key, default=None):
    v = os.environ.get(key)
    return v if v not in (None, "") else default


API_ID = int(_env("API_ID", "24491381"))
API_HASH = _env("API_HASH", "227a712a0a17644e26a92d53cb0aa324")
BOT_TOKEN = _env("BOT_TOKEN", "8718362222:AAFTT9z6_kduxCGCB3Wq6g7H4TkL96KzQGs")

# MTProto-аккаунты берутся из accounts.json (см. _load_accounts ниже).
# Формат записи:
#   {"name": "accN", "api_id": ..., "api_hash": "...",
#    "session": "accN", "proxy_port": 10808}
#
# proxy_port — порт Xray, через который аккаунт заводился. Он навсегда
# задаёт IP сессии: ротация для такого аккаунта ОТКЛЮЧЕНА, потому что
# смена IP на живой сессии = Telegram отзывает ключ.
#
# Файл удалён или бит → берутся значения ниже.
_ACCOUNTS_FALLBACK = [
    {"name": "acc5", "api_id": 33598185,
     "api_hash": "5fe98611b35ac10864382cac60f55916",
     "session": "acc5", "proxy_port": 10808},
]

_ACCOUNTS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "accounts.json")


def _load_accounts():
    """Читает accounts.json. При любой ошибке берёт запасной список."""
    accs, seen = [], set()
    try:
        if not os.path.exists(_ACCOUNTS_FILE):
            return list(_ACCOUNTS_FALLBACK)
        with open(_ACCOUNTS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"[accounts] не читается ({type(e).__name__}: {e}) — "
              f"беру запасной список", flush=True)
        return list(_ACCOUNTS_FALLBACK)
    if not isinstance(raw, list):
        print("[accounts] accounts.json — ожидался список, беру запасной",
              flush=True)
        return list(_ACCOUNTS_FALLBACK)
    for a in raw:
        if not isinstance(a, dict):
            continue
        sess = a.get("session")
        if not (a.get("name") and sess and a.get("api_hash")):
            print(f"[accounts] пропущена кривая запись: {a}", flush=True)
            continue
        try:
            a["api_id"] = int(a["api_id"])
        except (KeyError, TypeError, ValueError):
            print(f"[accounts] пропущен api_id: {a}", flush=True)
            continue
        if sess in seen:          # один Telegram-аккаунт = одна сессия
            print(f"[accounts] дубликат сессии «{sess}» — пропущен", flush=True)
            continue
        seen.add(sess)
        accs.append(a)
    return accs or list(_ACCOUNTS_FALLBACK)


ACCOUNTS = _load_accounts()

# Пул локальных прокси (Xray-инстансы на Beget).
PROXY_LIST = [
    ("127.0.0.1", 10808),
    ("127.0.0.1", 10810),
    ("127.0.0.1", 10812),
    ("127.0.0.1", 10814),
    ("127.0.0.1", 10815),
]

POLL_SECONDS = 5            # пауза между циклами, сек.
                           # БЫЛО 0 — без пауз это ~250 запросов подряд без
                           # остановки, что рвёт сессии Telegram.
SKIP_TTL = 2 * 60           # тип без лотов пропускаем это время (сек)
CATALOG_REFRESH_MINUTES = 30  # обновление каталога базовых подарков

# ---------- Защита от флуда / разрыва сессий ----------
# Telegram рвёт user-сессию не за «много аккаунтов», а за темп запросов
# на ОДИН аккаунт. Лимитируем каждый аккаунт отдельно: при N аккаунтах
# суммарный темп растёт, но нагрузка на каждый остаётся в норме.
RPS_PER_ACCOUNT = 2.0       # максимум запросов в секунду на один аккаунт
BURST_PER_ACCOUNT = 4       # размер пачки (разово пропустить можно чуть больше)
MIN_TYPE_INTERVAL = 40      # минимальный интервал между опросами одного типа, сек
ACCOUNT_STAGGER = 2.0       # разброс старта аккаунтов, сек (не бьют синхронно)
MAX_FLOOD_WAIT = 600        # FLOOD_WAIT дольше — считаем аккаунт потерянным

# Ошибки, после которых сессию Telegram отозвал. Повтором НЕ лечатся —
# нужен новый логин по номеру и коду.
SESSION_DEAD_ERRORS = (
    tl_errors.AuthKeyUnregisteredError,
    tl_errors.SessionRevokedError,
    tl_errors.SessionPasswordNeededError,
)

PAGE_LIMIT = 100            # размер страницы getResaleStarGifts
SELLER_CACHE_TTL = 7200     # кэш данных продавцов, сек (2 часа)

# warmup: сколько полных проходов рынка сделать до старта отправок
WARMUP_MIN_PASS = 2         # минимум полных проходов
WARMUP_STABLE = 500         # если новых ключей за проход меньше — снимок готов
WARMUP_MAX_PASS = 6         # максимум проходов (страховка от вечного warmup)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "state.json")
LOG_FILE = os.path.join(HERE, "bot.log")


def log(*parts, t=True):
    """Пишет в консоль и в bot.log (для отладки кнопок/ошибок)."""
    line = " ".join(str(p) for p in parts)
    if t:
        line = time.strftime("%H:%M:%S") + " " + line
    try:
        print(line, flush=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# Брони: записываются на диск сразу при отправке лота, чтобы переживать
# перезапуск/падение. state.json пишется в конце цикла и может быть старым.
CLAIMS_FILE = os.path.join(HERE, "claims.json")

# Защита от двойного запуска: два инстанса с одним токеном делят getUpdates.
LOCK_FILE = os.path.join(HERE, "bot.lock")


def _pid_alive(pid):
    """Жив ли процесс с PID (Linux-проверка; на Windows считаем мёртвым)."""
    if sys.platform == "win32":
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def acquire_lock():
    """Занимаем бота. Если уже запущен другой живой инстанс — отказ."""
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE, encoding="utf-8") as f:
                pid = int((f.read() or "0").strip() or 0)
            if pid and _pid_alive(pid):
                print(f"! Бот уже запущен (PID {pid}). Второй инстанс с тем же "
                      f"токеном ломает кнопки «Занять». Остановите его и "
                      f"запустите заново.", flush=True)
                return False
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True
    except Exception as e:
        print(f"[lock] {e}", flush=True)
        return True


def release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE, encoding="utf-8") as f:
                pid = int((f.read() or "0").strip() or 0)
            if pid == os.getpid():
                os.remove(LOCK_FILE)
    except Exception:
        pass

# ------------------------- прокси -------------------------
# Обычно выключено (на сервере). Для локального теста через обход:
#   set GIFT_PROXY=1   (Windows)   /   GIFT_PROXY=1 python main.py (Linux)
PROXY_ENABLED = os.environ.get("GIFT_PROXY", "0") == "1"   # Beget — через Xray-прокси
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 10808
PROXY_BOT_API = False

_PROXY_LOCK = threading.Lock()  # переключение прокси

try:
    from python_socks import ProxyType
    _PROXY_TUPLE = (ProxyType.SOCKS5, PROXY_HOST, PROXY_PORT) if PROXY_ENABLED else None
    _HAS_PROXY = True
except Exception:
    _PROXY_TUPLE = None
    _HAS_PROXY = False

HTTP_PROXIES = None
if PROXY_ENABLED:
    HTTP_PROXIES = {
        "http": f"socks5h://{PROXY_HOST}:{PROXY_PORT}",
        "https": f"socks5h://{PROXY_HOST}:{PROXY_PORT}",
    }

# Если бот сам не нашёл чат — впишите число (см. лог).
CHAT_ID_OVERRIDE = None

# PM_MODE = False: слать в форум FORUM_CHAT_ID по темам.
PM_MODE = False

# Форум-супергруппа с темами для рассылки
FORUM_CHAT_ID = -1003978513084

# Новые категории (6 шт) -> названия тем в форуме
TOPIC_NAMES = {
    "country": "Разные страны",
    "black":   "Черный фон",
    "cheap":   "Дешёвые",
    "girls":   "Девочки",
    "dear":    "Дорогие",
    "nft10":   "NFT от 10 ton",
    "upgrade": "Upgrade",
}

# Приоритет не используется жёстко — лот может уйти в несколько тем:
# англоязычные дублируются везде, где подходят; русские — во все кроме
# «Разные страны»; арабы/китайцы — только в «Разные страны».
FEED_PRIORITY = ["country", "black", "cheap", "girls", "dear", "nft10", "upgrade"]

# ── Дешёвые — строго по названию (нижний регистр) ──
CHEAP_TITLES = {
    "b-day candle",
    "berry box",
    "big year",
    "bow tie",
    "bunny muffin",
    "candy cane",
    "cookie heart",
    "desk calendar",
    "easter egg",
    "eternal candle",
    "evil eye",
    "ginger cookie",
    "hanging star",
    "hex pot",
    "holiday drink",
    "homemade cake",
    "hypno lollipop",
    "jelly bunny",
    "jester hat",
    "jingle bells",
    "jolly chimp",
    "joyful bundle",
    "lollipop",
    "love candle",
    "lunar snake",
    "lush bouquet",
    "moon pendant",
    "party sparkler",
    "pet snake",
    "pool float",
    "restless jar",
    "sakura flower",
    "santa hat",
    "sleigh bell",
    "snake box",
    "snow globe",
    "snow mittens",
    "spiced wine",
    "spy agaric",
    "star notepad",
    "swag bag",
    "surge board",
    "timeless book",
    "tama gadget",
    "victory medal",
    "vice cream",
    "witch hat",
    "whip cupcake",
    "winter wreath",
    "xmas stocking",
    "input key",
    "fresh socks",
    "clover pin",
    "faith amulet",
    "happy brownie",
    "ice cream",
    "instant ramen",
    "mousse cake",
    "money pot",
    "pretty posy",
    "mood pack",
    "chill flame",
    "liberty figure",
}

# ── Дорогие — строго по названию (нижний регистр) ──
DEAR_TITLES = {
    "heart locket",
    "mighty arm",
    "durov's cap",
    "precious peach",
    "nail bracelet",
    "plush pepe",
    "astral shard",
    "low rider",
    "gem signet",
    "mini oscar",
    "scared cat",
    "loot bag",
    "perfume bottle",
}

# ── NFT от 10 ton — строго по названию (нижний регистр) ──
NFT10_TITLES = {
    "astral shard",
    "bonded ring",
    "crystal ball",
    "diamond ring",
    "electric skull",
    "eternal rose",
    "flying broom",
    "genie lamp",
    "jack-in-the-box",
    "kissed frog",
    "love potion",
    "mad pumpkin",
    "magic potion",
    "record player",
    "skull flower",
    "sharp tongue",
    "signet ring",
    "swiss watch",
    "top hat",
    "toy bear",
    "trapped heart",
    "vintage cigar",
    "voodoo doll",
    "cupid charm",
    "valentine box",
    "snoop cigar",
    "ionic dryer",
    "sky stilettos",
    "khabib's papakha",
    "ufc strike",
    "rare bird",
    "bling binky",
    "coffin",
    "durov's boots",
}

# ── Upgrade — мониторинг улучшений ──
# Имена коллекций берутся автоматически из данных рынка (resale listings).
# Для каждой коллекции бот отслеживает availability_issued и оповещает
# о каждом новом улучшении.
UPGRADE_SLUGS = [
    "SantaHat", "SignetRing", "PreciousPeach", "PlushPepe", "SpicedWine",
    "JellyBunny", "DurovsCap", "PerfumeBottle", "EternalRose", "BerryBox",
    "VintageCigar", "MagicPotion", "KissedFrog", "HexPot", "EvilEye",
    "SharpTongue", "TrappedHeart", "SkullFlower", "ScaredCat", "SpyAgaric",
    "HomemadeCake", "GenieLamp", "LunarSnake", "PartySparkler", "JesterHat",
    "WitchHat", "HangingStar", "LoveCandle", "CookieHeart", "DeskCalendar",
    "JingleBells", "SnowMittens", "VoodooDoll", "MadPumpkin", "HypnoLollipop",
    "BDayCandle", "BunnyMuffin", "AstralShard", "FlyingBroom", "CrystalBall",
    "EternalCandle", "SwissWatch", "GingerCookie", "MiniOscar", "LolPop",
    "IonGem", "StarNotepad", "LootBag", "LovePotion", "ToyBear",
    "DiamondRing", "SakuraFlower", "SleighBell", "TopHat", "RecordPlayer",
    "WinterWreath", "SnowGlobe", "ElectricSkull", "TamaGadget", "CandyCane",
    "NekoHelmet", "JackInTheBox", "EasterEgg", "BondedRing", "PetSnake",
    "SnakeBox", "XmasStocking", "BigYear", "HolidayDrink", "GemSignet",
    "LightSword", "RestlessJar", "NailBracelet", "HeroicHelmet", "BowTie",
    "HeartLocket", "LushBouquet", "WhipCupcake", "JoyfulBundle", "CupidCharm",
    "ValentineBox", "SnoopDogg", "SwagBag", "SnoopCigar", "LowRider",
    "WestsideSign", "StellarRocket", "JollyChimp", "MoonPendant", "IonicDryer",
    "InputKey", "MightyArm", "ArtisanBrick", "CloverPin", "SkyStilettos",
    "FreshSocks", "HappyBrownie", "IceCream", "SpringBasket", "InstantRamen",
    "FaithAmulet", "MousseCake", "BlingBinky", "MoneyPot", "PrettyPosy",
    "KhabibsPapakha", "UfcStrike", "VictoryMedal", "RareBird", "MoodPack",
    "PoolFloat", "TimelessBook", "ChillFlame", "ViceCream", "SurgeBoard",
    "LibertyFigure", "DurovsGlasses", "FinePen", "IntelligenceCup",
    "AlgorithmCup",
]

# Интервал проверки upgrade-коллекций (сек)
UPGRADE_POLL_SECONDS = 30

# ── Общие фильтры (применяются ко всем категориям) ──
MAX_RATING = 2          # не больше 2 уровня
MAX_NFT_GIFTS = 10      # не больше 10 NFT в профиле

# Женские имена (нижний регистр) — для фильтрации «Girls»
FEMALE_NAMES = {
    # ── Русские ──
    "аврора", "агата", "агния", "агриппина", "ада", "аделина", "адель",
    "адриана", "азалия", "акулина", "алана", "александра", "алёна",
    "алина", "алиса", "алла", "альбина", "альмира", "амалия", "амина",
    "амира", "анастасия", "ангелина", "анжела", "анита", "анна", "антонина",
    "аполлинария", "арина", "арсения", "асия", "афина", "белина", "божена",
    "валентина", "валерия", "варвара", "василиса", "василина", "вера",
    "вероника", "виктория", "виола", "виолетта", "галина", "глория",
    "дарья", "диана", "дина", "доминика", "екатерина", "елена", "елизавета",
    "ефимия", "жанна", "ждана", "забава", "залина", "зара", "зинаида",
    "злата", "зоя", "инесса", "инна", "ирина", "ия", "калина", "камалия",
    "карolina", "кира", "ксения", "кристина", "лариса", "левана", "леокадия",
    "лиана", "лилия", "лилияна", "линия", "лита", "лариса", "луиза",
    "любовь", "людмила", "маира", "майя", "маргарита", "маргаритка",
    "марина", "мария", "марта", "марьяна", "матрёна", "мефодия", "мирослава",
    "милица", "надежда", "нана", "нара", "настасья", "настя", "нелли",
    "нина", "нора", "оксана", "олеся", "ольга", "маргарита", "пелагея",
    "полина", "прасковья", "рада", "разина", "регина", "римма", "родионовна",
    "роса", "рубановна", "руслана", "руфина", "светлана", "снежана",
    "софия", "софья", "станислава", "сусанна", "таисия", "таисия",
    "тамара", "тема", "тerea", "устина", "уляна", "фаина", "фатима",
    "фёкла", "христина", "эвелина", "элла", "эльвира", "эльмира",
    "эмма", "юлиана", "юлия", "яна", "ярослава",

    # ── Английские / западные ──
    "abigail", "ada", "adeline", "adrianne", "agatha", "agnes", "aileen",
    "aimee", "alaina", "alana", "alaska", "alba", "alberta", "alejandra",
    "alexa", "alexandra", "alexandria", "alexis", "alice", "alicia",
    "alina", "aline", "alisa", "allegra", "allison", "alma", "alondra",
    "amber", "amelia", "amanda", "amberly", "amie", "amy", "ana",
    "anastasia", "andrea", "angela", "angelica", "angelina", "angie",
    "anita", "ann", "anna", "annabelle", "anne", "annette", "april",
    "arabella", "ariadne", "ariel", "ariana", "arianna", "arlene",
    "ashley", "aubrey", "audrey", "augusta", "aurora", "autumn",
    "ava", "avery",

    # ── Испанские / португальские / итальянские ──
    "abella", "abigail", "adelaida", "adela", "adriana", "alejandra",
    "alexis", "alicia", "amanda", "amara", "amaya", "amelia", "amira",
    "ana", "anabella", "andrea", "angela", "angelina", "anita", "anna",
    "antonia", "ariana", "belen", "camila", "carla", "carmen", "carolina",
    "catalina", "clara", "claudia", "constanza", "cristina", "daniela",
    "diana", "elena", "elisa", "elisabetta", "elvira", "erica", "esmeralda",
    "estefania", "estela", "estrella", "eva", "fabiola", "fernanda", "flor",
    "francesca", "gabriela", "ginevra", "giorgia", "giulia", "giovanna",
    "grecia", "griselda", "helena", "hugo", "iliana", "ilmira", "isabella",
    "isabel", "isidora", "ivanova", "jazmin", "jessica", "julia", "juliana",
    "julietta", "karen", "karla", "katherine", "katia", "kira", "laura",
    "leticia", "lia", "liana", "lili", "liliana", "lina", "liz",
    "lizbeth", "lorena", "lucia", "luciana", "lucrecia", "luisa",
    "marcela", "marco", "maria", "mariana", "marina", "marta", "martina",
    "mercedes", "micaela", "mila", "milagros", "milena", "monica",
    "monique", "nadia", "nancy", "naomi", "natalia", "natalie", "nerea",
    "nikole", "nina", "noelia", "noemi", "olga", "pamela", "paola",
    "patricia", "paula", "paulina", "priscila", "raquel", "rebekah",
    "regina", "regina", "renata", "ricarda", "rocio", "rosa", "rosario",
    "rosenda", "rosita", "rudolf", "sabrina", "salome", "samantha",
    "sandra", "sara", "sofia", "soledad", "sonia", "sofia", "sophia",
    "susana", "tamara", "tatiana", "teresa", "thalia", "tiana",
    "valentina", "valeria", "vanessa", "venus", "veronica", "victoria",
    "violeta", "virginia", "ximena", "yolanda", "zara", "zoey", "zoe",

    # ── Французские ──
    "adeline", "adrienne", "agathe", "agnes", "aimee", "albane", "aline",
    "amandine", "amélie", "anastasie", "andrea", "angelique", "anne",
    "annick", "antoinette", "arabella", "ariane", "audrey", "aurore",
    "beatrice", "benedicte", "bernadette", "brigitte", "camille",
    "caroline", "catherine", "cecile", "chantal", "charlotte", "chloe",
    "christelle", "claire", "clotilde", "constance", "corinne", "danielle",
    "diane", "dominique", "dorothee", "edwige", "elena", "elise",
    "eloise", "emilie", "emma", "estelle", "eugenie", "eulalie",
    "eva", "eve", "fanny", "flavie", "florence", "francoise", "gabrielle",
    "germaine", "gwenaelle", "heloise", "henriette", "isabelle", "jacqueline",
    "jennifer", "jessica", "juliette", "justine", "karine", "laetitia",
    "laure", "lauren", "leila", "lorraine", "louise", "lucie", "madeleine",
    "manon", "marianne", "marie", "marina", "marine", "marion", "mathilde",
    "michele", "mireille", "monique", "nadia", "nadine", "nathalie",
    "nicole", "nina", "noemie", "olivia", "paulette", "perrine", "priscilla",
    "rachel", "raphaelle", "regine", "rosalie", "roseline", "sabine",
    "sandra", "sarah", "simone", "sophie", "stephanie", "sylvie",
    "tamara", "valerie", "veronique", "victoire", "vivienne",

    # ── Немецкие / голландские / скандинавские ──
    "adela", "adelheid", "agnes", "alma", "amalia", "amalie", "anita",
    "annalena", "annika", "antje", "astrid", "babetta", "barbara",
    "beate", "berit", "britta", "brunhilde", "carla", "carolina",
    "charlotte", "christel", "christiane", "christina", "clara",
    "dagmar", "dana", "daniela", "dorte", "dorothea", "edith",
    "elisabeth", "elke", "ella", "elsa", "emilia", "emma", "erika",
    "esther", "eva", "felicitas", "felicia", "florentine", "franziska",
    "frieda", "greta", "griet", "gunda", "gustava", "hana", "hannah",
    "heide", "heidi", "hella", "helga", "henriette", "hermine",
    "hilda", "hilde", "ida", "ilse", "inga", "ingrid", "irene",
    "irina", "iris", "isabel", "isabell", "jakobea", "jana",
    "janina", "jasmin", "jennifer", "jenny", "jerusa", "jette",
    "johanna", "josefine", "julia", "juliane", "justine", "kaatje",
    "karin", "karina", "karla", "katarina", "katharina", "kati",
    "katja", "katrin", "keila", "kerstin", "kira", "klara", "konstanze",
    "laila", "lara", "lara", "laura", "lena", "leni", "lera",
    "lieschen", "liese", "lilian", "linda", "lisbeth", "lorena",
    "lotta", "lotte", "louise", "luca", "lucia", "luisa", "maja",
    "manuela", "marcela", "maria", "marian", "marie", "marina",
    "marlene", "mathilda", "maureen", "melanie", "melissa", "merle",
    "meta", "micaela", "mila", "miriam", "mona", "monica", "nadia",
    "nina", "nora", "olga", "petra", "pia", "pirjo", "ragnhild",
    "regina", "renate", "ronja", "rosa", "rosalie", "rosmarie",
    "rubina", "sabine", "sandra", "sara", "selma", "sienna",
    "sigrid", "silke", "simone", "sofia", "sonja", "sophia", "sophie",
    "stefanie", "susanne", "sybille", "tamara", "teresa", "theda",
    "thora", "tilde", "trude", "uda", "ulrika", "ursula", "uschi",
    "valentina", "valeria", "vanessa", "verena", "victoria", "walburga",
    "wencke", "wibke", "wiebke", "wilhelmine", "yvonne", "zara",

    # ── Японские / корейские / китайские ──
    "акико", "аои", "ариса", "ацуко", "аой", "хана", "харука",
    "химари", "ику", "каори", "каэдэ", "кикуко", "маи", "маки",
    "мидори", "мика", "мисаки", "мия", "нана", "нанами", "наomi",
    "норико", "рэй", "рику", "сакура", "сатоми", "саяка", "томоко",
    "уиру", "утида", "юка", "юки", "юко", "юри", "ёко",
    "киран", "мидзухо", "котонэ", "саори", "таэко", "цукико",
    "юико", "ёсино", "аой", "хината", "мицуко", "сигэко",
    "киёмидзу", "томоэ", "ихэя", "сэйко",
    "유진", "지은", "수진", "민지", "서연", "예원", "하은", "지우",
    "채원", "윤서", "아린", "다은", "서연", "지호", "은채", "나은",
    "敏", "静", "丽", "芳", "娜", "颖", "琳", "玲", "婷", "雪",
    "慧", "佳", "欣", "雅", "梦", "瑶", "薇", "蕾", "思", "怡",
    "涵", "萱", "琪", "馨", "月", "雨", "晴", "悦", "彤", "宁",

    # ── Арабские / персидские / тюркские ──
    "آتنا", "آيدا", "أسيل", "أماني", "أميره", "إيناس", "بسمة",
    "تالا", "حنين", "دانية", "ديمة", "رنا", "رنيم", "سارة",
    "سلمى", "سمية", "شهد", "شيرين", "عايشة", "فاطمة", "قمر",
    "ليلى", "لينا", "مريم", "نورة", "هبة", "هند", "ياسمين",
    "نور", "لين", "arty", "parisa", "mahsa", "sara", "nazanin",
    "maryam", "yeganeh", "asal", "golnaz", "sanaz", "lehna", "niloufar",
    "shirin", "lehna", "elham", "azadeh", "nazila", "mahnaz", "pouya",
    "roksana", "shadi", "yasmin", "zohreh", "mahnaz", "haleh", "mona",
    "aylin", "elif", "eylül", "zeynep", "elif", "sena", "büşra", "gamze",
    "ebru", "bruçe", "ceren", "derya", "deniz", "eda", "ezgi", "fatma",
    "fulya", "gizem", "gülşen", "halime", "hande", "harika", "hilal",
    "irem", "jale", "kübra", "leys", "merve", "mutlu", "nihan", "ölüm",
    "pınar", "rüya", "safiye", "selin", "selma", "sevda", "sevil",
    "sezen", "songül", "şebnem", "şule", "tuğba", "tuba", "yasemin", "yeliz",

    # ── Индийские / восточные ──
    "aanjali", "aarti", "aditi", "ahana", "amara", "amisha", "amita",
    "ananya", "anika", "anita", "anjali", "anoushka", "anshula",
    "aparna", "bhavna", "bhumika", "chandrika", "charulata", "deepa",
    "devika", "divya", "gauri", "gita", "hema", "jalaja", "jaya",
    "kajol", "kalpana", "kanak", "kavita", "kirandeep", "lakshmi",
    "madhavi", "madhu", "malini", "meena", "meera", "mira", "mohana",
    "mohini", "naina", "nandini", "nandita", "narinda", "neelam",
    "nisha", "nita", "padma", "pallavi", "paramjit", "pooja", "poorna",
    "priya", "rachna", "radha", "ragini", "raina", "ramya", "ranjana",
    "ranjeeta", "riddhi", "ritika", "rupa", "sadhana", "sakshi",
    "sangeeta", "sanjana", "sapna", "sarika", "shailaja", "shakti",
    "shambhavi", "shanta", "shanti", "sharada", "shikha", "shreya",
    "shubha", "sneha", "sonal", "sonali", "sudha", "sujata", "suman",
    "sunanda", "sunita", "supriya", "sushma", "swati", "tara", "udita",
    "uma", "vaidhi", "varsha", "veena", "vidya", "vijaya", "vimla",
    "vinita", "violet", "yamini", "yashoda",

    # ── Африканские ──
    "abiola", "adama", "adedeji", "adjoa", "afua", "aisha", "akosua",
    "akua", "ama", "amara", "amina", "anika", "asante", "ashanti",
    "ayanna", "azaliya", "bolaji", "binta", "busisiwe", "chinwe",
    "dab起来", "dagmawi", "dalitso", "dando", "efua", "ehiose",
    "eires", "eunice", "faiza", "farai", "fatimah", "folake",
    "folashade", "genna", "ghirmai", "hadiza", "hawa", "ibukun",
    "ife", "ifeoma", "imani", "zuri", "jia", "kadiatou", "kagiso",
    "kala", "kamila", "kanayo", "karabo", "kefilwe", "khadijah",
    "khalia", "kindness", "kinfe", "laila", "lesedi", "limbani",
    "lindiwe", "lira", "lydia", "maha", "mahlet", "maisi", "makena",
    "malika", "mami", "mandisa", "maria", "marjani", "maurine",
    "mawuli", "maysa", "meron", "mina", "miriam", "monde", "mpho",
    "nana", "nandi", "nanle", "nontando", "nosa", "nubia", "nyakim",
    "nyaradzo", "omotola", "phuti", "rashida", "rebecca", "rifqa",
    "ruqayyah", "saafi", "sade", "safiyah", "salma", "sara",
    "shadia", "shani", "shante", "sharon", "sidi", "simone", "siphosethu",
    "socorro", "sophia", "sumayyah", "taliba", "tendai", "thandiwe",
    "titilayo", "tomi", "tunde", "uriel", "valerie", "waberi",
    "wairimu", "wanjiru", "widaad", "yaa", "yemi", "zainab", "zanele",
    "zulaikha", "zuri",

    # ── Вьетнамские / тайские / индонезийские ──
    "anh", "bach", "cam", "chi", "chau", "dat", "diem", "duyen",
    "ga", "ha", "hanh", "hien", "hoang", "huong", "huệ", "khanh",
    "kim", "lan", "le", "lien", "linh", "ly", "mai", "my", "nga",
    "ngan", "ngoc", "nhi", "nhung", "nu", "oanh", "phuong", "phan",
    "quynh", "son", "suong", "tha", "thao", "thi", "thu", "thuy",
    "trang", "trinh", "truc", "tuyet", "tuyen", "van", "vy",
    "只怕", "dao", "bong", "chau", "diep", "gia", "han",
    "ถุงเงิน", "ดาว", "น้ำ", "บัว", "ปราง", "ฝัน", "มด",
    "明", "月", "花", "英", "玉", "蓮", "鳳", "兰", "梅", "桂",
    "wulan", "sari", "dewi", "sri", "putri", "maya", "luna",
    "anisa", "nisa", "aya", "yuki", "bella", "citra", "dina",
    "eni", "erin", "fitri", "gita", "hesna", "ida", "ina",
    "jannah", "karina", "lina", "lisa", "mila", "mila", "nina",
    "novi", "putri", "ratna", "rita", "sari", "sinta", "sri",
    "suci", "susi", "tari", "tina", "utari", "wati", "yani",
    "yuni", "zahra", "zain",

    # ── Доп. женские формы / уменьшительные ──
    "катюша", "катя", "катенька", "даша", "дашенька", "лена", "леночка",
    "аня", "анечка", "маша", "машенька", "оля", "оленька", "наташа",
    "наташенька", "света", "светочка", "любаша", "людочка", "ира",
    "ириночка", "таня", "танечка", "жура", "женька", "капя", "капи",
    "-Agent", "надя", "наденька", "марина", "мариночка", "полина",
    "полиночка", "софья", "софийка", "вера", "верочка", "нина",
    "ниночка", "зина", "зиночка", "зоя", "зоенька", "раиса",
    "раисочка", "галина", "галиночка", "виктория", "викторочка",
    "валентина", "валентиночка", "валерия", "валерочка", "юлия",
    "юленька", "яна", "яночка", "арина", "ариночка", "кира",
    "кирочка", "леся", "лесечка", "лаиса", "лайочка",
    "angel", "princess", "queen", "lady", "miss", "snow", "lily",
    "rose", "daisy", "violet", "iris", "ruby", "pearl", "coral",
    "amber", "jade", "opal", "garnet", "crystal", "stella",

    # ── Женские ники Telegram ──
    "ӂέȞά_άλúώέρά", "цветочек", "stdafxа", " cry_baby", "стервочка",
    "life_is_life", "adelia", "bloodymary", "brednaя", "demonix@",
    "hedoctiiha9i", "kanpu[zzz]ka", "kys04ek_zla", "lady|fox", "lina",
    "litlle_princess", "tanchik", "tboyi_cy4ka", "yairam", "аннонимка",
    "дракоша", "конфетк@", "милка", "невеставетра", "пофигистка",
    "папина_доча", "фисташка", "хинаточка", "цыганочка", "шипучка",
    "аяllя", "ąℓɨɲą", "līšîҷҝã-иçтįрîҹҝå", ".checkBox", "ℙɣçℭҝꭿя",
    "зßезда", "детя_ангела", "нiкобася", "зая", "100%%%_ляпочка",
    "3aya_u3_capaya", "адммисок", "аристократка", "b♡i♡t♡c♡h",
    "belladonna", "bruneto4ka", "butamuhka", "steravrayona",
    "detk@", "eva_kiss", "fant@", "irinoh", "kart4werka", "marixuanna",
    "milkа", "mimimimonster", "molly", "neonka", "pandora",
    "princessa_aiwa", "rainbow_girl", "roxy", "tvoi_mechty",
    "ангел_во_плоти", "ангел_из_рая", "бабница", "брюнеточка",
    "ветерок", "дама_судьбы", "кислая", "легендочка", "мего-цыпа",
    "мейби_бейби", "не_твоя_малая", "пинкамина", "рыжуля",
    "сладкая_вишня", "харли_квин", "шахзода", "эльза", "ягодка",
    "stervоchka", "凤凰", "","#березка", "emilia", "персик",
    "мыфка", "wolfsoxlika", "арлианна", "заяties", "milka",
    "fox", "березка", "бězумная", "бellа",
    "love", "рэйн", "coffemanka",
    "красотка", "kira", "ahapka", "stka",
    "coffe", "loucy", "ледяная", "marshmelka", "medusa",
    "melkaya", "nazima", "nellya", "ninochka", "radmila", "sabina",
    "tabypeto4ka", "tatarka", "tyanochka", "wasabi", "xuliganKa",
    "zara", "анабель", "анэтта", "аферистка", "белоснежка",
    "вилочка", "гриня", "дари", "девушка_солдата", "демониха",
    "дерзкая_кошка", "джульетта_без_этикета", "дочь_сатаны", "женькина",
    "жучка_свечкина", "забытая", "имба", "инопланетянка", "ирэн",
    "каramelьkа", "кекси", "кеосем_султан", "клеопатра", "кроха",
    "крутая_бандитка", "лерика", "мадара", "маковка", "мандаринка",
    "мерега", "муся", "надежда", "наоми", "новелла", "няфка",
    "огненная", "пантерка", "перизат", "пофигистка", "росковья",
    "рина", "сама_не_своя", "севилия", "сосокровка", "стеша",
    "талисманка", "твоё_солнце", "три_зайки", "тэффи", "филя",
    "химера", "хранительница",
    "sunflower", "suka", "богатая", "лида",
    "розэшка", "weska", "агнусинька", "влюблённая_красавица",
    "vreemennyy_gost", "crazy.girl", "лизза", "твапь",
    "dayнка", "n0-s-u-k-a", "belli_eilish",
    "радужная", "цветочек", "ёлочка", "game_kvien",
    "derzkaya", "beiбi_tik_tok", "its_a_pink_salat",
    "я_красотка", "хулиганка", "умка", "коняка",
    "astasia", "delegatechka_s_xvostikom",
    "uadka_takaya", "bombay",
    "camaya_mborya", "giла",
    "tolko_tvoya", "непотерянная",
    "yuliya", "dayana",
    "рыжая", "млоллана", "кореллаззаая",
    "крылья_светов", "чемпион",
    "marika", "рыська", "pom",
    "eva", "никар@",
    "crzy", "минакakа",
    "sonikса", "aboba", "неномпаньная_девка",
    "мисс_квин", "queen", "сук@", "queeN",
    "буду_твоей", "love_you", "elli", "только_твоя",
    "beautiful", "тв0янявсюжизнь", "чёренашка",
    "iriska", "атомная_бомба",
    "palamed", "parmel",
    "miss.quinn", "сук@",
    "ant", "митероритка",
    "little_fox", "angel", "queen", "звезда", "iris",
    "stella", "стальна", "панда",
    "beautiful",
    "coffemanka",
    "милка", "love", "твоя_навеки",
    "bbя", "berta",
    "babamyaso", "бабка", "bady", "bad_sasha",
    "bagirochka", "bariga", "beat_girl", "beka", "best_girlfriend",
    "blestka", "blondy", "bomba_girl", "borobka",
    "borodahka", "brave_spirit", "bryunetochka",
    "bupa", "buterfly", "crazy_fox",
    "calibri", "candy_girl", "charming", "cherrycat",
    "chumovayatyan", "confetka", "corolla", "cosmos_girl",
    "steravochka", "cuamckaya", "calibri", "dashylka",
    "dasshe4ka", "daya", "dayana", "deb_girl", "deerzkaya",
    "delovaya", "derzkay", "devka", "dikaia_kracotka",
    "dikaya_zvezda", "dileka", "dilora", "dinastya",
    "doch_maminoy_podrugi", "dolysmeikahun", "dora_dura",
    "drakosha_tyan", "dyro4ka",
    "egaproska", "ejotala", "elija", "emeli", "emil_g",
    "eminika", "emuba", "epoda", "erdana", "ereha", "eresa",
    "eriva", "evо4ka", "ewanapaly", "ewe_may",
    "fafne", "fari", "fatima", "fatima_star", "favoruda",
    "fedeka", "femka", "fenomenalka", "flicyyy", "flopa",
    "flowerangel", "flowers", "fohoma", "folada", "frizya",
    "fro3en", "frozen_heart", "frozzy", "fsaro", "fuma",
    "furia", "futi", "f_a_n_a_t_k_a", "gabriela", "gabriella",
    "garza", "gea", "gerra", "gevanel", "geza",
    "giena", "girl_an_the_moon", "gorza", "grill",
    "guardian_girls", "guzaliya", "hanoika", "harli_kvin",
    "hayabusa", "heriga", "herokillersha", "herry",
    "hewa", "hebeсnaяdewyshka", "hima", "hit-girl",
    "holyspirits", "hovnan", "humblelady", "huroza",
    "igedxira", "iintriga", "irainee", "iriska",
    "ivetta", "izana", "iziqo", "izuna", "jacina",
    "jefacuxy", "jlehnuik", "jlamporchka", "juja",
    "juletta", "jycivonem",
    "kakazaba", "kamewek", "kapamelьka", "kapriz",
    "karen", "karoc4ka", "kartoshe4ka", "katenok",
    "kawaii_santa", "kefirka", "kenal", "kesha",
    "kgoleqa", "ki4a", "kilary", "kisa_v_satane",
    "kiska", "kisа_v_kedah", "kiss-my", "kiti",
    "kloepatraaa07", "konchenaя", "konfio", "koniklida",
    "kopeechka", "koroleva", "korine", "korobo4ka",
    "koshmar", "kosya04ok", "kotik_demona", "kracavica",
    "krasiva", "ksenia", "ksuha_lyubov",
    "kuanyshgul", "kuza", "lady_of_the_north", "lafuka",
    "laiselleska", "lala", "lama", "lamartina", "landish",
    "lany", "lavuru", "ledy", "ledy_dragon", "legendarnaya",
    "lenusia", "leta", "lil_pip", "lila", "linansty",
    "linaxpi", "linsa", "liza", "lizka", "lola", "lora",
    "lorochka", "lost_girl", "lozzy", "luji", "luli",
    "luna_wolf", "luni", "lunnay", "lusyk", "luza", "lyna",
    "milana", "malaoua", "m1uzzy", "mad_girl", "madama",
    "malenkaya_malishka", "malin04ka", "malyshka_is_raya",
    "mandavoshka", "manner", "mapisha", "margarita",
    "marino4ka", "marityanka", "maschula_keks", "mashe4ka",
    "mehrobanna", "melya", "memorable", "merelin", "metiska",
    "meybl", "mia_moor", "milanks", "milashka",
    "milashka090", "milawka", "milkа", "milla_miller",
    "millie", "milva", "miralida", "mishell", "mishura",
    "miss_angel", "miss_mia", "miss_няша", "miwers", "mizuriks",
    "moha", "mokki", "monamoz", "ms_chetverus", "mukha",
    "mulka", "muney", "myda",
    "нагибаторша", "nagiko", "nancy", "narka", "naryadn",
    "nataKotik", "naynanotthot", "neerwana", "nensy", "nezumi",
    "nicizi", "niec", "niella", "nikamimi", "nikol",
    "nina-olenina", "niru", "nishinoya", "nitefi", "niuga",
    "nodcake", "nohyna", "npo100lalka", "nutelka", "nuxa",
    "nyqixenype", "obha", "osci", "odinokaja_kisa",
    "ofialisa", "okiva", "opuga", "otteca",
    "pandora", "pandochka", "paulig", "pbl6exa", "pdebegi",
    "pelageja", "pidoea", "pipa", "pipuka", "polinchik",
    "poly", "poMIdoRKaa", "pozitivka", "princess_dream",
    "princess_mira", "prinssa", "pritty_lady", "pro100_nyasha",
    "promo_girl", "queen", "quenni", "raika",
    "rainbow_cream", "rawipa", "rayana", "reimbai", "riaba",
    "rinа_tyan", "roga", "rolodeta", "rubina", "rubrika",
    "sankatumа", "s1s1chkа", "sad_bitch", "salamandra",
    "salamandra", "sanka", "sayonara_girls", "sencha",
    "shokolatka", "shopkina", "shor_gagarina", "shuk@",
    "sl1vki", "sleepingprincess", "slmiranda", "snizhana",
    "sofa", "sofa_sun", "sophi", "sora", "soso4ka",
    "sowina", "star_girl", "sterva", "steravochka", "runa",
    "sunny_peach", "suujia", "sweet_honey", "sweet_ded",
    "syzo", "taisiya", "tanachymo", "tar4a", "tarasia",
    "tatka", "tbiKobka", "tboyi_dura", "tboyi_lyubimayi",
    "tboyi_niha", "teqilkaa", "thegoldbaby", "tiana", "tina",
    "tolko_tvoya", "toma", "top_girl_happy", "torpeda",
    "tuchka", "uba", "ubetok_ayotosa", "uikksa", "ulia",
    "uri", "uzohura", "uzroza",
    "vlyublena_vsyu_zhizn", "vasilina", "valeriya",
    "vanka", "vanmasha", "vechno_blyayha", "velina", "vena",
    "vernaya", "veronicka", "vertuaika", "vibyma", "vika_4ika",
    "viplive", "vip_kiska_vip", "vpisska", "waza", "wolfaxela",
    "wolfi_cat", "wyv", "xamura", "xentatyano4ka", "xila",
    "ximoza", "xumote", "xymena", "xyna",
    "yaga", "yama", "ydyguve", "yenellefer", "ykina", "yluly",
    "yota", "yrire", "zaika_s_nebes", "zama", "zanoza", "zaura",
    "zaure", "zayabusinka", "zhanali", "zhanel", "zimina",
    "zlatovlaska", "zoja", "zovana", "zureks", "zymiine",
    "zanosa", "адрина", "адалия", "айра", "айяру", "аква",
    "алоэ_бич", "американка", "амурочка", "анимешка",
    "аниме_ангел", "анрюша", "анфиса", "арока", "аська",
    "бакосья", "барби", "барига", "безруシкофф",
    "безумная_дьяволица", "белая_роза", "белорусска",
    "бесичка", "беспощадная", "бестыжая", "бесценная",
    "бесячка", "бешаная", "бигагуль", "богдана",
    "богиня_добрыоты", "богиня_таяна", "булочка_с_корицей",
    "бухая_фея", "бэйби_гэби", "в_маминой_кофте", "вегги",
    "ведьма", "великодушная_леди", "взгляд_ангела", "викутя",
    "вредная_зая", "вредная_мышка", "вреднюшка", "гарпия",
    "гера", "глория", "голубоглазая", "гордая_девочка",
    "гордая_тигрица", "гречанка", "даффина", "девочка_каф",
    "девочка_под_каф", "девушка_химия", "девушка_в_кедах",
    "дерзкая_штучка", "детка_я_крутая", "дженни", "джульетта",
    "дикая_дивчинка", "дикая_рысь", "дильнура", "доминаторша",
    "дора_дура", "доця", "дочка_путина", "дочь_крылой",
    "драмионна", "дюня", "его_малая", "единарожа",
    "елизаздра", "жасмина", "жду_солдата", "жестокая_стерва",
    "забывная_блоха", "забыта_сном", "загадочная",
    "зая_из_рая", "зая_сегодня_злая", "звёздочка_твоя",
    "земфира", "зена", "зеная", "зечара",
    "злая_от_того_что_знаю", "злая_зая", "злая_жучка",
    "злая_кися", "злая_снегурка", "злYsha_moya", "зубная_фея",
    "избранная_небом", "инопланетяшка", "ириндиска",
    "искрометная_леди", "клубникабомба", "кайфовая_зая",
    "кама", "камари", "каталея", "катька", "кекси_фрау",
    "кератина", "килла", "кисуаме", "кискакексилюбит",
    "кнопа", "козырная", "кокакола", "колючая",
    "коралева_фламинго", "коран_и_сунна", "кошка_зеленоглазая",
    "крами", "красная", "краснаяроза", "красотка_зажигательная",
    "краш", "крашиха", "крёстная", "крутая_чико4га",
    "кука", "курута", "кусяюсь", "ламия",
    "леди_вамп", "ледяная_леди", "леро4ка",
    "лиганда", "лиса_звезда", "лисичка_огонёк",
    "лунная_кошечка", "любила_тебя", "любимая_твоя", "люма",
    "мадмазель", "майсорим", "мамалак", "мамиладка",
    "марлен", "марлин", "маська", "мелиначка",
    "мерлин", "милая_обманщица", "милая", "миночка",
    "минерало4ка", "мисс_аксель", "мой_только_мой",
    "монеси", "мышка_сосиска", "мышьбелая", "мэйбал",
    "мюня", "навелла", "нагибаторша", "насия",
    "нервотрепка", "ника", "николетта", "нуттела",
    "няфкаа", "няффка", "няха_забивняха",
    "околдовала", "опасная", "остролистая",
    "патриция", "паника", "пантера", "персефона",
    "персичек", "пипа", "плазма", "плюшевая",
    "полли", "пони_головая", "проказница", "простая",
    "пушка", "пчёлка", "пьяная", "радуги",
    "режина", "рейн", "ренгай", "ронда_роуз",
    "chokolatka", "сама_по_себе", "самика", "сбежавшая_невеста",
    "сегодня_я_добря", "сексуальная", "серцеедка",
    "синеглазая", "синеглазка", "сиюй", "слада",
    "сладкая_девочка", "слендерина", "слишком_красивая",
    "слоумоша", "сметанка", "совочка", "солныфко",
    "софушка", "срэлла", "стальная", "страная_панда",
    "страшная_карта", "сумасшедшая", "сумасшеднее_солнышко",
    "таблетка", "твой_космостар", "твой_мур", "твоя_навеки",
    "твоя_пусечка", "твоя_штучка", "твоя_я",
    "твоя_непонятная", "твоя_прелесть",
    "твоя_принцесса_анабель", "темыя", "тереза", "тефида",
    "тигрица", "топовая_тян", "тори", "трикс",
    "тэми", "тян_твоих_мечтаний", "тянеа", "тянка", "тянока",
    "улиточка", "уля", "уматная_матильда", "утка",
    "фаворида", "фаришта", "фимка", "фиона", "флаттершай",
    "хакерша", "ханна_монтана", "хилиганка", "хищница",
    "хорошенькая_кнопочка", "хранительница.ангелов",
    "хулиганочка", "хюля_пуля", "царевна", "царица",
    "цветочек", "цветочки", "черепаха", "черная_канарейка",
    "черная_лилия", "черника", "чертовская", "чёткая",
    "чиканутая", "чикибамбони", "чонка", "чудная",
    "чупакабра", "шайнинг", "шальная_аристократка", "шамуля",
    "шева", "шлепон_ка", "щастливая_из_ада",
    "эллии", "энигма", "эфа", "эффектная",
    "юлианна", "юни", "юпитер",
    "я_девушка_тихая", "я_его_слива", "я_зая", "я_королевна",
    "я_космос", "я_любила_тебя", "я_твоя_детка",
    "ягодина", "яночка", "японка", "яролика",
    "ясеневская", "ясмина", "ятвоятян",
    "madama", "целуй_меня_нежно",
    "золотая_гулька", "расавица",
    "папина_дочка", "вспышка", "дерзкая_девка",
    "lune", "алоголиха",
    "девочка_классная", "я_её", "пахда",
    "бомба", "я_нестерва", "красавица",
    "iva", "мисс_принцесса",
    "перчинга", "матерь_драконова",
    "королева", "maneka",
    "angel", "пуговка", "stuff_girl",
    "viktoria", "девка_занята",
    "коpолевна_в_короне", "billie",
    "зая",
    "клиюхина", "лампочка",
    "juja", "jycivonem", "kamewek", "kapamelьka",
    "karen", "karoc4ka", "kartoshe4ka", "kawaii_santa", "keshа",
    "kisа_v_sаtаne", "kloepatraaa07", "konchenaya", "koTiK_dEmOna",
    "krасavica", "krasiva", "ksenia", "kuanyshgul", "lady_of_the_north",
    "lafuka", "lala", "lama", "lamartina", "lany", "lavyru", "ledy",
    "lenusia", "leta", "lila", "linsa", "lisa", "liza",
    "lozzy", "luna_wolf", "lunnay", "lusyk", "luza", "lyna",
    "milana", "m1uzzy", "mаlеnkaя_malishka", "malin04ka",
    "mashа", "mashа_cat", "mashа", "mashe4ka",
    "melema", "mell", "milanks", "milashka", "milaха",
    "milla_miller", "millie", "milva", "minochka",
    "mineralо4ka", "mishell", "miss_angel", "miss_mia",
    "miss_няша", "miwers", "molly", "monamoz",
    "mokki", "mukha", "mulka", "muneey", "myda",
    "навелла", "nagibatorsha", "nagiko", "nancy", "narka",
    "nastyaha", "nato4ka", "nina", "niru", "nisha",
    "nitefi", "niuga", "nodcake", "nutelka", "nuxa",
    "obha", "odinokaja_kisa", "olga", "oteka", "pandora",
    "paulig", "pelageja", "pipa", "polinchik", "polty",
    "poly", "poMIdorkaa", "pozitivka", "princess_dream",
    "prinssa", "pro100_няша", "queen", "raika", "rainbow_cream",
    "rayana", "reimbai", "riaba", "rina", "roga", "rubina",
    "sankа", "sara", "sencha", "shopkina", "sl1vki",
    "slmiranda", "snizhana", "sofa", "sophi", "sora",
    "soso4ka", "sowina", "star_girl", "sterva", "sunny_peach",
    "sweet_honey", "sweet_ded", "taisiya", "tarasia",
    "tatka", "tiana", "tina", "toma", "tuchka",
    "uba", "ulia", "uri", "uzohura", "uzroza",
    "valeriya", "vanka", "velina", "veronicka",
    "vika", "waza", "wolfi_cat", "xamura", "xila",
    "yaga", "yama", "yennefer", "ykina", "yluly",
    "yota", "yrire", "zara", "zaya", "zayabusinka",
    "zimina", "zoja", "zovana",
    # ── Ники (womanish patterns) ──
    "steravochka", "sterv4ka", "crazy_girl", "angl",
    "рыжуля", "pipuka", "ламия", "барби", "нюша", "шанс",
    "ника", "тайна", "радуга", "сказка", "удача",
    "звёздочка", "луна", "заря", "радость", "ласка",
    "нежность", "краска", "пена", "сова", "росинка",
    "ласточка", "зефирка", "вишенка", "птичка", "ромашка",
    "снежинка", "солнышко", "зайка", "мишка", "киса",
    "котёнок", "сладкая", "милая", "красивая", "добрая",
    "злая", "смелая", "дерзкая", "крутая", "шальная",
    "капризная", "нежная", "ласковая", "страстная",
    "огненная", "лёдяная", "загадочная", "таинственная",
    "волшебная", "необычная", "уникальная", "редкая",
    "прелесть", "королева", "принцесса", "леди", "мисс",
    "ангел", "фея", "русалка", "богиня", "звезда",
    "комета", "метеор", "астероид",
    "феникс", "единорог", "пегас", "грифон",
    "сирена", "вампир", "демон",
    "призрак", "тень", "фантом",
    "офелия", "джульетта",
    "анна", "мария", "елена", "наталья", "татьяна",
    "ольга", "ирина", "екатерина", "светлана", "людмила",
    "галина", "валентина", "лариса", "нина", "зинаида",
    "зоя", "раиса", "лидия", "валерия", "юлия",
    "дарья", "полина", "арина", "марина", "кира",
    "вероника", "алина", "алиса", "виктория", "ангелина",
    "ксения", "милана", "софия", "александра", "алёна",
    "елизавета", "маргарита", "амелия", "василиса",
    "стефания", "ульяна", "агата", "мирослава", "варвара",
    "снежана", "евгения", "лер катерина",
    "василина", "диана", "кристина", "антонина",
    "любовь", "марина", "лариса",
    "надежда", "лидия", "валерия", "юлия",
    "дарья", "полина", "арина", "марина", "кира",
    "вероника", "алина", "алиса", "василиса", "мирослава",
    "варвара", "снежана", "евгения",
    "софия", "александра", "елизавета", "маргарита", "амелия",
    "агата", "ульяна", "стефания", "ангелина", "ксения",
    "милана", "диана", "кристина", "арина", "полина",
    "дарья", "юлия", "валерия", "лидия", "нина",
    "зоя", "раиса", "зинаида", "светлана", "людмила",
    "галина", "валентина", "лариса", "надежда", "татьяна",
    "наталья", "елена", "ольга", "ирина", "мария",
    "екатерина", "анна", "антонина", "любовь", "марина",
}

# ID тем (запасной вариант, если MTProto не может прочитать форум)
FALLBACK_TOPICS = {}

# ------------------------- премиум-эмодзи (Bot API 9.4) -------------------------
def _tg_emoji(emoji, eid):
    return f'<tg-emoji emoji-id="{eid}">{emoji}</tg-emoji>'

EMOJI = {
    "new":       _tg_emoji("♥️", "5893406892092297627"),
    "model":     _tg_emoji("💫", "5895770017458294953"),
    "price":     _tg_emoji("💰", "5893473283696759404"),
    "stars":     _tg_emoji("📱", "5893034681636491040"),
    "gram":      _tg_emoji("🪙", "6039802097916974085"),
    "lvl":       _tg_emoji("📊", "5895444149699612825"),
    "quantity":   _tg_emoji("🏆", "5893376775781617954"),
    "premium":   _tg_emoji("📱", "5893034681636491040"),
    "prem_yes":  _tg_emoji("✅", "5895514131896733546"),
    "prem_no":   _tg_emoji("❌", "5893163582194978381"),
    "country":   _tg_emoji("👤", "5902335789798265487"),
    "status":    _tg_emoji("🕞", "5893102202817352158"),
    # Upgrade-эмодзи
    "upgrade_title": _tg_emoji("🌟", "5287674954119133403"),
    "upgrade_fire":  _tg_emoji("🔥", "5893185207355315979"),
    "upgrade_bg":    _tg_emoji("✨", "5893321843149902412"),
    "upgrade_flash": _tg_emoji("⚡️", "5893450623449305489"),
}

# Премиум-эмодзи на inline-кнопках (Bot API 9.4+): icon_custom_emoji_id
BTN_PROFILE_ICON = "6035305550625902723"
BTN_GIFT_ICON = "6032644646587338669"
BTN_RESERVE_ICON = "5879895758202735862"   # 🔒 — кнопка «Забронировать»

# Эмодзи для сообщения о брони в ЛС
EMOJI_RESERVE = {
    "money":   _tg_emoji("💰", "5375296873982604963"),
    "diamond": _tg_emoji("💎", "6028530359975548369"),
    "talk":    _tg_emoji("💬", "5985630530111020079"),
    "lvl1":    _tg_emoji("1⃣", "5794182096603847292"),
    "lvl2":    _tg_emoji("2⃣", "5794303034292968945"),
    "lock":    _tg_emoji("🔒", "5879895758202735862"),
}
BTN_PM_PROFILE_ICON = "5920344347152224466"   # 👤 — Open profile в ЛС
BTN_PM_GIFT_ICON = "6028530359975548369"      # 💎 — View gift в ЛС

# ========================================================================

BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ------------------------- язык/страна -------------------------
ARABIC_RANGES = [(0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF),
                 (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)]
CYRILLIC_RANGES = [(0x0400, 0x04FF), (0x0500, 0x052F)]
PERSIAN_EXTRA = set("\u067e\u0686\u0698\u06af")   # پ چ ژ گ — персидские буквы
PERSIAN_DIGITS = set("\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9")
HAN_RANGES = [(0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF)]

FLAG_COUNTRIES = {
    "\U0001F1EE\U0001F1F7": "Иран",   # 🇮🇷
    "\U0001F1E6\U0001F1EA": "ОАЭ",    # 🇦🇪
    "\U0001F1E8\U0001F1F3": "Китай",  # 🇨🇳
    "\U0001F1F7\U0001F1FA": "Россия", # 🇷🇺
}

# Код страны -> страна по префиксу телефонного номера (по самому длинному совпадению).
PHONE_COUNTRIES = [
    ("998", "Узбекистан"),  ("996", "Кыргызстан"), ("994", "Азербайджан"),
    ("993", "Туркменистан"), ("992", "Таджикистан"), ("995", "Грузия"),
    ("880", "Бангладеш"),  ("971", "ОАЭ"),         ("966", "Сауд. Аравия"),
    ("973", "Бахрейн"),    ("974", "Катар"),       ("965", "Кувейт"),
    ("968", "Оман"),       ("964", "Ирак"),        ("963", "Сирия"),
    ("962", "Иордания"),   ("961", "Ливан"),       ("967", "Йемен"),
    ("380", "Украина"),    ("375", "Беларусь"),    ("374", "Армения"),
    ("373", "Молдова"),    ("372", "Эстония"),     ("371", "Латвия"),
    ("370", "Литва"),      ("355", "Албания"),     ("356", "Мальта"),
    ("852", "Гонконг"),    ("886", "Тайвань"),
    ("86", "Китай"),       ("81", "Япония"),       ("82", "Южная Корея"),
    ("84", "Вьетнам"),     ("91", "Индия"),        ("92", "Пакистан"),
    ("93", "Афганистан"),  ("94", "Шри-Ланка"),    ("98", "Иран"),
    ("90", "Турция"),      ("66", "Таиланд"),      ("62", "Индонезия"),
    ("60", "Малайзия"),    ("63", "Филиппины"),    ("65", "Сингапур"),
    ("976", "Монголия"),   ("20", "Египет"),       ("212", "Марокко"),
    ("213", "Алжир"),      ("216", "Тунис"),       ("234", "Нигерия"),
    ("27", "ЮАР"),         ("44", "Великобритания"), ("61", "Австралия"),
    ("64", "Новая Зеландия"), ("353", "Ирландия"),
    ("7", "Россия"),       ("1", "США / Канада"),
]

# Страны, чей номер телефона указывает на англоязычного продавца
ENGLISH_PHONE_COUNTRIES = {
    "США / Канада", "Великобритания", "Австралия", "Новая Зеландия",
    "ЮАР", "Ирландия", "Сингапур", "Филиппины", "Нигерия",
}

KEYWORD_COUNTRIES = [
    ("Иран", ["ایران", "ایرانیه", "تهران", "مشهد", "شیراز", "اصفهان", "تبریز",
              "فارسی", "پارسی", "persian", "persia", "farsi", "iran", "iranian",
              "tehran", "mashhad", "shiraz", "isfahan", "tabriz", "iran1",
              "irancell", "هاست", "پوشک"]),
    ("ОАЭ", ["الإمارات", "الامارات", "امارات", "إمارات", "دبي", "دبى", "ابوظبي",
             "أبوظبي", "ابو ظبي", "الشارقة", "شارقة", "عجمان", "أجمان", "فجيرة",
             "رأس الخ", "العين", "dubai", "dubayy", "uae", "emirates", "sharjah",
             "ajman", "fujairah", "al ain", "abudhabi", "abu dhabi", "ras al khaimah"]),
    ("Китай", ["中国", "中文", "中文说", "普通话", "汉字", "漢字", "china", "chinese",
               "mandarin", "北京", "beijing", "上海", "shanghai", "深圳", "shenzhen",
               "广州", "guangzhou", "成都", "chengdu", "武汉", "wuhan", "杭州",
               "hangzhou", "香港", "hong kong", "台湾", "taiwan"]),
    ("Россия", ["россия", "росси", "русский", "русских", "русскоя", "москва",
                "moscow", "питер", "петербург", "st petersburg",
                "saint petersburg", "санкт-петербург", "казань",
                "kazan", "новосибирск", "novosibirsk",
                "екатеринбург", "yekaterinburg", "нижний", "nizhny", "уфа", "ufa",
                "краснодар", "krasnodar", "самара", "samara", "ростов", "rostov",
                "russian", "russia", "рус"]),
]

# Суффиксы/префиксы username -> страна (по самому длинному совпадению).
# Проверяются последние 3-4 символа никнейма: user_ru, user.ru, ru_user и т.д.
_USERNAME_COUNTRY_MAP = [
    # --- СНГ ---
    ("ru", "Россия"), ("ua", "Украина"), ("kz", "Казахстан"),
    ("by", "Беларусь"), ("uz", "Узбекистан"), ("kg", "Кыргызстан"),
    ("tm", "Туркменистан"), ("tj", "Таджикистан"), ("az", "Азербайджан"),
    ("am", "Армения"), ("ge", "Грузия"), ("md", "Молдова"),
    # --- Ближний Восток ---
    ("ir", "Иран"), ("tr", "Турция"), ("ae", "ОАЭ"), ("sa", "Сауд. Аравия"),
    ("iq", "Ирак"), ("eg", "Египет"), ("il", "Израиль"),
    # --- Азия ---
    ("cn", "Китай"), ("kr", "Южная Корея"), ("jp", "Япония"),
    ("in", "Индия"), ("pk", "Пакистан"), ("id", "Индонезия"),
    ("th", "Таиланд"), ("vn", "Вьетнам"), ("my", "Малайзия"),
    ("ph", "Филиппины"), ("sg", "Сингапур"),
    # --- Европа ---
    ("uk", "Великобритания"), ("de", "Германия"), ("fr", "Франция"),
    ("es", "Испания"), ("it", "Италия"), ("pl", "Польша"),
    ("nl", "Нидерланды"), ("pt", "Португалия"),
    # --- Америка ---
    ("us", "США"), ("ca", "Канада"), ("br", "Бразилия"),
    # --- Африка / Океания ---
    ("ng", "Нигерия"), ("za", "ЮАР"), ("au", "Австралия"),
]


def _detect_username_country(username):
    """Определяет страну по суффиксу/префиксу username (user_xx / xx_user / user.xx)."""
    if not username:
        return None
    u = username.lower()
    # Извлекаем潜在ные 2-буквенные коды из разделителей: _ . 
    parts = re.split(r"[._\-]+", u)
    # Проверяем каждую часть (кроме основного имени) — ищем коды длины 2-3
    for p in parts:
        if len(p) not in (2, 3):
            continue
        for code, country in _USERNAME_COUNTRY_MAP:
            if p == code:
                return country
    return None


def phone_country(phone):
    """Страна по телефонному номеру (по самому длинному префиксу) или None."""
    if not phone:
        return None
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if not digits:
        return None
    for pref, country in PHONE_COUNTRIES:
        if digits.startswith(pref):
            return country
    return None


def detect_country(name, bio="", phone=None, username=None):
    """Определяет страну: телефон -> ник -> флаги -> ключевые слова -> письменность."""
    text = (name or "") + " " + (bio or "")

    pc = phone_country(phone)
    if pc:
        return pc

    uc = _detect_username_country(username)
    if uc:
        return uc

    for flag, country in FLAG_COUNTRIES.items():
        if flag in text:
            return country

    low = text.lower()
    for country, words in KEYWORD_COUNTRIES:
        if any(w.lower() in low for w in words):
            return country

    for part in (name or "", bio or ""):
        if not part:
            continue
        has_persian = any(ch in PERSIAN_EXTRA or ch in PERSIAN_DIGITS for ch in part)
        has_cyrillic = any(any(s <= ord(ch) <= e for s, e in CYRILLIC_RANGES) for ch in part)
        has_han = any(any(s <= ord(ch) <= e for s, e in HAN_RANGES) for ch in part)
        has_arabic = any(any(s <= ord(ch) <= e for s, e in ARABIC_RANGES) for ch in part)
        if has_persian:
            return "Иран"
        if has_cyrillic:
            return "Россия"
        if has_han:
            return "Китай"
        if has_arabic:
            return "ОАЭ"
    return "Не определено"


def username_info(user):
    """Извлекает ОДИН основной публичный username. Только если ровно 1."""
    us = getattr(user, "usernames", None)
    if us is not None and len(us):
        # Ровно 1 username и он editable (базовый) — ок
        if len(us) == 1:
            un = getattr(us[0], "username", None)
            ed = getattr(us[0], "editable", None)
            if un and (ed is True or ed is None):
                return (True, un)
        # Больше 1 username (коллекционные) — пропускаем
        return (False, None)
    # Нет вектора usernames — проверяем старое поле username
    uname = getattr(user, "username", None)
    return (bool(uname), uname)


def user_name(u):
    return " ".join(x for x in (
        getattr(u, "first_name", "") or "",
        getattr(u, "last_name", "") or "") if x)


def status_text(user):
    st = getattr(user, "status", None)
    name = type(st).__name__ if st else ""
    return {
        "UserStatusOnline": "В сети",
        "UserStatusRecently": "Недавно",
        "UserStatusLastWeek": "На этой неделе",
        "UserStatusLastMonth": "В этом месяце",
        "UserStatusEmpty": "Нет данных",
        "UserStatusOffline": "Не в сети",
    }.get(name, "Не в сети")


def _is_blocked_country(country):
    """Китай/Иран/арабские страны → ТОЛЬКО Different Countries."""
    return country in {
        "Китай", "Иран", "ОАЭ", "Сауд. Аравия", "Бахрейн", "Катар",
        "Кувейт", "Оман", "Ирак", "Сирия", "Иордания", "Ливан", "Йемен",
        "Египет", "Марокко", "Алжир", "Тунис", "Нигерия", "Бангладеш",
        "Пакистан", "Афганистан", "Шри-Ланка",
    }


def online_label(seller):
    """«Статус» в сообщении: Онлайн / Недавно / Не в сети."""
    o = seller.get("online")
    if o == "В сети":
        return "Онлайн"
    if o == "Недавно":
        return "Недавно"
    return "Не в сети"


# ------------------------- цена и модель -------------------------
def _fmt_num(val):
    try:
        if val != val or val == float('inf') or val == float('-inf'):
            return "—"
        if val == int(val):
            return str(int(val))
        return f"{val:.2f}"
    except (TypeError, ValueError):
        return "—"


def price_parts(g):
    stars, ton = None, None
    for a in (getattr(g, "resell_amount", None) or []):
        nanos = getattr(a, "nanos", 0) or 0
        if isinstance(a, types.StarsTonAmount):
            gram = getattr(a, "amount", 0) / 1_000_000_000
            ton = _fmt_num(gram)
        else:
            val = getattr(a, "amount", 0)
            if nanos:
                val += nanos / 1e9
            stars = _fmt_num(val)
    return stars or "—", ton or "—"


def ton_price(g):
    """Цена ресейла в TON (float) или None, если лот не в TON."""
    for a in (getattr(g, "resell_amount", None) or []):
        if isinstance(a, types.StarsTonAmount):
            return getattr(a, "amount", 0) / 1_000_000_000
    return None


def model_name(g):
    for a in (getattr(g, "attributes", None) or []):
        if isinstance(a, types.StarGiftAttributeModel):
            return getattr(a, "name", None)
    return None


def _lum(rgb):
    r = (rgb >> 16) & 0xFF
    g = (rgb >> 8) & 0xFF
    b = rgb & 0xFF
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def is_black_backdrop(g):
    """Только строго «Black» (не Onyx Black): имя ровно "Black" И цвета реально тёмные."""
    for a in (getattr(g, "attributes", None) or []):
        if not isinstance(a, types.StarGiftAttributeBackdrop):
            continue
        name = (getattr(a, "name", "") or "").strip().lower()
        if name != "black":
            return False
        c = _lum(getattr(a, "center_color", 0))
        e = _lum(getattr(a, "edge_color", 0))
        return c < 0.6 and e < 0.6
    return False


# ------------------------- англоязычность -------------------------
def _has_any_script(text, ranges):
    return any(any(s <= ord(ch) <= e for s, e in ranges) for ch in text)


def is_english_speaker(seller):
    """Англоязычный продавец?

    Правило (выбрано пользователем):
    1. Если номер открыт и страна по нему — англоязычная (+1/+44/+61/...) -> да.
    2. Если номер открыт и страна известна, но НЕ англоязычная -> нет.
    3. Если номера нет/не определён — по имени и описанию:
       нет кириллицы/арабицы/хань/персидского, нет маркеров RU/IR/CN/AE,
       есть латиница -> да.
    """
    phone = seller.get("phone")
    if phone:
        c = phone_country(phone)
        if c is not None:
            return c in ENGLISH_PHONE_COUNTRIES

    name = seller.get("name") or ""
    about = seller.get("about") or ""
    text = name + " " + about
    if not text.strip():
        return False
    if _is_blocked_country(detect_country(name, about, None)):
        return False
    if _has_any_script(text, CYRILLIC_RANGES):
        return False
    if _has_any_script(text, ARABIC_RANGES):
        return False
    if _has_any_script(text, HAN_RANGES):
        return False
    if any(ch in PERSIAN_EXTRA or ch in PERSIAN_DIGITS for ch in text):
        return False
    if re.search(r"[A-Za-z]", text):
        return True
    return False


def _is_russian_seller(seller):
    """Русскоязычный продавец? Кириллица в имени/описании ИЛИ страна Россия."""
    country = seller.get("country") or ""
    if country == "Россия":
        return True
    name = seller.get("name") or ""
    about = seller.get("about") or ""
    username = seller.get("username") or ""
    text = name + " " + about + " " + username
    if not text.strip():
        return False
    if _has_any_script(text, CYRILLIC_RANGES):
        return True
    return False


# ------------------------- общие фильтры -------------------------
def _rating_ok(seller):
    r = seller.get("rating")
    return r is None or r <= MAX_RATING


def _nft_ok(seller):
    c = seller.get("nft_gifts_count")
    # None = не удалось получить — пропускаем (не блокируем)
    return c is None or c <= MAX_NFT_GIFTS


def _free_messages_ok(seller):
    """Только бесплатные сообщения."""
    return seller.get("free_messages") is not False


_SPAM_USERNAME = ("+rep", " rew ", " rev ", "holder")


def _spam_username(seller):
    """Спам-юзернеймы: +Rep, rew, rev, holder."""
    u = (seller.get("username") or "").lower()
    if not u:
        return False
    return any(s in u for s in _SPAM_USERNAME)


def _general_ok(seller):
    """Общий фильтр для всех категорий."""
    return _free_messages_ok(seller) and _rating_ok(seller) and _nft_ok(seller)


def _is_female_name(seller):
    """Проверяет, является ли имя или юзернейм продавца женским."""
    name = seller.get("name") or ""
    username = seller.get("username") or ""
    combined = name + " " + username
    if not combined.strip():
        return False
    # Убираем всё кроме букв, цифр, дефиса и подчёркивания
    cleaned = re.sub(r"[^\w\-]", " ", combined.lower())
    parts = cleaned.split()
    for part in parts:
        part = part.strip("-_")
        if not part:
            continue
        if part in FEMALE_NAMES:
            return True
    return False


def stars_price(g):
    """Цена в Stars (float) или None."""
    for a in (getattr(g, "resell_amount", None) or []):
        if isinstance(a, types.StarsTonAmount):
            continue
        val = getattr(a, "amount", 0)
        nanos = getattr(a, "nanos", 0) or 0
        if nanos:
            val += nanos / 1e9
        return float(val)
    return None


def candidate_feeds(gift, seller, is_new):
    """Новые 6 категорий. Отправляются ТОЛЬКО свежие листинги после warmup.

    Общие фильтры уже проверены в is_eligible (free, ≤5 lvl, ≤10 nft),
    но повторно проверяем _general_ok на всякий случай.

    Логика языка (обновлено):
      - «Разные страны» — все НЕ русские (другие страны).
      - Остальные 5 категории — ТОЛЬКО русские, Россия не попадает в Разные страны.
      - Неопределенно удалено из кода — считается как не-русский -> Разные страны.

    Логика содержимого:
      - Черный фон — только is_black_backdrop
      - Девочки — только _is_female_name
      - Дешёвые — title в CHEAP_TITLES (список выше)
      - Дорогие — title в DEAR_TITLES (список выше)
      - NFT от 10 ton — title в NFT10_TITLES (список выше)
      - Разные страны — любой подарок от не-русского продавца
    """
    if not is_new:
        return []
    if not _general_ok(seller):
        return []

    is_ru = _is_russian_seller(seller)  # включает country==Россия и кириллицу

    is_black = is_black_backdrop(gift)
    is_girl = _is_female_name(seller)
    title = (getattr(gift, "title", "") or "").strip().lower()
    is_cheap = title in CHEAP_TITLES
    is_dear = title in DEAR_TITLES
    is_nft10 = title in NFT10_TITLES

    # ── Разные страны: все кроме русских ──
    if not is_ru:
        return ["country"]

    # ── Остальные 5: ТОЛЬКО русские (Россия не идет в Разные страны) ──
    feeds = []

    if is_black:
        feeds.append("black")
    if is_cheap:
        feeds.append("cheap")
    if is_dear:
        feeds.append("dear")
    if is_nft10:
        feeds.append("nft10")
    if is_girl:
        feeds.append("girls")

    # Убираем дубликаты, сохраняем порядок
    seen = set()
    uniq = []
    for f in feeds:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def is_eligible(seller):
    """Общий фильтр: ТОЛЬКО с Open Profile + бесплатные сообщения, ≤10 NFT, ≤5 lvl."""
    # Только с кнопкой Open Profile (есть username)
    if not seller.get("username"):
        return "нет username"
    if _spam_username(seller):
        return "спам username"
    if seller.get("usernames_count") != 1:
        return "username != 1"
    # +888 — анонимные номера Telegram, спам/скам
    phone = seller.get("phone") or ""
    if phone.startswith("+888") or phone.startswith("888"):
        return "телефон +888"
    if seller.get("free_messages") is False:
        return "платные сообщения"
    r = seller.get("rating")
    if r is not None and r > MAX_RATING:
        return f"рейтинг {r} > {MAX_RATING}"
    c = seller.get("nft_gifts_count")
    if c is not None and c > MAX_NFT_GIFTS:
        return f"nft {c} > {MAX_NFT_GIFTS}"
    return None


# ------------------------- Бот API -------------------------
_BOT_SESSION = requests.Session()      # только для getUpdates (polling)
_API_SESSION = requests.Session()      # для исходящих вызовов (answer, edit, send)
if HTTP_PROXIES:
    _BOT_SESSION.proxies.update(HTTP_PROXIES)
    _API_SESSION.proxies.update(HTTP_PROXIES)

_PROXY_CACHE = {"t": 0.0, "px": None}


def _bot_proxies(force=False):
    """SOCKS5 для Bot API: первый живой из PROXY_LIST, иначе HTTP_PROXIES."""
    if not PROXY_ENABLED:
        return None
    now = time.time()
    if not force and _PROXY_CACHE["px"] is not None and now - _PROXY_CACHE["t"] < 60:
        return _PROXY_CACHE["px"]
    best = HTTP_PROXIES
    try:
        with _PROXY_LOCK:
            for p in PROXY_LIST:
                if _proxy_alive(p):
                    url = f"socks5h://{p[0]}:{p[1]}"
                    best = {"http": url, "https": url}
                    break
    except Exception:
        pass
    _PROXY_CACHE["t"] = now
    _PROXY_CACHE["px"] = best
    return best


def _refresh_bot_session(force=False):
    try:
        px = _bot_proxies(force=force)
        if px:
            _BOT_SESSION.proxies.update(px)
            _API_SESSION.proxies.update(px)
    except Exception:
        pass


def _http_post(url, payload, tries=4, wait=1.0, cap_wait=None):
    last = None
    for i in range(tries):
        try:
            r = _API_SESSION.post(url, json=payload, timeout=20).json()
        except Exception as e:
            last = e
            _refresh_bot_session(force=True)
            time.sleep(wait)
            continue
        if r.get("ok"):
            return r
        last = r
        err = str(r)
        if "Too Many Requests" in err:
            try:
                wait = int((r.get("parameters") or {}).get("retry_after", 5))
            except Exception:
                wait = 5
            wait = min(max(wait, 1), 30)
            if cap_wait is not None:
                wait = min(wait, cap_wait)
            print(f"[429] ждём {wait}с ({i+2}/{tries})", flush=True)
            time.sleep(wait)
            continue
        break
    raise RuntimeError(f"бот API: {last}")


# ----------------------------------------------------------------------
# Очередь правок форума. Telegram даёт ~1 editMessageText/с в диалоге:
# делаем правки через очередь с мягким темпом (~1/с при всплесках и без
# паузы когда очередь пуста), дубли одной карточки схлопываем -> вместо
# ожидания «ждём 30с» на flood правки применяются плавно.
_EDIT_Q = queue.Queue()
_EDIT_BUSY = set()
_EDIT_MUT = threading.Lock()


def _edit_schedule(chat_id, msg_id, key, title="Лот"):
    with _EDIT_MUT:
        if (chat_id, msg_id) in _EDIT_BUSY:
            return
        _EDIT_BUSY.add((chat_id, msg_id))
    _EDIT_Q.put((chat_id, msg_id, key, title))


def _edit_reserved_text(title):
    t = html.escape(title or "Лот")
    return "\n".join([
        f'<tg-emoji emoji-id="5893406892092297627">♥️</tg-emoji>"{t}"',
        '<tg-emoji emoji-id="5895770017458294953">💫</tg-emoji>Модель: hidden',
        '<tg-emoji emoji-id="5893473283696759404">💰</tg-emoji> Цена: hidden',
        '<tg-emoji emoji-id="5895444149699612825">📊</tg-emoji> LVL: hidden',
        '<tg-emoji emoji-id="5893376775781617954">🏆</tg-emoji> Количество: hidden',
        '<tg-emoji emoji-id="5893034681636491040">📱</tg-emoji> Premium: hidden',
        '<tg-emoji emoji-id="5902335789798265487">👤</tg-emoji> Страна: hidden',
        '<tg-emoji emoji-id="5893102202817352158">🕞</tg-emoji> Статус: hidden',
    ])


def _edit_worker():
    while True:
        try:
            chat_id, msg_id, key, title = _EDIT_Q.get(timeout=1)
        except queue.Empty:
            continue
        try:
            r = _http_post(f"{BOT_API}/editMessageText", {
                "chat_id": chat_id, "message_id": msg_id,
                "text": _edit_reserved_text(title),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": [[
                    {"text": "Забронировано", "style": "danger",
                     "callback_data": f"reserve:{key}",
                     "icon_custom_emoji_id": BTN_RESERVE_ICON},
                ]]},
            }, tries=3)
            log(f"[reserve][edit] {key} ok={(r or {}).get('ok')}")
        except Exception as e:
            log(f"[reserve][edit] {key} ОШИБКА: {e}")
        finally:
            with _EDIT_MUT:
                _EDIT_BUSY.discard((chat_id, msg_id))
            _EDIT_Q.task_done()


def invalidate_topics():
    global _STATE
    try:
        with _STATE_LOCK:
            st = _STATE
            st["topics_ok"] = False
            st["topics_t"] = 0
        save_state(st)
        print("[topics] кэш тем сброшен (thread not found) — перечитываю темы", flush=True)
    except Exception as e:
        print(f"[topics] не удалось сбросить кэш: {e}", flush=True)


def bot_send(chat_id, text, buttons=None, thread_id=None, claim_key=None):
    text = text.strip()
    while text:
        part, text = text[:3800], text[3800:]
        payload = {
            "chat_id": chat_id, "text": part,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if thread_id:
            payload["message_thread_id"] = thread_id
        if buttons or claim_key:
            rows = [[{"text": t, "url": u, "icon_custom_emoji_id": ic}]
                    for t, u, ic in (buttons or [])]
            if claim_key:
                # Зелёная кнопка «Забронировать» — под кнопками Open Profile/View Gift
                rows.append([{
                    "text": "Забронировать",
                    "callback_data": f"reserve:{claim_key}",
                    "style": "success",
                    "icon_custom_emoji_id": BTN_RESERVE_ICON,
                }])
            payload["reply_markup"] = {"inline_keyboard": rows}
        try:
            r = _http_post(f"{BOT_API}/sendMessage", payload)
        except RuntimeError as e:
            if "thread not found" in str(e):
                invalidate_topics()
            raise RuntimeError(f"sendMessage: {e}")


def _title_from_card(text):
    """Достаёт название подарка из карточки лота («"..."` в первой строке)."""
    m = re.search(r'"(.+?)"', text or "", re.S)
    return (m.group(1).strip()[:60] or "Аноним") if m else "Аноним"


def pm_markup(buttons):
    """Клавиатура для ЛС (после брони): ссылки продавца/подарка.
    Только для ЛС — в группу не уходит."""
    rows = [[{"text": t, "url": u, "icon_custom_emoji_id": ic}]
            for t, u, ic in (buttons or [])]
    return {"inline_keyboard": rows}


def _load_claims():
    """Брони с диска (claims.json) или {}."""
    try:
        if os.path.exists(CLAIMS_FILE):
            with open(CLAIMS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        print(f"[claims] чтение: {e}", flush=True)
    return {}


_CLAIMS_LOCK = threading.Lock()


def _persist_claims(claims):
    """Атомарная запись броней в claims.json."""
    try:
        with _CLAIMS_LOCK:
            tmp = CLAIMS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(claims, f, ensure_ascii=False)
            os.replace(tmp, CLAIMS_FILE)
    except Exception as e:
        print(f"[claims] запись: {e}", flush=True)


# ------------------------- состояние -------------------------
def load_state():
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        backup = DATA_FILE + f".broken.{int(time.time())}"
        try:
            os.replace(DATA_FILE, backup)
            print(f"[state] повреждён — сохранён как {backup}, стартую чисто",
                  flush=True)
        except Exception:
            print("[state] повреждён — стартую чисто", flush=True)
    return {}


_BG_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="bg")


_SAVE_LOCK = threading.Lock()
_SAVE_ST = None
_SAVE_DIRTY = False
_SAVE_WAKE = threading.Event()


def save_state(st):
    global _SAVE_ST, _SAVE_DIRTY
    with _SAVE_LOCK:
        _SAVE_ST = st
        _SAVE_DIRTY = True
    _SAVE_WAKE.set()


def _saver_loop():
    global _SAVE_DIRTY, _SAVE_ST
    while not _STOP_EVT.is_set():
        _SAVE_WAKE.wait(3)
        _SAVE_WAKE.clear()
        with _SAVE_LOCK:
            if not _SAVE_DIRTY:
                continue
            _SAVE_DIRTY = False
            st = _SAVE_ST
        if st is None:
            continue
        try:
            tmp = DATA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False)
            os.replace(tmp, DATA_FILE)
        except BaseException:
            with _SAVE_LOCK:
                _SAVE_DIRTY = True
            continue


def flush_state():
    with _SAVE_LOCK:
        st = _SAVE_ST
        _SAVE_DIRTY = False
    if st is None:
        return
    try:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except BaseException:
        pass


def write_crash(tb):
    try:
        with open(os.path.join(HERE, "crash.log"), "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            f.write(tb)
    except Exception:
        pass


# ------------------------- MTProto-операции -------------------------
MAX_PAGES = 4                 # максимум страниц на тип при глубоком скане
MAX_SEND_PER_CYCLE = 800      # лимит отправок за цикл
SEND_PAUSE = 0.03             # пауза между отправками, сек
SEND_RPS = 2.0                # не больше сообщений в секунду боту (Bot API)
SEND_BURST = 3                # пачка отправок
MAX_RESOLVE_PER_CYCLE = 300   # сколько продавцов досощать за цикл
                               # (каждый = отдельный GetFullUserRequest)


class SessionDead(Exception):
    """Telegram отозвал сессию аккаунта — повторами не лечится."""

    def __init__(self, msg, client=None):
        super().__init__(msg)
        self.client = client


class RateLimiter:
    """Token bucket: не больше RPS запросов в секунду на один аккаунт.

    Общий на все аккаунты не делаем намеренно: у каждого свой лимит,
    потому что рвут сессии именно по темпу на аккаунт.
    """

    def __init__(self, rps, burst):
        self.rps = max(0.1, float(rps))
        self.burst = max(1.0, float(burst))
        self._tokens = self.burst
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost=1.0):
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(self.burst,
                                   self._tokens + (now - self._last) * self.rps)
                self._last = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                need = (cost - self._tokens) / self.rps
            await asyncio.sleep(need)

    def penalize(self, seconds):
        """Telegram потребовал паузу — временно опустошаем бакет."""
        with_ = min(self.burst, max(0.0, seconds) * self.rps * 0.5)
        self._tokens = min(self._tokens, with_)


# Лимитер на каждый TelegramClient: ключ — сам объект клиента.
_RL_LOCK = threading.Lock()
_LIMITERS = {}


def limiter_for(client):
    # У обёртки лимитер уже свой — иначе fetch_listings посчитал бы
    # один и тот же запрос дважды и урезал скорость вдвое.
    rl = getattr(client, "_rl", None)
    if rl is not None:
        return rl
    with _RL_LOCK:
        rl = _LIMITERS.get(id(client))
        if rl is None:
            rl = RateLimiter(RPS_PER_ACCOUNT, BURST_PER_ACCOUNT)
            _LIMITERS[id(client)] = rl
        return rl


class ThrottledClient:
    """Обёртка клиента: ЛЮБОЙ его запрос проходит через лимитер аккаунта.

    Без неё лимитер покрывал только fetch_listings, а resolve_seller
    (GetFullUserRequest на каждого продавца) шёл мимо — это десятки
    тысяч запросов за цикл, что и рвало сессии.
    """

    def __init__(self, client):
        self._c = client
        self._rl = limiter_for(client)

    def __getattr__(self, name):
        # is_connected/disconnect/get_me и прочее — к исходному клиенту
        return getattr(self._c, name)

    async def __call__(self, request, *args, _retry=True, **kwargs):
        await self._rl.acquire()
        try:
            return await self._c(request, *args, **kwargs)
        except SESSION_DEAD_ERRORS as e:
            raise SessionDead(str(e), self) from e
        except tl_errors.FloodWaitError as e:
            wait = int(getattr(e, "seconds", 0) or 0)
            if not _retry or wait > MAX_FLOOD_WAIT:
                raise
            print(f"[flood] {type(request).__name__}: Telegram просит ждать "
                  f"{wait}с — жду", flush=True)
            self._rl.penalize(wait)
            await asyncio.sleep(wait + 1)
            return await self(request, *args, _retry=False, **kwargs)


_SEND_RL = None


def send_limiter():
    """Ограничитель отправок Bot API (у Telegram свой лимит на чат)."""
    global _SEND_RL
    if _SEND_RL is None:
        _SEND_RL = RateLimiter(SEND_RPS, SEND_BURST)
    return _SEND_RL


async def get_gift_catalog(client):
    res = await asyncio.wait_for(
        client(functions.payments.GetStarGiftsRequest(hash=0)), timeout=60)
    return [g.id for g in (getattr(res, "gifts", None) or [])
            if (getattr(g, "availability_resale", 0) or 0) > 0]


async def fetch_listings(client, gift_id, deep=False):
    """Страницы лотов одного базового подарка: (key, gift, users_by_id).

    Без sort_by_price/sort_by_num результаты идут по времени последнего
    изменения цены — новые/изменённые сверху (первая страница = свежие).

    Каждый запрос проходит через лимитер аккаунта (ThrottledClient),
    включая повтор после FLOOD_WAIT. Раньше лимитер покрывал только этот
    вызов, а остальные пути шли мимо.
    """
    rows = []
    seen = set()
    offset = ""
    pages = MAX_PAGES if deep else 1
    try:
        for _ in range(pages):
            res = None
            for attempt in range(3):
                try:
                    if not client.is_connected():
                        try:
                            await client.connect()
                        except Exception:
                            pass
                    res = await asyncio.wait_for(client(
                        functions.payments.GetResaleStarGiftsRequest(
                            gift_id=gift_id, offset=offset, limit=PAGE_LIMIT,
                            attributes_hash=0)),
                        timeout=45)
                    break
                except SessionDead:
                    raise
                except tl_errors.FloodWaitError as e:
                    # Паузу уже выдержала обёртка; если дошли сюда —
                    # Telegram попросил слишком много, отдаём тип дальше
                    wait = int(getattr(e, "seconds", 0) or 0)
                    print(f"[flood] gift={gift_id}: жду {wait}с", flush=True)
                    return None
                except Exception:
                    if attempt < 2:
                        await asyncio.sleep(2 + attempt)
                        # попробуем переподключиться перед повтором
                        try:
                            if not client.is_connected():
                                await client.connect()
                        except Exception:
                            pass
                    else:
                        raise
            if res is None:
                # все попытки съедены FLOOD_WAIT — тип НЕ пустой,
                # вернуть None, иначе он уйдёт в skip и пропадёт на SKIP_TTL
                return None
            gifts = getattr(res, "gifts", None) or []
            if not gifts:
                break
            users_by_id = {u.id: u for u in (getattr(res, "users", None) or [])}
            for g in gifts:
                if not isinstance(g, types.StarGiftUnique):
                    continue
                key = f"{g.gift_id}:{g.num}"
                if key in seen:
                    continue
                seen.add(key)
                rows.append((key, g, users_by_id))
            nxt = getattr(res, "next_offset", None)
            if not nxt or len(gifts) < PAGE_LIMIT:
                break
            offset = nxt
    except SessionDead:
        raise
    except Exception as e:
        print(f"[getResaleStarGifts gift={gift_id}] "
              f"{type(e).__name__}: {e}", flush=True)
        return None
    return rows


async def fetch_nft_gift_count(client, user):
    """Количество отображаемых NFT-подарков в профиле через payments.getSavedStarGifts."""
    try:
        peer = types.InputPeerUser(user_id=user.id,
                                   access_hash=getattr(user, "access_hash", None) or 0)
        res = await asyncio.wait_for(
            client(functions.payments.GetSavedStarGiftsRequest(
                peer=peer, offset="", limit=1,
                exclude_unsaved=True,
                exclude_unlimited=True,
            )), timeout=15)
        return getattr(res, "count", None)
    except Exception as e:
        print(f"[nft_count uid={user.id}] {type(e).__name__}: {e}", flush=True)
        return None


async def get_seller_cache(client, state, user):
    with _STATE_LOCK:
        cache = state.setdefault("user_cache", {})
    uid = user.id
    now = time.time()
    with _CYCLE_LOCK:
        if uid in _CYCLE_CACHE and _CYCLE_CACHE[uid] is not _CYCLE_MISS:
            return _CYCLE_CACHE[uid]
    if uid in cache and now - cache[uid]["t"] < SELLER_CACHE_TTL:
        return cache[uid]

    with _CYCLE_LOCK:
        lock = _CYCLE_LOCKS.get(uid)
        if lock is None:
            lock = asyncio.Lock()
            _CYCLE_LOCKS[uid] = lock
    if lock.locked():
        async with lock:
            with _CYCLE_LOCK:
                ent = _CYCLE_CACHE.get(uid)
            if ent is not None and ent is not _CYCLE_MISS:
                return ent
    else:
        await lock.acquire()
    try:
        ok_username, uname = username_info(user)
        us_list = getattr(user, "usernames", None) or []
        entry = {
            "t": now,
            "user_id": user.id,
            "access_hash": getattr(user, "access_hash", None) or 0,
            "verified": False,
            "username": uname,
            "usernames_count": len(us_list) if us_list else (1 if uname else 0),
            "name": user_name(user),
            "about": "",
            "premium": bool(getattr(user, "premium", False)),
            "online": "—",
            "country": detect_country(user_name(user), username=uname),
            "phone": getattr(user, "phone", None),
            "free_messages": None,
            "paid_message_stars": None,
            "rating": None,
            "stars": None,
            "next": None,
            "gifts_count": None,
            "nft_gifts_count": None,
        }
        try:
            full = await asyncio.wait_for(client(GetFullUserRequest(types.InputUser(
                user_id=user.id, access_hash=user.access_hash or 0))), timeout=15)
            for fu in (getattr(full, "users", None) or []):
                if getattr(fu, "id", None) == user.id:
                    ok_u, fresh_uname = username_info(fu)
                    fu_us = getattr(fu, "usernames", None) or []
                    entry["username"] = fresh_uname
                    entry["usernames_count"] = len(fu_us) if fu_us else (1 if fresh_uname else 0)
                    entry["phone"] = getattr(fu, "phone", None) or entry["phone"]
                    entry["online"] = status_text(fu)
                    entry["online_expires"] = getattr(
                        getattr(fu, "status", None), "expires", None)
                    break
            uf = getattr(full, "full_user", None)
            if uf is not None:
                entry["verified"] = True
                r = getattr(uf, "stars_rating", None)
                if r is not None:
                    entry.update({
                        "rating": getattr(r, "level", None),
                        "stars": getattr(r, "stars", None),
                        "next": getattr(r, "next_level_stars", None),
                    })
                entry["gifts_count"] = getattr(uf, "stargifts_count", None)
                paid = getattr(uf, "send_paid_messages_stars", None)
                entry["paid_message_stars"] = paid
                entry["free_messages"] = not bool(paid)
                about = getattr(uf, "about", None) or ""
                entry["about"] = about
                entry["country"] = detect_country(
                    user_name(user), about, getattr(user, "phone", None),
                    username=entry.get("username"))
            else:
                entry["t"] = now - SELLER_CACHE_TTL
        except Exception as e:
            print(f"[full_user uid={uid}] {type(e).__name__}: {e}", flush=True)
            entry["t"] = now - SELLER_CACHE_TTL

        nft = await fetch_nft_gift_count(client, user)
        if nft is not None:
            entry["nft_gifts_count"] = nft

        with _STATE_LOCK:
            cache[uid] = entry
        with _CYCLE_LOCK:
            _CYCLE_CACHE[uid] = entry
        return entry
    finally:
        if lock.locked():
            lock.release()


async def resolve_seller(client, state, users_by_id, gift):
    peer = getattr(gift, "owner_id", None)
    user = None
    if isinstance(peer, types.PeerUser):
        user = users_by_id.get(peer.user_id)
        if user is None:
            user = next((u for u in users_by_id.values() if u.id == peer.user_id),
                        None)
    if user is not None:
        return await get_seller_cache(client, state, user)
    return {
        "t": time.time(), "user_id": None, "verified": False,
        "username": None,
        "usernames_count": 0,
        "name": getattr(gift, "owner_name", None) or "Аноним",
        "about": "",
        "premium": None, "online": "—", "country": "Аноним / TON",
        "phone": None, "free_messages": None, "paid_message_stars": None,
        "rating": None, "stars": None, "next": None, "gifts_count": None,
        "nft_gifts_count": None,
    }


# ------------------------- дизайн сообщения -------------------------
def format_compact(key, gift, seller):
    title = html.escape(getattr(gift, "title", "") or "Аноним")
    stars, ton = price_parts(gift)
    price = f"{stars}{EMOJI['stars']}/ {ton}{EMOJI['gram']}"
    model = html.escape(model_name(gift) or "—")
    level = seller.get("rating")
    lvl = str(level) if level is not None else "—"
    nft_count = seller.get("nft_gifts_count")
    qty = str(nft_count) if nft_count is not None else "—"
    prem = EMOJI["prem_yes"] if seller.get("premium") else EMOJI["prem_no"]
    country = seller.get("country") or "—"
    return "\n".join([
        f'{EMOJI["new"]}"{title}"',
        "",
        f'{EMOJI["model"]}Модель: {model}',
        f'{EMOJI["price"]} Цена: <b>{stars}{EMOJI["stars"]}</b>/ {ton}{EMOJI["gram"]}',
        f'{EMOJI["lvl"]} LVL: <b>{lvl}</b>',
        f'{EMOJI["quantity"]} Количество: <b>{qty}</b>',
        f'{EMOJI["premium"]} Premium: {prem}',
        f'{EMOJI["country"]} Страна: {country}',
        f'{EMOJI["status"]} Статус: <b>{online_label(seller)}</b>',
    ])


def build_buttons(gift, seller):
    """Кнопки с премиум-эмодзи: (text, url, icon_custom_emoji_id)."""
    buttons = []
    if seller.get("username"):
        buttons.append(("Open Profile",
                        f"https://t.me/{seller['username']}",
                        BTN_PROFILE_ICON))
    slug = getattr(gift, "slug", None)
    if slug:
        buttons.append(("View Gift",
                        f"https://t.me/nft/{slug}-{gift.num}",
                        BTN_GIFT_ICON))
    return buttons


def build_upgrade_buttons(gift, seller):
    """Кнопки для категории Upgrade: slug уже содержит Collection-Num."""
    buttons = []
    if seller.get("username"):
        buttons.append(("Open Profile",
                        f"https://t.me/{seller['username']}",
                        BTN_PROFILE_ICON))
    slug = getattr(gift, "slug", None)
    if slug:
        buttons.append(("View Gift",
                        f"https://t.me/nft/{slug}",
                        BTN_GIFT_ICON))
    return buttons


# ------------------------- upgrade-форматирование -------------------------
def _backdrop_name(gift):
    """Имя фона (backdrop) из атрибутов подарка."""
    for a in (getattr(gift, "attributes", None) or []):
        if isinstance(a, types.StarGiftAttributeBackdrop):
            return getattr(a, "name", None) or "—"
    return "—"


def _upgrade_model(gift):
    """Модель (model) из атрибутов подарка."""
    return model_name(gift) or "—"


def _upgrade_level(gift):
    """Уровень (num) улучшенного подарка."""
    return getattr(gift, "num", None) or "—"


def _upgrade_premium(seller):
    """Premium-статус владельца."""
    return EMOJI["prem_yes"] if seller.get("premium") else EMOJI["prem_no"]


def format_upgrade(key, gift, seller):
    """Формат сообщения для категории Upgrade (улучшенный подарок)."""
    title = html.escape(getattr(gift, "title", "") or "") or \
        html.escape(getattr(gift, "slug", "") or "Улучшенный подарок")
    bg = html.escape(_backdrop_name(gift))
    lvl = _upgrade_level(gift)
    prem = _upgrade_premium(seller)
    country = seller.get("country") or "—"
    user = seller.get("username")
    name = seller.get("name") or ""
    lines = [f'{EMOJI["upgrade_title"]}UPGRADE GIFT', ""]
    lines.append(f'{EMOJI["upgrade_fire"]}Название: {title}')
    if bg and bg != "—":
        lines.append(f'{EMOJI["upgrade_bg"]}Фон: {bg}')
    if lvl and lvl != "—":
        lines.append(f'{EMOJI["lvl"]} LVL: <b>{lvl}</b>')
    lines.append(f'{EMOJI["premium"]} Premium: {prem}')
    if user:
        lines.append(f'{EMOJI["country"]} @{html.escape(user)}')
    elif name:
        lines.append(f'{EMOJI["country"]} {html.escape(name)}')
    elif country and country != "Аноним / TON":
        lines.append(f'{EMOJI["country"]} Страна: {country}')
    lines += ["", f'{EMOJI["upgrade_flash"]}Подарок был только что улучшен.']
    return "\n".join(lines)


# ------------------------- upgrade-мониторинг -------------------------
async def _find_upgrade_max(client, slug, hint=0):
    """Находит максимальный существующий номер уникального подарка коллекции
    (сколько всего выпущено). Бинарный поиск — десятки запросов вместо тысяч.
    hint — известная нижняя граница (максимум с рынка), если есть.
    Возвращает максимальный существующий номер (0 — коллекция пуста)."""
    # 1) Нижняя граница (заведомо существующий номер)
    lo = max(0, hint)
    if lo > 0:
        g, err = await _fetch_single_gift(client, slug, lo)
        if g is None:
            lo = 0  # hint оказался недостоверным — ищем с нуля

    # 2) Верхняя граница удвоением, пока подарок существует
    hi = max(lo, 1)
    while True:
        g, err = await _fetch_single_gift(client, slug, hi)
        if g is None or err is not None:
            break
        lo = hi
        if hi > 5_000_000:
            break
        hi *= 2

    # 3) Бинарный поиск между lo (есть) и hi (нет)
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        g, err = await _fetch_single_gift(client, slug, mid)
        if g is not None:
            lo = mid
        else:
            hi = mid
    return lo


async def _fetch_single_gift(client, slug, num):
    """Получает один улучшенный подарок по slug-N.

    Возвращает (gift, None) если подарок существует,
    (None, None) если подарок ещё не выпущен (STARGIFT_SLUG_INVALID),
    иначе (None, e) — другая ошибка."""
    try:
        gift = await asyncio.wait_for(
            client(functions.payments.GetUniqueStarGiftRequest(
                slug=f"{slug}-{num}")),
            timeout=20)
        return gift, None
    except Exception as e:
        if "STARGIFT_SLUG_INVALID" in str(e):
            # Подарок с таким номером ещё не выпущен — это НЕ ошибка,
            # это сигнал «новых улучшений пока нет».
            return None, None
        print(f"[upgrade] {slug}-{num}: {type(e).__name__}: {e}", flush=True)
        return None, e


async def _upgrade_owner_seller(client, state, gift):
    """Получает данные владельца улучшенного подарка (аналог resolve_seller)."""
    peer = getattr(gift, "owner_id", None)
    if isinstance(peer, types.PeerUser):
        try:
            user = await asyncio.wait_for(
                client.get_entity(peer.user_id),
                timeout=15)
            return await get_seller_cache(client, state, user)
        except Exception as e:
            print(f"[upgrade][owner] {type(e).__name__}: {e}", flush=True)
            # Попробуем через GetFullUserRequest
            try:
                full = await asyncio.wait_for(
                    client(GetFullUserRequest(types.InputUser(
                        user_id=peer.user_id, access_hash=0))),
                    timeout=15)
                entry = {
                    "t": time.time(), "user_id": peer.user_id,
                    "access_hash": 0, "verified": False,
                    "username": None, "usernames_count": 0,
                    "name": getattr(gift, "owner_name", None) or "Аноним",
                    "about": "", "premium": None, "online": "—",
                    "country": "Не определено", "phone": None,
                    "free_messages": None, "paid_message_stars": None,
                    "rating": None, "stars": None, "next": None,
                    "gifts_count": None, "nft_gifts_count": None,
                }
                uf = getattr(full, "full_user", None)
                if uf is not None:
                    paid = getattr(uf, "send_paid_messages_stars", None)
                    entry["free_messages"] = not bool(paid)
                    r = getattr(uf, "stars_rating", None)
                    if r is not None:
                        entry["rating"] = getattr(r, "level", None)
                    about = getattr(uf, "about", None) or ""
                    entry["about"] = about
                    entry["country"] = detect_country(
                        entry["name"], about, username=entry.get("username"))
                for fu in (getattr(full, "users", None) or []):
                    if getattr(fu, "id", None) == peer.user_id:
                        entry["username"] = getattr(fu, "username", None)
                        entry["premium"] = bool(getattr(fu, "premium", False))
                        entry["online"] = status_text(fu)
                        break
                nft = None
                try:
                    # Попробуем получить nft count — может не работать
                    # если access_hash неизвестен
                    nft_user = types.User(
                        id=peer.user_id, access_hash=0, photo=None,
                        first_name="", last_name="", phone=None,
                        status=None, premium=False)
                    nft = await fetch_nft_gift_count(client, nft_user)
                except Exception:
                    pass
                if nft is not None:
                    entry["nft_gifts_count"] = nft
                return entry
            except Exception:
                pass
    # Fallback — анонимный продавец
    return {
        "t": time.time(), "user_id": None, "verified": False,
        "username": None, "usernames_count": 0,
        "name": getattr(gift, "owner_name", None) or "Аноним",
        "about": "", "premium": None, "online": "—",
        "country": "Аноним / TON", "phone": None,
        "free_messages": None, "paid_message_stars": None,
        "rating": None, "stars": None, "next": None,
        "gifts_count": None, "nft_gifts_count": None,
    }


async def poll_upgrades(clients, state):
    """Мониторинг только коллекций из UPGRADE_SLUGS (точный список заказчика).

    Логика:
      - Для каждой коллекции храним upgrade_counts[slug] = последний известный
        (максимальный) номер выпущенного уникального подарка.
      - Если база неизвестна — устанавливаем её один раз через бинарный поиск
        (находим точный максимум), НИЧЕГО не отправляя.
      - Затем каждый цикл пробуем только slug-(base+1). Если существует — это
        «только что улучшенный» (номер прибавился на 1): отправляем, base += 1,
        и пробуем следующий. Как только STARGIFT_SLUG_INVALID — новых нет, стоп.
    """
    if not clients:
        print("[upgrade] старт, но нет clients — выход", flush=True)
        return

    client = clients[0]
    print(f"[upgrade] старт, коллекций в списке: {len(UPGRADE_SLUGS)}", flush=True)
    last_nums = dict(state.get("upgrade_counts") or {})
    sent_keys = set(state.get("upgrade_sent") or [])
    valid_slugs = set(state.get("upgrade_valid_slugs") or [])
    topics = {}
    try:
        topics = {} if PM_MODE else await discover_topics(client, state)
    except Exception as e:
        print(f"[upgrade] discover_topics: {type(e).__name__}: {e}", flush=True)
    chat_id = FORUM_CHAT_ID if CHAT_ID_OVERRIDE is None else CHAT_ID_OVERRIDE

    if not chat_id:
        print("[upgrade] нет chat_id — выход", flush=True)
        return

    topic_id = None if PM_MODE else topics.get(_norm_topic(TOPIC_NAMES.get("upgrade", "")))
    if not PM_MODE:
        print(f"[upgrade] topic_id темы 'Upgrade' = {topic_id}", flush=True)
    if not PM_MODE and not topic_id:
        print("[upgrade] тема 'Upgrade' не найдена — карточки не отправятся "
              "в форум (только сохранятся как лоты)", flush=True)

    # Мониторим ТОЛЬКО список заказчика (не всё, что на рынке)
    all_slugs = set(UPGRADE_SLUGS)
    if not all_slugs:
        print("[upgrade] список UPGRADE_SLUGS пуст — выход", flush=True)
        return

    new_total = 0
    checked = 0
    # Рыночные максимумы (подсказка для стартовой базы новых коллекций)
    discover_hint = dict(state.get("upgrade_market_max") or {})

    for slug in sorted(all_slugs):
        if not clients:
            break
        client = clients[0]
        base = last_nums.get(slug)
        if base is None:
            # База никогда не ставилась — бинарным поиском находим точный
            # максимум (сколько всего выпущено). Ничего не отправляем.
            hint = discover_hint.get(slug, 0)
            base = await _find_upgrade_max(client, slug, hint)
            last_nums[slug] = base
            print(f"[upgrade] {slug}: база установлена, выпущено = {base}",
                  flush=True)
            continue  # в этом цикле только фиксируем базу

        # База известна (ранее подтверждена бинарным поиском).
        # Проверяем ТОЛЬКО следующий номер base+1 — это и есть
        # «только что улучшенный» подарок.
        nxt = base + 1
        scanned = 0
        while True:
            gift, err = await _fetch_single_gift(client, slug, nxt)
            if gift is not None:
                checked += 1
                key = f"upgrade:{slug}:{nxt}"
                if key not in sent_keys:
                    try:
                        seller = await _upgrade_owner_seller(client, state, gift)
                        ok = (_general_ok(seller) and seller.get("username")
                              and not _spam_username(seller)
                              and not ((seller.get("phone") or "").startswith("+888")
                                       or (seller.get("phone") or "").startswith("888")))
                        if ok:
                            msg = format_upgrade(key, gift, seller)
                            btns = build_upgrade_buttons(gift, seller)
                            with _STATE_LOCK:
                                state.setdefault("claims", {})[key] = {
                                    "msg": msg, "buttons": btns,
                                    "markup": pm_markup(btns), "t": time.time(),
                                    "title": getattr(gift, "title", "") or "Аноним",
                                    "owner_username": seller.get("username"),
                                    "owner_name": seller.get("name") or "Аноним",
                                    "owner_id": seller.get("user_id"),
                                    "rating": seller.get("rating"),
                                    "slug": slug, "num": nxt,
                                }
                                _persist_claims(state["claims"])
                            if topic_id:
                                try:
                                    await send_limiter().acquire()
                                    await asyncio.to_thread(
                                        bot_send, chat_id, msg, btns, topic_id, key)
                                    new_total += 1
                                    print(f"[upgrade] {slug}-{nxt} -> тема Upgrade",
                                          flush=True)
                                except Exception as e:
                                    print(f"[upgrade] {slug}-{nxt} отправка: {e}",
                                          flush=True)
                        else:
                            print(f"[upgrade] {slug}-{nxt}: отфильтрован", flush=True)
                    except Exception as e:
                        print(f"[upgrade] {slug}-{nxt} обработка: {e}", flush=True)
                    sent_keys.add(key)
                last_nums[slug] = nxt
                nxt += 1
                scanned += 1
                if scanned > 3:  # нормальный рост — по одному; 3 = страховка
                    break
            elif err is None:
                break  # STARGIFT_SLUG_INVALID — новых пока нет
            else:
                break  # другая ошибка

    # Ограничим sent_keys (храним последние 10k)
    if len(sent_keys) > 10000:
        sent_keys = set(list(sent_keys)[-8000:])

    with _STATE_LOCK:
        state["upgrade_counts"] = last_nums
        state["upgrade_sent"] = list(sent_keys)
        state["upgrade_valid_slugs"] = list(valid_slugs)
    save_state(state)

    if checked:
        print(f"[upgrade] проверено, новых улучшений отправлено: {new_total}",
              flush=True)


# ------------------------- бронирование -------------------------
def reserve_pm_text(entry):
    """Текст «💰 ВАШ ЛОТ» для ЛС после брони."""
    E = EMOJI_RESERVE
    title = html.escape(entry.get("title") or "Аноним")
    rating = entry.get("rating")
    if rating == 1:
        lvl = E["lvl1"]
    elif rating == 2:
        lvl = E["lvl2"]
    else:
        lvl = "—"
    return "\n".join([
        f'{E["money"]}ВАШ ЛОТ{E["money"]}',
        "",
        f'{E["diamond"]}Название: {title}',
        f'{E["talk"]}Уровень: {lvl}',
        "",
        f'{E["lock"]}Лот забронирован вами. Никто из бота больше не сможет ему написать.',
    ])


def reserve_pm_markup(entry):
    """Кнопки в ЛС: 👤 Open profile / 💎 View gift."""
    rows = []
    owner_uname = entry.get("owner_username")
    owner_id = entry.get("owner_id")
    if owner_uname:
        rows.append([{"text": "Open profile",
                      "url": f"https://t.me/{owner_uname}",
                      "icon_custom_emoji_id": BTN_PM_PROFILE_ICON}])
    elif owner_id:
        rows.append([{"text": "Open profile",
                      "url": f"tg://user?id={owner_id}",
                      "icon_custom_emoji_id": BTN_PM_PROFILE_ICON}])
    slug = entry.get("slug")
    num = entry.get("num")
    if slug:
        rows.append([{"text": "View gift",
                      "url": f"https://t.me/nft/{slug}-{num}"
                              if num else f"https://t.me/nft/{slug}",
                      "icon_custom_emoji_id": BTN_PM_GIFT_ICON}])
    return {"inline_keyboard": rows}


RESERVE_CD = 12.0
_LAST_RESERVE = {}
_LAST_RESERVE_MUT = threading.Lock()


def handle_callback(state, cq):
    """Обработка нажатия «🔒 Забронировать»."""
    cqid = cq.get("id")
    qdata = cq.get("data") or ""
    user = cq.get("from") or {}
    uid = user.get("id")
    uname = user.get("username") or user.get("first_name") or "?"
    msg_ = cq.get("message") or {}
    chat_id = (msg_.get("chat") or {}).get("id")
    msg_id = msg_.get("message_id")
    log(f"[cb] data={qdata!r} uid={uid} (@{uname}) chat={chat_id} msg={msg_id}")

    def _answer(**kw):
        payload = {"callback_query_id": cqid}
        payload.update(kw)
        try:
            r = _http_post(f"{BOT_API}/answerCallbackQuery", payload,
                           tries=2, cap_wait=3)
            log(f"[cb][answer] ok={(r or {}).get('ok')} {kw.get('text','')!r}")
            return (r or {}).get("ok")
        except Exception as e:
            log(f"[cb][answer] ОШИБКА: {e}")
            return False

    try:
        if not qdata.startswith("reserve:"):
            _answer()
            return
        key = qdata[len("reserve:"):]

        with _STATE_LOCK:
            claims = state.setdefault("claims", {})
            entry = claims.get(key)
            if entry is None:
                claims.update(_load_claims())
                entry = claims.get(key)
            if entry is None:
                card_text = (msg_.get("text") or "").strip()
                if not card_text:
                    log(f"[reserve] лот {key} не найден и текст сообщения пуст")
                    _answer(text="Лот не найден или его срок истёк",
                            show_alert=True)
                    return
                entry = {
                    "msg": card_text,
                    "buttons": [],
                    "markup": {"inline_keyboard": []},
                    "t": time.time(),
                    "title": _title_from_card(card_text),
                    "owner_username": None,
                    "owner_name": "Аноним",
                    "owner_id": None,
                    "rating": None,
                    "slug": None,
                    "num": None,
                }
                claims[key] = entry
            if not uid:
                log(f"[reserve] лот {key}: нет user_id в callback")
                _answer(text="Не удалось определить пользователя",
                        show_alert=True)
                return
            if entry.get("claimed_by"):
                log(f"[reserve] лот {key} уже занят {entry.get('claimed_by')}")
                _answer(text="Лот уже забронирован", show_alert=True)
                return
            now = time.time()
            with _LAST_RESERVE_MUT:
                wait = _LAST_RESERVE.get(uid, 0.0) + RESERVE_CD - now
            if wait > 0:
                log(f"[reserve] лот {key}: кд {int(wait) + 1}с для {uid}")
                _answer(text=f"⏳ КД: подождите {int(wait) + 1} сек "
                             f"перед следующим лотом",
                        show_alert=True)
                return
            entry["claimed_by"] = uid
            entry["claimed_at"] = time.time()
            with _LAST_RESERVE_MUT:
                _LAST_RESERVE[uid] = time.time()
            # Копируем данные ДО выхода из lock
            entry_snapshot = dict(entry)
            chat_id_snap = chat_id
            msg_id_snap = msg_id

        # Отвечаем МГНОВЕННО — пользователь видит «Забронировано»
        _answer(text="Лот забронирован — проверьте ЛС ✨")

        # Фоновая запись на диск — не блокирует callback
        def _persist_bg():
            try:
                _persist_claims(claims)
                save_state(state)
            except Exception:
                pass
        threading.Thread(target=_persist_bg, daemon=True).start()

        text = reserve_pm_text(entry_snapshot)
        markup = reserve_pm_markup(entry_snapshot)
        if chat_id_snap and msg_id_snap:
            _edit_schedule(chat_id_snap, msg_id_snap, key,
                           entry_snapshot.get("title") or "Лот")
        if uid:
            def _pm():
                try:
                    r = _http_post(f"{BOT_API}/sendMessage", {
                        "chat_id": uid, "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                        "reply_markup": markup,
                    }, tries=2)
                    log(f"[reserve][pm] {key} ok={(r or {}).get('ok')} "
                        f"chat={uid}")
                except Exception as e:
                    log(f"[reserve][pm] {key} uid={uid} ОШИБКА: {e}")
            threading.Thread(target=_pm, daemon=True).start()
        log(f"[reserve] лот {key} забронирован пользователем {uid}")
    except Exception:
        log("[reserve] НЕОЖИДАННАЯ ошибка:\n" + traceback.format_exc())


# ------------------------- цикл -------------------------
def _norm_topic(s):
    return (s or "").strip().lower().replace("ё", "е")


_BOT_CLIENT = None
_STATE = {}  # инициализируется в main() перед стартом


async def _get_bot_client():
    """MTProto-клиент от имени бота (админ форума — гарантированный доступ к темам)."""
    global _BOT_CLIENT
    if _BOT_CLIENT is None:
        bot = TelegramClient(os.path.join(HERE, "bot_session"), API_ID, API_HASH,
                             proxy=_PROXY_TUPLE)
        await bot.start(bot_token=BOT_TOKEN)
        _BOT_CLIENT = bot
    return _BOT_CLIENT


async def discover_topics(client, state):
    """Ищет темы форума по названиям -> id. Кэш на 6 часов."""
    required = {_norm_topic(v) for v in TOPIC_NAMES.values()}
    cached = dict(state.get("topics") or {})
    stale = time.time() - state.get("topics_t", 0) > 6 * 3600
    if state.get("topics_ok") and not stale and cached:
        return cached
    clients = [client]
    try:
        clients.append(await _get_bot_client())
    except Exception as e:
        print(f"[topics] бот-клиент: {e}", flush=True)
    last_err = "нет доступа к темам"
    for cli in clients:
        try:
            chat = await asyncio.wait_for(cli.get_entity(FORUM_CHAT_ID), timeout=30)
            res = await asyncio.wait_for(cli(functions.messages.GetForumTopicsRequest(
                peer=chat, offset_date=0, offset_id=0, offset_topic=0, limit=100)), timeout=45)
            got = {_norm_topic(getattr(t, "title", None)): t.id
                   for t in (getattr(res, "topics", None) or [])}
            if got:
                topics = dict(got)
                with _STATE_LOCK:
                    state["topics"] = topics
                    state["topics_t"] = time.time()
                    state["topics_ok"] = True
                save_state(state)
                print(f"[topics] реальные темы: {got}", flush=True)
                missing = required - set(got)
                if missing:
                    print(f"[topics] нет тем: {sorted(missing)} — "
                          f"пропуск (тема не в форуме)", flush=True)
                return topics
            last_err = f"тем нашлось только: {list(got)}"
        except Exception as e:
            last_err = str(e)
    print(f"[topics] {last_err}; перечитаю в след. цикле", flush=True)
    topics = dict(state.get("topics") or {})
    topics.update(FALLBACK_TOPICS)
    with _STATE_LOCK:
        state["topics"] = topics
        state["topics_ok"] = False
    save_state(state)
    return topics


_STOP_EVT = threading.Event()
_STATE_LOCK = threading.RLock()
_CLAIM_LOCK = threading.Lock()  # атомарный захват лота

_CYCLE_LOCK = threading.Lock()
_CYCLE_CACHE = {}
_CYCLE_LOCKS = {}
_CYCLE_MISS = object()


def updates_worker(state):
    """Фон: getUpdates, обработка нажатий кнопок и поиск ID тем по сообщениям."""
    offset = state.get("updates_offset", 0)
    err_n = 0
    # Поток update_id может «откатиться» при смене API-датацентра/прокси:
    # Telegram начинает отдавать апдейты с update_id ниже сохранённого offset,
    # и новые сообщения застревают за ними. Если первый ожидающий апдейт ниже
    # нашего offset — сбрасываем offset к началу очереди.
    if offset:
        try:
            _refresh_bot_session(force=True)
            rr = _BOT_SESSION.get(
                f"{BOT_API}/getUpdates",
                params={"offset": 0, "limit": 1, "timeout": 0},
                timeout=12)
            rr.raise_for_status()
            first_id = ((rr.json().get("result") or [{}])[0]).get("update_id")
            if first_id is not None and first_id + 1 < offset:
                log(f"[updates] поток откатился: первый апдейт {first_id}, "
                    f"сохранённый offset {offset} -> сброс на 0")
                offset = 0
        except Exception as e:
            log(f"[updates] предпроверка offset не удалась: "
                f"{type(e).__name__}: {e}")
    while not _STOP_EVT.is_set():
        try:
            resp = _BOT_SESSION.get(
                f"{BOT_API}/getUpdates",
                params={"offset": offset, "limit": 100, "timeout": 10},
                timeout=15)
            if resp.status_code == 409:
                err_n += 1
                if err_n % 5 == 1:
                    log("! КОНФЛИКТ getUpdates (409): бот запущен в ДВУХ "
                        "экземплярах с одним токеном. Лишний инстанс ломает "
                        "кнопки — остановите его.")
                time.sleep(2)
                continue
            data = resp.json()
        except Exception as e:
            err_n += 1
            log(f"[getUpdates] сеть/ответ (раз {err_n}): {type(e).__name__}: {e}")
            time.sleep(2)
            continue
        err_n = 0
        try:
            last_id = None
            # Нажатия кнопок — в начало пакета (стабильная сортировка):
            # их обработка не должна ждать, пока пережуём старые сообщения
            # из бэклога -> тост/бронь приходят практически мгновенно.
            ups = sorted((data.get("result") or []),
                         key=lambda u: 0 if u.get("callback_query") else 1)
            for u in ups:
                uid = u.get("update_id")
                if uid is not None:
                    last_id = uid
                    offset = max(offset, uid + 1)
                cq = u.get("callback_query") or {}
                if cq:
                    # В ФОНЕ, не в цикле getUpdates: одиночный поток при потоке
                    # нажатий не успевает ответить в 10с -> «query is too old».
                    # _CLAIM_LOCK/_STATE_LOCK обеспечивают атомарность захвата.
                    fut = _BG_POOL.submit(handle_callback, state, cq)

                    def _log_cb_err(f):
                        try:
                            f.result()
                        except Exception:
                            log("[cb] НЕОЖИДАННАЯ ошибка обработки:\n" +
                                traceback.format_exc())
                    fut.add_done_callback(_log_cb_err)
                else:
                    m = u.get("message") or {}
                    text = (m.get("text") or "").strip()
                    ch = (m.get("chat") or {}).get("id")
                    tid = m.get("message_thread_id")
                    if PM_MODE and ch and ch > 0:
                        with _STATE_LOCK:
                            state["pm_chat_id"] = ch
                        save_state(state)
                        print(f"[pm] личный чат найден: {ch}", flush=True)
                    if not PM_MODE and ch and ch > 0 and text:
                        # Личка с ботом (группа/форум имеют отрицательные id).
                        # /start — сигнал, что боту можно писать в ЛС.
                        if text.startswith("/start"):
                            _BG_POOL.submit(
                                _http_post, f"{BOT_API}/sendMessage", {
                                    "chat_id": ch,
                                    "text": ("👋 Привет! Я бот-парсер подарков.\n\n"
                                             "Нажимай кнопку <b>«Забронировать»</b> под лотом "
                                             "в форуме — карточка лота и контакт продавца придут "
                                             "тебе сюда в ЛС."),
                                    "parse_mode": "HTML",
                                    "disable_web_page_preview": True,
                                }, 2)
                            log(f"[start] пользователь {ch} запустил бота")
                        elif text.startswith("/test"):
                            # Тестовая карточка лота с кнопкой «Забронировать» —
                            # для проверки нажатия без ожидания нового листинга.
                            test_key = "test:debug"
                            with _STATE_LOCK:
                                claims = state.setdefault("claims", {})
                                claims[test_key] = {
                                    "msg": "",
                                    "buttons": [
                                        ("Open Profile",
                                         "https://t.me/durov",
                                         BTN_PROFILE_ICON),
                                        ("View Gift",
                                         "https://t.me/nft/HeartLocket-2026",
                                         BTN_GIFT_ICON),
                                    ],
                                    "markup": None,
                                    "t": time.time(),
                                    "title": "Heart Locket",
                                    "owner_username": "durov",
                                    "owner_name": "Дуров",
                                    "owner_id": 1,
                                    "rating": 2,
                                    "slug": "HeartLocket",
                                    "num": 2026,
                                }
                                _persist_claims(claims)
                            test_msg = "\n".join([
                                f'{EMOJI["new"]}"Heart Locket"',
                                "",
                                f'{EMOJI["model"]}Модель: Heart Locket',
                                f'{EMOJI["price"]} Цена: <b>5000{EMOJI["stars"]}</b>/ '
                                f'55{EMOJI["gram"]}',
                                f'{EMOJI["lvl"]} LVL: <b>2</b>',
                                f'{EMOJI["quantity"]} Количество: <b>7</b>',
                                f'{EMOJI["premium"]} Premium: {EMOJI["prem_yes"]}',
                                f'{EMOJI["country"]} Страна: Россия',
                                f'{EMOJI["status"]} Статус: <b>Онлайн</b>',
                            ])
                            try:
                                bot_send(ch, test_msg, claims[test_key]["buttons"],
                                         claim_key=test_key)
                                log(f"[test] тестовая карточка отправлена в {ch}")
                            except Exception as e:
                                log(f"[test] ОШИБКА отправки: {e}")
                    if ch == FORUM_CHAT_ID and tid and text:
                        key = _norm_topic(text)
                        if key in {_norm_topic(v) for v in TOPIC_NAMES.values()}:
                            with _STATE_LOCK:
                                topics = dict(state.get("topics") or {})
                                topics[key] = tid
                                state["topics"] = topics
                            save_state(state)
                            print(f"[topics] тема «{text}» -> id {tid}", flush=True)
            # Поток update_id может откатиться при смене API-датацентра/прокси.
            # Если Telegram прислал апдейты ниже нашего offset — принимаем его
            # счётчик, иначе новые сообщения будут перекрыты «застрявшим» старым.
            if last_id is not None and last_id + 1 < offset:
                log(f"[updates] принимаю текущий поток (апдейт {last_id}): "
                    f"offset {offset} -> {last_id + 1}")
                offset = last_id + 1
        except Exception:
            log("!!! updates_worker: непредвиденная ошибка:\n" +
                traceback.format_exc())
            write_crash(traceback.format_exc())
            time.sleep(3)
        with _STATE_LOCK:
            state["updates_offset"] = offset
        time.sleep(0.2)


async def poll_once(clients, state):
    # переподключаем отвалившиеся клиенты
    for c in clients:
        try:
            if not c.is_connected():
                await c.connect()
        except Exception:
            pass
    with _CYCLE_LOCK:
        _CYCLE_CACHE.clear()
        _CYCLE_LOCKS.clear()

    def _pick(gid):
        return clients[gid % len(clients)]

    chat_id = FORUM_CHAT_ID if CHAT_ID_OVERRIDE is None else CHAT_ID_OVERRIDE
    with _STATE_LOCK:
        state["chat_id"] = chat_id
    if not chat_id:
        print("! Бот не нашёл чат.")
        return

    now = time.time()

    # каталог базовых подарков
    if not state.get("gift_ids") or \
            now - state.get("last_catalog", 0) > CATALOG_REFRESH_MINUTES * 60:
        try:
            ids = await get_gift_catalog(clients[0])
            if ids:
                random.shuffle(ids)
                with _STATE_LOCK:
                    state["gift_ids"] = ids
                    state["last_catalog"] = now
                save_state(state)
                print(f"[каталог] базовых подарков: {len(ids)}", flush=True)
        except Exception as e:
            print(f"[каталог] {e}", flush=True)

    gift_ids = state.get("gift_ids") or []
    if not gift_ids:
        return

    prev = state.get("prev_active", {})
    skip = state.get("skip", {})
    warmup = not state.get("warmup_done", False)

    topics = {} if PM_MODE else await discover_topics(clients[0], state)

    # SKIP_TTL пропускает только ПУСТЫЕ типы. Типы с активными лотами раньше
    # опрашивались в каждом цикле без ограничения частоты — это и был основной
    # источник флуда. Добавляем минимальный интервал на каждый тип.
    last_poll = state.setdefault("last_poll", {})
    fresh = [gid for gid in gift_ids
             if now - last_poll.get(gid, 0) >= MIN_TYPE_INTERVAL]
    if warmup:
        # на прогреве проходим всё — иначе базовый снимок не построить
        batch = fresh or gift_ids
    else:
        batch = [gid for gid in fresh
                 if not (gid in skip and now - skip[gid] < SKIP_TTL)]
    for gid in batch:
        last_poll[gid] = now

    if warmup:
        # полный глубокий проход всех типов без отправок — строим базовый снимок
        deep_sel = set(batch)
    else:
        # Новые листинги всегда в первой странице (сортировка по времени
        # последнего изменения). Глубокая досъемка в бою не нужна — это
        # только удваивает число запросов и замедляет цикл.
        deep_sel = set()

    sent = 0
    skipped = 0
    errs = 0
    found_total = 0
    warmup_new = 0
    by_topic = {}
    skip_reasons = {}
    discovered_slugs = set()  # slugs для upgrade-мониторинга
    discovered_max = {}       # коллекция -> макс. номер (для старта мониторинга)
    # 2 акка — можно поднять параллельность
    sem_scan = asyncio.Semaphore(8)
    sem_res = asyncio.Semaphore(4)
    t0 = time.time()

    async def _fetch_one(gid):
        async with sem_scan:
            rows = await fetch_listings(_pick(gid), gid, deep=gid in deep_sel)
        if rows is None:
            return None
        with _STATE_LOCK:
            if not rows:
                skip[gid] = now
            else:
                skip.pop(gid, None)
        return rows

    async def _resolve(key, gift, users_by_id):
        async with sem_res:
            cli = _pick(getattr(gift, "gift_id", 0))
            seller = await resolve_seller(cli, state, users_by_id, gift)
        return key, gift, seller

    async def _send_one(key, gift, seller):
        nonlocal sent, skipped
        try:
            reason = is_eligible(seller)
            if reason:
                skipped += 1
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                return
            feeds = candidate_feeds(gift, seller, is_new=True)
            if not feeds:
                skipped += 1
                skip_reasons["нет категории"] = \
                    skip_reasons.get("нет категории", 0) + 1
                return
            msg = format_compact(key, gift, seller)
            btns = build_buttons(gift, seller)
            with _STATE_LOCK:
                state.setdefault("claims", {})[key] = {
                    "msg": msg,
                    "buttons": btns,
                    "markup": pm_markup(btns),
                    "t": time.time(),
                    "title": getattr(gift, "title", "") or "Аноним",
                    "owner_username": seller.get("username"),
                    "owner_name": seller.get("name") or "Аноним",
                    "owner_id": seller.get("user_id"),
                    "rating": seller.get("rating"),
                    "slug": getattr(gift, "slug", None),
                    "num": getattr(gift, "num", None),
                }
                _persist_claims(state["claims"])
            sent_any = False
            for feed in feeds:
                tid = None if PM_MODE else topics.get(
                    _norm_topic(TOPIC_NAMES[feed]))
                if not PM_MODE and not tid:
                    print(f"[route] для темы «{TOPIC_NAMES[feed]}» "
                          f"id не найден", flush=True)
                    continue
                try:
                    await send_limiter().acquire()
                    await asyncio.to_thread(
                        bot_send, chat_id, msg, btns, tid, key)
                except RuntimeError as e:
                    if "BUTTON_USER_INVALID" in str(e) and btns:
                        safe = [b for b in btns
                                if "Open Profile" not in b[0]]
                        await send_limiter().acquire()
                        await asyncio.to_thread(
                            bot_send, chat_id, msg, safe or None,
                            tid, key)
                    else:
                        raise
                sent_any = True
                by_topic[feed] = by_topic.get(feed, 0) + 1
            if sent_any:
                sent += 1
            else:
                skipped += 1
                skip_reasons["тема не найдена"] = \
                    skip_reasons.get("тема не найдена", 0) + 1
        except Exception as e:
            print(f"[send] {e}", flush=True)

    scan = {asyncio.ensure_future(_fetch_one(gid)) for gid in batch}
    res = set()
    scheduled = set()
    resolved = 0
    deferred = 0
    pending = []
    last_gifts = deque(maxlen=4)
    while scan or res or pending:
        if scan or res:
            done, _ = await asyncio.wait(scan | res, return_when=asyncio.FIRST_COMPLETED)
            for fut in done:
                if fut in scan:
                    scan.discard(fut)
                    try:
                        rows = fut.result()
                    except SessionDead:
                        # сессия отозвана — молча глотать нельзя,
                        # иначе бот будет долбить мёртвый аккаунт
                        raise
                    except Exception as e:
                        print(f"[scan] {e}", flush=True)
                        continue
                    if rows is None:
                        errs += 1
                        continue
                    if not rows:
                        continue
                    for key, gift, users_by_id in rows:
                        # Собираем названия коллекций для upgrade-мониторинга
                        # slug в данных имеет вид "CollectionName-N"; суффикс -N отбрасываем
                        gslug = getattr(gift, "slug", None)
                        if gslug:
                            gcol = re.sub(r"-\d+$", "", gslug)
                            if gcol:
                                discovered_slugs.add(gcol)
                                # Обновляем максимальный номер по коллекции
                                m = re.search(r"-(\d+)$", gslug)
                                if m:
                                    gnum = int(m.group(1))
                                    if gnum > discovered_max.get(gcol, 0):
                                        discovered_max[gcol] = gnum
                        if key in prev:
                            continue  # старый листинг — не шлём никогда
                        found_total += 1
                        if warmup:
                            with _STATE_LOCK:
                                prev[key] = now
                            warmup_new += 1
                            continue
                        if sent >= MAX_SEND_PER_CYCLE:
                            continue
                        if key in scheduled:
                            continue
                        # Каждый resolve — это отдельный запрос
                        # GetFullUserRequest за токен аккаунта. Без потолка
                        # один цикл съедал весь лимит (и рвал сессию),
                        # остальные ключи доберутся в следующем цикле.
                        if resolved >= MAX_RESOLVE_PER_CYCLE:
                            deferred += 1
                            continue
                        scheduled.add(key)
                        resolved += 1
                        res.add(asyncio.ensure_future(
                            _resolve(key, gift, users_by_id)))
                else:
                    res.discard(fut)
                    try:
                        key, gift, seller = fut.result()
                    except Exception as e:
                        print(f"[resolve] {e}", flush=True)
                        continue
                    pending.append((key, gift, seller))
        for _ in range(10):
            if not pending:
                break
            cand = [i for i, (_, g, _) in enumerate(pending)
                    if getattr(g, "gift_id", None) not in last_gifts]
            if cand:
                best = random.choice(cand)
            else:
                best = random.randrange(len(pending))
            key, gift, seller = pending.pop(best)
            last_gifts.append(getattr(gift, "gift_id", None))
            await _send_one(key, gift, seller)
            with _STATE_LOCK:
                prev[key] = now
            await asyncio.sleep(SEND_PAUSE)

    # управление warmup
    if warmup:
        wp = state.get("warmup_pass", 0) + 1
        with _STATE_LOCK:
            state["warmup_pass"] = wp
        print(f"[warmup] проход {wp}: новых ключей {warmup_new}, "
              f"всего в снимке {len(prev)}", flush=True)
        done = (wp >= WARMUP_MIN_PASS and warmup_new < WARMUP_STABLE) or \
               wp >= WARMUP_MAX_PASS
        if done:
            with _STATE_LOCK:
                state["warmup_done"] = True

    print(f"[цикл] обход {len(batch)} типов за {time.time() - t0:.0f}с: "
          f"ошибок {errs}, новых {found_total}, отправлено {sent}, "
          f"фильтр {skipped}, продавцов {resolved}"
          + (f" (отложено {deferred})" if deferred else ""), flush=True)
    if sent:
        print(f"[new {sent}] отправлено: {by_topic}", flush=True)
    if skipped:
        print(f"[filter] пропущено {skipped}: {skip_reasons}", flush=True)

    if len(prev) > 15000:
        cutoff = now - 30 * 86400
        prev = {k: v for k, v in prev.items() if v >= cutoff}

    claims = state.get("claims") or {}
    if len(claims) > 500:
        cl_cut = now - 24 * 3600
        claims = {k: v for k, v in claims.items()
                  if v.get("claimed_by") or v.get("t", 0) >= cl_cut}
        with _STATE_LOCK:
            state["claims"] = claims
        _persist_claims(claims)

    cooldowns = state.get("claim_cooldown") or {}
    if cooldowns:
        cd_cut = now - 3600
        stale_cd = [k for k, v in cooldowns.items() if v.get("t", 0) < cd_cut]
        if stale_cd:
            with _STATE_LOCK:
                for k in stale_cd:
                    cooldowns.pop(k, None)
            print(f"[cleanup] claim_cooldown: удалено {len(stale_cd)} устаревших",
                  flush=True)

    write_tpl = state.get("write_tpl") or {}
    if write_tpl and len(write_tpl) > 1000:
        with _STATE_LOCK:
            state["write_tpl"] = {}
        print("[cleanup] write_tpl сброшен (>1000 записей)", flush=True)

    user_cache = state.get("user_cache") or {}
    if user_cache and len(user_cache) > 5000:
        uc_cut = now - SELLER_CACHE_TTL * 3
        stale_uc = [k for k, v in user_cache.items() if v.get("t", 0) < uc_cut]
        if stale_uc:
            with _STATE_LOCK:
                for k in stale_uc:
                    user_cache.pop(k, None)
            print(f"[cleanup] user_cache: удалено {len(stale_uc)} устаревших",
                  flush=True)

    with _STATE_LOCK:
        state["prev_active"] = prev
        state["skip"] = skip
        # Сохраняем рыночные максимумы как подсказку для старта базы
        # новых upgrade-коллекций (реальная база ставится бинарным поиском).
        if discovered_max:
            hints = dict(state.get("upgrade_market_max") or {})
            for gcol, gnum in discovered_max.items():
                if gnum > hints.get(gcol, 0):
                    hints[gcol] = gnum
            state["upgrade_market_max"] = hints
    _persist_claims(state.get("claims") or {})
    save_state(state)


# ------------------------- пул прокси (авто-переключение) -------------------------
_RECONNECT = set()


def _proxy_alive(proxy):
    try:
        s = socket.create_connection(proxy, timeout=2)
        s.close()
        return True
    except OSError:
        return False


def _pinned_port(cfg):
    """Порт прокси, закреплённый за аккаунтом (proxy_port в ACCOUNTS)."""
    try:
        return int(cfg.get("proxy_port")) if cfg.get("proxy_port") else None
    except (TypeError, ValueError):
        return None


def _proxy_tuple_for(cfg):
    """SOCKS5-кортеж для конкретного аккаунта.

    Если у аккаунта закреплён proxy_port — идём только через него. Ротация
    ЗАПРЕЩЕНА: смена IP на живой сессии = Telegram отзывает ключ.
    """
    if not PROXY_ENABLED:
        return None
    port = _pinned_port(cfg)
    if port:
        return (ProxyType.SOCKS5, PROXY_HOST, port)
    return _PROXY_TUPLE


def _next_proxy(name, state, start=0):
    with _STATE_LOCK:
        idx_map = state.setdefault("proxy_index", {})
    cur = idx_map.get(name, start)
    n = len(PROXY_LIST)
    for i in range(n):
        idx = (cur + i) % n
        p = PROXY_LIST[idx]
        if _proxy_alive(p):
            idx_map[name] = idx
            return p, idx
    return PROXY_LIST[cur % n], cur % n


def proxy_watchdog(state):
    while not _STOP_EVT.is_set():
        time.sleep(30)
        if not PROXY_ENABLED:
            continue
        try:
            with _STATE_LOCK:
                idx_map = dict(state.get("proxy_index") or {})
            for cfg in ACCOUNTS:
                if _pinned_port(cfg):
                    # Закреплённый аккаунт не ротируем — предупреждаем,
                    # но НЕ переключаем: смена IP убьёт сессию.
                    p = (PROXY_HOST, _pinned_port(cfg))
                    if not _proxy_alive(p):
                        print(f"[proxy] «{cfg['name']}»: закреплённый прокси "
                              f"{p[1]} не отвечает — аккаунт работать не будет "
                              f"(ротация отключена намеренно)", flush=True)
                    continue
                idx = idx_map.get(cfg["name"], 0) % len(PROXY_LIST)
                if not _proxy_alive(PROXY_LIST[idx]):
                    print(f"[proxy] «{cfg['name']}»: прокси "
                          f"{PROXY_LIST[idx][1]} мёртв — переключаюсь...", flush=True)
                    with _PROXY_LOCK:
                        _RECONNECT.add(cfg["name"])
        except Exception as e:
            print(f"[proxy] watchdog: {e}", flush=True)


async def _reconnect_client(clients, state, i, cfg):
    if not _HAS_PROXY:
        print(f"[proxy] «{cfg['name']}»: python-socks не установлен, "
              f"переключение невозможно", flush=True)
        return
    try:
        await clients[i].disconnect()
    except Exception:
        pass
    # Закреплённый аккаунт переподключаем на ТОТ ЖЕ порт, иначе IP сменится
    if _pinned_port(cfg):
        p = (PROXY_HOST, _pinned_port(cfg))
        print(f"[proxy] «{cfg['name']}»: переподключаю на закреплённый "
              f"порт {p[1]} (IP не меняется)", flush=True)
        cli = TelegramClient(os.path.join(HERE, cfg["session"]),
                             cfg["api_id"], cfg["api_hash"],
                             proxy=(ProxyType.SOCKS5, p[0], p[1]))
        try:
            await asyncio.wait_for(cli.start(), timeout=45)
            clients[i] = ThrottledClient(cli)
            return
        except Exception as e:
            print(f"[proxy] «{cfg['name']}» порт {p[1]}: {e}", flush=True)
            return
    with _STATE_LOCK:
        state.setdefault("proxy_index", {})
    for _ in range(len(PROXY_LIST)):
        with _STATE_LOCK:
            state["proxy_index"][cfg["name"]] = \
                (state["proxy_index"].get(cfg["name"], 0) + 1) % len(PROXY_LIST)
        proxy, _ = _next_proxy(cfg["name"], state)
        cli = TelegramClient(os.path.join(HERE, cfg["session"]),
                             cfg["api_id"], cfg["api_hash"],
                             proxy=(ProxyType.SOCKS5, proxy[0], proxy[1]))
        try:
            await asyncio.wait_for(cli.start(), timeout=45)
            # Обязательно оборачиваем: сырой клиент шёл бы мимо лимитера
            clients[i] = ThrottledClient(cli)
            print(f"[proxy] «{cfg['name']}» переключён на прокси {proxy[1]}",
                  flush=True)
            return
        except Exception as e:
            print(f"[proxy] «{cfg['name']}» прокси {proxy[1]}: {e}", flush=True)
    print(f"[proxy] «{cfg['name']}»: нет живых прокси", flush=True)


async def main():
    log("=" * 60)
    log("[boot] старт бота (Python " + sys.version.split()[0] + ")")
    if not acquire_lock():
        return

    if PROXY_ENABLED:
        if not _HAS_PROXY:
            print("! python-socks не установлен — прокси не работает, "
                  "установите: pip install python-socks")
        else:
            _refresh_bot_session(force=True)
            print(f"Прокси включён: SOCKS5 {PROXY_HOST}:{PROXY_PORT}. "
                  f"У аккаунтов с proxy_port в ACCOUNTS — свой порт.",
                  flush=True)

    state = load_state()
    global _STATE
    _STATE = state

    # Подтягиваем заявки из claims.json (они переживают перезапуск и пишутся
    # сразу при отправке). state.json может быть старше.
    disk_claims = _load_claims()
    if disk_claims:
        claims = state.get("claims") or {}
        claims.update(disk_claims)
        with _STATE_LOCK:
            state["claims"] = claims

    # Миграция на новые топики: сбрасываем кэш если там старые названия или нет новой темы
    old_keys = {"new gifts", "dear", "mamonts", "mamonts > 10+ ton"}
    with _STATE_LOCK:
        topics = state.get("topics") or {}
        need_reset = any(k in old_keys for k in topics)
        # новые топики NFT от 10 ton и Upgrade добавлены — нужен перечит
        if not need_reset and "nft от 10 ton" not in topics:
            need_reset = True
        if not need_reset and "upgrade" not in topics:
            need_reset = True
        if need_reset:
            state["topics"] = {}
            state["topics_ok"] = False
            state["topics_t"] = 0
            print("[reset] кэш тем сброшен (старые/новые категории) — перечитаю форум", flush=True)
    # Сброс истории лотов при КАЖДОМ запуске: собираем рынок заново (warmup)
    # и присылаем только листинги, появившиеся после этого запуска.
    with _STATE_LOCK:
        state["prev_active"] = {}
        state["warmup_done"] = False
        state["warmup_pass"] = 0
        state["deep_rot"] = 0
    save_state(state)
    print("[reset] история лотов очищена — собираю свежий снимок рынка",
          flush=True)

    async def _acquire_clients():
        clis = []
        names = []
        for cfg in ACCOUNTS:
            # У каждого аккаунта СВОЙ прокси (proxy_port), иначе все
            # сидят на одном IP — и с падением одного прокси Telegram
            # отзывает сразу все сессии.
            cli = TelegramClient(os.path.join(HERE, cfg["session"]),
                                 cfg["api_id"], cfg["api_hash"],
                                 proxy=_proxy_tuple_for(cfg))
            print(f"Вход в аккаунт «{cfg['name']}» "
                  f"(при первом запуске введите номер и код)...", flush=True)
            try:
                await asyncio.wait_for(cli.connect(), timeout=60)
            except Exception as e:
                print(f"! Не удалось соединиться с «{cfg['name']}»: {e}",
                      flush=True)
                continue
            try:
                if not await cli.is_user_authorized():
                    # is_user_authorized() ловит ЛЮБОЙ RPCError, включая
                    # AUTH_KEY_UNREGISTERED. Поэтому «нет сессии» и
                    # «сессию отозвал Telegram» выглядят одинаково —
                    # различаем по наличию файла на диске.
                    sfile = os.path.join(HERE, cfg["session"]) + ".session"
                    reason = ("сессии нет на диске"
                              if not os.path.exists(sfile)
                              else "Telegram отозвал сессию")
                    print(f"! Аккаунт «{cfg['name']}» не авторизован "
                          f"({reason}) — пропускаю.\n"
                          f"  Чтобы восстановить: "
                          f"py -3.14 login.py {cfg['session']}", flush=True)
                    try:
                        await cli.disconnect()
                    except Exception:
                        pass
                    continue
            except SESSION_DEAD_ERRORS as e:
                print(f"! Сессия «{cfg['name']}» отозвана Telegram "
                      f"({type(e).__name__}) — нужен новый логин: "
                      f"python login.py {cfg['session']}", flush=True)
                continue
            except Exception as e:
                print(f"! Не удалось проверить авторизацию «{cfg['name']}»: {e}",
                      flush=True)
                continue
            try:
                res = await asyncio.wait_for(cli.start(), timeout=90)
            except SESSION_DEAD_ERRORS as e:
                print(f"! Сессия «{cfg['name']}» отозвана Telegram "
                      f"({type(e).__name__}) — нужен новый логин: "
                      f"python login.py {cfg['session']}", flush=True)
                try:
                    await cli.disconnect()
                except Exception:
                    pass
                continue
            except Exception as e:
                print(f"! Не удалось войти в аккаунт «{cfg['name']}»: {e}",
                      flush=True)
                try:
                    await cli.disconnect()
                except Exception:
                    pass
                continue
            try:
                me = await cli.get_me()
            except Exception as e:
                print(f"! Не удалось проверить аккаунт «{cfg['name']}»: {e}",
                      flush=True)
                try:
                    await cli.disconnect()
                except Exception:
                    pass
                continue
            if not me:
                print(f"! Не удалось войти в аккаунт «{cfg['name']}».", flush=True)
                try:
                    await cli.disconnect()
                except Exception:
                    pass
                continue
            print(f"Аккаунт «{cfg['name']}»: {user_name(me)} "
                  f"(@{me.username or 'нет'})", flush=True)
            # Обёртка: ВСЕ запросы этого аккаунта идут через лимитер
            clis.append(ThrottledClient(cli))
            names.append(cfg["name"])
            # Разводим аккаунты по времени старта, иначе N аккаунтов
            # бьют в Telegram синхронно на каждом цикле.
            if len(clis) > 1:
                await asyncio.sleep(ACCOUNT_STAGGER)
        return clis, names

    async def _drop_dead(idx):
        """Убирает аккаунт, чью сессию отозвали, и чистит его лимитер."""
        name = client_names[idx] if idx < len(client_names) else "?"
        cli = clients[idx]
        print(f"[сессия] «{name}» отключён. Требуется новый логин: "
              f"python login.py {ACCOUNTS[0]['session'] if len(ACCOUNTS) == 1 else name}",
              flush=True)
        try:
            await cli.disconnect()
        except Exception:
            pass
        # лимитеры ключуются по СЫРОМ клиенту, а в списке лежит обёртка
        with _RL_LOCK:
            _LIMITERS.pop(id(getattr(cli, "_c", cli)), None)
        clients.pop(idx)
        if idx < len(client_names):
            client_names.pop(idx)
        return name

    # Потоки запускаем ДО подключения парсинг-аккаунтов: кнопки брони
    # должны отвечать мгновенно даже пока/если аккаунт не вошёл.
    threading.Thread(target=_edit_worker, daemon=True).start()
    threading.Thread(target=_saver_loop, daemon=True).start()
    th = threading.Thread(target=updates_worker, args=(state,), daemon=True)
    th.start()
    threading.Thread(target=proxy_watchdog, args=(state,), daemon=True).start()
    log("[boot] потоки запущены: updates_worker, saver, proxy_watchdog, edit_worker")

    clients, client_names = await _acquire_clients()
    print(f"Аккаунтов для парсинга: {len(clients)}"
          f"{' (лимит ' + str(RPS_PER_ACCOUNT) + ' зап/с на аккаунт)' if clients else ''}",
          flush=True)

    # Upgrade-мониторинг: отдельный таймер, не зависит от POLL_SECONDS
    try:
        # Upgrade-мониторинг в отдельной задаче — не блокирует цикл рынка
        upgrade_task = None

        while True:
            if not clients:
                print("! Парсинг-аккаунты ещё не подключены — повторю через "
                      "30с (кнопки брони работают)", flush=True)
                await asyncio.sleep(30)
                try:
                    clients, client_names = await _acquire_clients()
                except Exception as e:
                    print(f"! Повтор подключения аккаунтов: {e}", flush=True)
                continue
            with _PROXY_LOCK:
                to_reconnect = list(_RECONNECT)
                _RECONNECT.clear()
            for cfg in ACCOUNTS:
                if cfg["name"] in to_reconnect:
                    i = next((j for j, a in enumerate(ACCOUNTS)
                              if a["name"] == cfg["name"]), None)
                    if i is not None and i < len(clients):
                        await _reconnect_client(clients, state, i, cfg)
            try:
                await poll_once(clients, state)
            except SessionDead as e:
                # Telegram отозвал сессию одного из аккаунтов. Убираем его
                # из ротации, остальные продолжают работать.
                dead = getattr(e, "client", None)
                try:
                    idx = clients.index(dead) if dead is not None else 0
                except ValueError:
                    idx = 0
                if clients:
                    await _drop_dead(idx)
                    print(f"[сессия] осталось аккаунтов: {len(clients)}"
                          f"{' — работа продолжается на них' if clients else ''}",
                          flush=True)
                else:
                    print("[сессия] живых аккаунтов не осталось. Бот ждёт "
                          "нового логина (python login.py <имя>).", flush=True)
                    await asyncio.sleep(30)
                    clients, client_names = await _acquire_clients()
                continue
            except Exception as e:
                print(f"[цикл] {e}")
                traceback.print_exc()
            # Запускаем upgrade-мониторинг как фоновую задачу (если не бежит)
            if upgrade_task is None or upgrade_task.done():
                def _log_upgrade_failure(fut):
                    try:
                        fut.result()
                    except Exception as e:
                        print(f"[upgrade] ЗАДАЧА УПАЛА: {type(e).__name__}: {e}",
                              flush=True)
                        traceback.print_exc()
                upgrade_task = asyncio.ensure_future(
                    poll_upgrades(clients, state))
                upgrade_task.add_done_callback(_log_upgrade_failure)
            if POLL_SECONDS:
                await asyncio.sleep(POLL_SECONDS)
    except BaseException:
        tb = traceback.format_exc()
        # KeyboardInterrupt = штатная остановка (Ctrl+C), не крэш.
        if isinstance(sys.exc_info()[1], KeyboardInterrupt):
            print("Остановлен пользователем (Ctrl+C).", flush=True)
            return
        print("!!! main: непредвиденная ошибка:", flush=True)
        print(tb, flush=True)
        write_crash(tb)
        raise
    finally:
        release_lock()
        flush_state()
        for cli in clients:
            try:
                await cli.disconnect()
            except BaseException:
                pass
        if _BOT_CLIENT is not None:
            try:
                await _BOT_CLIENT.disconnect()
            except BaseException:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Остановлен пользователем (Ctrl+C).", flush=True)
    except BaseException:
        tb = traceback.format_exc()
        write_crash(tb)
        raise
