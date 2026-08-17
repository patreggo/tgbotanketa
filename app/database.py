from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class Applicant:
    telegram_id: int
    username: str | None
    telegram_first_name: str | None
    telegram_last_name: str | None
    first_name: str | None
    last_name: str | None
    status: str
    invite_link: str | None
    created_at: str
    updated_at: str
    submitted_at: str | None
    join_requested_at: str | None
    approved_at: str | None
    joined_at: str | None
    left_at: str | None

    @property
    def full_name(self) -> str:
        return " ".join(part for part in (self.last_name, self.first_name) if part) or "—"


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS applicants (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                telegram_first_name TEXT,
                telegram_last_name TEXT,
                first_name TEXT,
                last_name TEXT,
                status TEXT NOT NULL,
                invite_link TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                submitted_at TEXT,
                join_requested_at TEXT,
                approved_at TEXT,
                joined_at TEXT,
                left_at TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_applicants_status ON applicants(status);
            """
        )
        self.connection.commit()

    def _audit(self, telegram_id: int, event: str, details: str | None = None) -> None:
        self.connection.execute(
            "INSERT INTO audit_log (telegram_id, event, details, created_at) VALUES (?, ?, ?, ?)",
            (telegram_id, event, details, utcnow()),
        )

    def begin_form(self, *, telegram_id: int, username: str | None, first_name: str | None, last_name: str | None) -> None:
        now = utcnow()
        self.connection.execute(
            """
            INSERT INTO applicants (
                telegram_id, username, telegram_first_name, telegram_last_name,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'filling', ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                telegram_first_name = excluded.telegram_first_name,
                telegram_last_name = excluded.telegram_last_name,
                first_name = NULL,
                last_name = NULL,
                status = 'filling',
                invite_link = NULL,
                submitted_at = NULL,
                join_requested_at = NULL,
                approved_at = NULL,
                joined_at = NULL,
                left_at = NULL,
                updated_at = excluded.updated_at
            """,
            (telegram_id, username, first_name, last_name, now, now),
        )
        self._audit(telegram_id, "form_started")
        self.connection.commit()

    def save_first_name(self, telegram_id: int, first_name: str) -> None:
        self.connection.execute(
            "UPDATE applicants SET first_name = ?, updated_at = ? WHERE telegram_id = ?",
            (first_name, utcnow(), telegram_id),
        )
        self.connection.commit()

    def submit_form(self, telegram_id: int, last_name: str) -> Applicant:
        now = utcnow()
        self.connection.execute(
            """UPDATE applicants SET last_name = ?, status = 'registered', submitted_at = ?,
               updated_at = ? WHERE telegram_id = ?""",
            (last_name, now, now, telegram_id),
        )
        self._audit(telegram_id, "form_submitted")
        self.connection.commit()
        applicant = self.get(telegram_id)
        assert applicant is not None
        return applicant

    def save_invite_link(self, telegram_id: int, invite_link: str) -> None:
        self.connection.execute(
            "UPDATE applicants SET invite_link = ?, updated_at = ? WHERE telegram_id = ?",
            (invite_link, utcnow(), telegram_id),
        )
        self._audit(telegram_id, "invite_link_issued")
        self.connection.commit()

    def mark_join_requested(self, telegram_id: int) -> Applicant | None:
        applicant = self.get(telegram_id)
        if applicant is None or applicant.status == "filling":
            return None
        now = utcnow()
        self.connection.execute(
            "UPDATE applicants SET status = 'join_requested', join_requested_at = ?, updated_at = ? WHERE telegram_id = ?",
            (now, now, telegram_id),
        )
        self._audit(telegram_id, "join_requested")
        self.connection.commit()
        return self.get(telegram_id)

    def mark_approved(self, telegram_id: int, approved_by: int) -> Applicant | None:
        if self.get(telegram_id) is None:
            return None
        now = utcnow()
        self.connection.execute(
            "UPDATE applicants SET status = 'approved', approved_at = ?, updated_at = ? WHERE telegram_id = ?",
            (now, now, telegram_id),
        )
        self._audit(telegram_id, "approved", f"admin={approved_by}")
        self.connection.commit()
        return self.get(telegram_id)

    def mark_rejected(self, telegram_id: int, rejected_by: int) -> Applicant | None:
        if self.get(telegram_id) is None:
            return None
        self.connection.execute(
            "UPDATE applicants SET status = 'rejected', updated_at = ? WHERE telegram_id = ?",
            (utcnow(), telegram_id),
        )
        self._audit(telegram_id, "rejected", f"admin={rejected_by}")
        self.connection.commit()
        return self.get(telegram_id)

    def mark_member(self, telegram_id: int, is_member: bool) -> Applicant | None:
        if self.get(telegram_id) is None:
            return None
        now = utcnow()
        status, column = ("joined", "joined_at") if is_member else ("left", "left_at")
        self.connection.execute(
            f"UPDATE applicants SET status = ?, {column} = ?, updated_at = ? WHERE telegram_id = ?",
            (status, now, now, telegram_id),
        )
        self._audit(telegram_id, status)
        self.connection.commit()
        return self.get(telegram_id)

    def get(self, telegram_id: int) -> Applicant | None:
        row = self.connection.execute("SELECT * FROM applicants WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return self._to_applicant(row) if row else None

    def all_applicants(self) -> list[Applicant]:
        rows = self.connection.execute("SELECT * FROM applicants ORDER BY created_at DESC").fetchall()
        return [self._to_applicant(row) for row in rows]

    def counts(self) -> dict[str, int]:
        rows = self.connection.execute("SELECT status, COUNT(*) AS count FROM applicants GROUP BY status").fetchall()
        return {row["status"]: row["count"] for row in rows}

    @staticmethod
    def _to_applicant(row: sqlite3.Row) -> Applicant:
        return Applicant(**dict(row))
