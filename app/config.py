from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    target_chat_id: int
    admin_ids: frozenset[int]
    database_path: Path


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv("BOT_TOKEN", "").strip()
    chat_id_raw = os.getenv("TARGET_CHAT_ID", "").strip()
    admins_raw = os.getenv("ADMIN_IDS", "").strip()
    database_path = Path(os.getenv("DATABASE_PATH", "data/applicants.sqlite3"))

    missing = [
        name
        for name, value in {
            "BOT_TOKEN": token,
            "TARGET_CHAT_ID": chat_id_raw,
            "ADMIN_IDS": admins_raw,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Заполните {', '.join(missing)} в файле .env")

    try:
        chat_id = int(chat_id_raw)
        admin_ids = frozenset(int(value.strip()) for value in admins_raw.split(",") if value.strip())
    except ValueError as error:
        raise RuntimeError("TARGET_CHAT_ID и ADMIN_IDS должны быть числовыми ID Telegram.") from error

    if not admin_ids:
        raise RuntimeError("В ADMIN_IDS должен быть хотя бы один Telegram ID администратора.")

    return Settings(token, chat_id, admin_ids, database_path)

