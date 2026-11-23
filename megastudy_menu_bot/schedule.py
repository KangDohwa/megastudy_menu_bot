from __future__ import annotations

import datetime as dt
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .menu import KST


def parse_time(value: str) -> dt.time:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("시간은 00:00~23:59 범위의 HH:mm 형식으로 입력해 주세요. 예: 11:30")
    return dt.time.fromisoformat(value)


@dataclass(frozen=True)
class Schedule:
    channel_id: int
    clock: str
    first_date: dt.date


def next_run(schedule: Schedule, now: dt.datetime, consumed_today: bool) -> dt.datetime:
    now = now.astimezone(KST)
    day = max(now.date(), schedule.first_date)
    while True:
        candidate = dt.datetime.combine(day, parse_time(schedule.clock), KST)
        if day.weekday() < 5 and candidate > now and not (day == now.date() and consumed_today):
            return candidate
        day += dt.timedelta(days=1)


def is_due(schedule: Schedule, now: dt.datetime) -> bool:
    now = now.astimezone(KST)
    if now.weekday() >= 5 or now.date() < schedule.first_date:
        return False
    target = dt.datetime.combine(now.date(), parse_time(schedule.clock), KST)
    return 0 <= (now - target).total_seconds() <= 600


class ScheduleStore:
    def __init__(self, path: Path, legacy_guild_id: int | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        try:
            self._initialize(legacy_guild_id)
        except Exception:
            self.connection.close()
            raise

    def _initialize(self, legacy_guild_id: int | None) -> None:
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute("""CREATE TABLE IF NOT EXISTS guild_schedules (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                clock TEXT NOT NULL,
                first_date TEXT NOT NULL
            )""")
            self.connection.execute("""CREATE TABLE IF NOT EXISTS guild_deliveries (
                guild_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('pending', 'sent')),
                message_id TEXT,
                PRIMARY KEY (guild_id, day)
            )""")
            # 옛 테이블에는 서버 ID가 없어서 이전 설정의 서버에만 기록을 옮긴다.
            for old, new, columns in (
                ("schedule", "guild_schedules", "channel_id, clock, first_date"),
                ("deliveries", "guild_deliveries", "day, state, message_id"),
            ):
                if not self.connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (old,)
                ).fetchone():
                    continue
                if legacy_guild_id is None and self.connection.execute(f"SELECT 1 FROM {old} LIMIT 1").fetchone():
                    raise ValueError("기존 예약과 발송 기록을 옮기려면 기존 DISCORD_GUILD_ID가 필요합니다.")
                self.connection.execute(
                    f"INSERT INTO {new} (guild_id, {columns}) SELECT ?, {columns} FROM {old}",
                    (legacy_guild_id,),
                )
                self.connection.execute(f"DROP TABLE {old}")

    def scheduled_guilds(self) -> list[int]:
        return [row[0] for row in self.connection.execute("SELECT guild_id FROM guild_schedules")]

    def get(self, guild_id: int) -> Schedule | None:
        row = self.connection.execute(
            "SELECT channel_id, clock, first_date FROM guild_schedules WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return Schedule(row[0], row[1], dt.date.fromisoformat(row[2])) if row else None

    def consumed(self, guild_id: int, day: dt.date) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM guild_deliveries WHERE guild_id = ? AND day = ?", (guild_id, day.isoformat())
        ).fetchone() is not None

    def record_view(self, guild_id: int, day: dt.date) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO guild_deliveries(guild_id, day, state) VALUES (?, ?, 'sent')",
                (guild_id, day.isoformat()),
            )

    def set(self, guild_id: int, channel_id: int, clock: str, now: dt.datetime) -> Schedule:
        parse_time(clock)
        now = now.astimezone(KST)
        provisional = Schedule(channel_id, clock, now.date())
        first = next_run(provisional, now, self.consumed(guild_id, now.date())).date()
        with self.connection:
            self.connection.execute(
                "INSERT INTO guild_schedules(guild_id, channel_id, clock, first_date) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET "
                "channel_id=excluded.channel_id, clock=excluded.clock, first_date=excluded.first_date",
                (guild_id, channel_id, clock, first.isoformat()),
            )
        return Schedule(channel_id, clock, first)

    def disable(self, guild_id: int) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM guild_schedules WHERE guild_id = ?", (guild_id,))

    def claim(self, guild_id: int, day: dt.date) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO guild_deliveries(guild_id, day, state) VALUES (?, ?, 'pending')",
                (guild_id, day.isoformat()),
            )
        return cursor.rowcount == 1

    def release(self, guild_id: int, day: dt.date) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM guild_deliveries WHERE guild_id = ? AND day = ? AND state = 'pending'",
                (guild_id, day.isoformat()),
            )

    def mark_sent(self, guild_id: int, day: dt.date, message_id: int) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE guild_deliveries SET state = 'sent', message_id = ? WHERE guild_id = ? AND day = ?",
                (str(message_id), guild_id, day.isoformat()),
            )

    def close(self) -> None:
        self.connection.close()
