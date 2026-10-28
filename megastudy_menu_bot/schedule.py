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
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS schedule (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                channel_id INTEGER NOT NULL,
                clock TEXT NOT NULL,
                first_date TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                day TEXT PRIMARY KEY,
                state TEXT NOT NULL CHECK (state IN ('pending', 'sent')),
                message_id TEXT
            );
        """)

    def get(self) -> Schedule | None:
        row = self.connection.execute("SELECT channel_id, clock, first_date FROM schedule WHERE id = 1").fetchone()
        return Schedule(row[0], row[1], dt.date.fromisoformat(row[2])) if row else None

    def consumed(self, day: dt.date) -> bool:
        return self.connection.execute("SELECT 1 FROM deliveries WHERE day = ?", (day.isoformat(),)).fetchone() is not None

    def set(self, channel_id: int, clock: str, now: dt.datetime) -> Schedule:
        parse_time(clock)
        now = now.astimezone(KST)
        provisional = Schedule(channel_id, clock, now.date())
        first = next_run(provisional, now, self.consumed(now.date())).date()
        with self.connection:
            self.connection.execute(
                "INSERT INTO schedule VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "channel_id=excluded.channel_id, clock=excluded.clock, first_date=excluded.first_date",
                (channel_id, clock, first.isoformat()),
            )
        return Schedule(channel_id, clock, first)

    def disable(self) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM schedule WHERE id = 1")

    def claim(self, day: dt.date) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO deliveries(day, state) VALUES (?, 'pending')", (day.isoformat(),)
            )
        return cursor.rowcount == 1

    def release(self, day: dt.date) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM deliveries WHERE day = ? AND state = 'pending'", (day.isoformat(),))

    def mark_sent(self, day: dt.date, message_id: int) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE deliveries SET state = 'sent', message_id = ? WHERE day = ?",
                (str(message_id), day.isoformat()),
            )

    def close(self) -> None:
        self.connection.close()
