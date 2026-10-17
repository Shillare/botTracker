"""Снять права старых владельцев и отключить их бизнес-подключение.

Права в `.env` меняются руками (переменная ``OWNER_IDS``), но в таблице
``admins`` старые ID остаются — бот продолжит считать их админами. Скрипт
приводит базу в соответствие с конфигурацией:

* снимает права со всех, кого нет в ``OWNER_IDS``;
* отключает бизнес-подключения, оставшиеся от прежних владельцев.

Запуск: ``python tools/reset_owners.py``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.database import (
    AdminRepository,
    BusinessConnectionRepository,
    Database,
)


async def main() -> int:
    settings = get_settings()
    owners = settings.owner_id_set

    db = Database(settings.db_path)
    await db.connect()
    try:
        admins = AdminRepository(db)
        connections = BusinessConnectionRepository(db)

        rows = await admins.get_all()
        print(f"Владельцы по конфигурации: {sorted(owners) or '—'}")
        print(f"Админы в базе: {[int(r['user_id']) for r in rows] or '—'}")

        removed = [
            user_id
            for user_id in (int(r["user_id"]) for r in rows)
            if user_id not in owners and await admins.remove(user_id)
        ]
        print(f"Сняты права: {removed or '—'}")

        disabled = [
            (row["id"], int(row["user_id"]))
            for row in await connections.list_all()
            if int(row["user_id"]) not in owners
            and row["is_enabled"]
            and await connections.set_enabled(row["id"], False)
        ]
        print(f"Подключения отключены: {disabled or '—'}")

        print("\nИтог:")
        print(f"  владельцы: {sorted(owners) or '—'}")
        print(f"  админы: {[int(r['user_id']) for r in await admins.get_all()] or '—'}")
        for row in await connections.list_all():
            state = "активна" if row["is_enabled"] else "отключена"
            print(f"  подключение {row['id']}: user_id={int(row['user_id'])} ({state})")
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
