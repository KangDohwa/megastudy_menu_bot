"""외부 발송 없이 날짜·예약 저장·표 경계 검출의 핵심 규칙을 확인한다."""

import datetime as dt
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from .menu import CropError, KST, menu_period, safe_url, self_boxes, week_start
from .schedule import ScheduleStore, is_due, parse_time


def main():
    sunday = dt.date(2026, 11, 1)
    assert week_start(sunday) == sunday
    assert week_start(sunday + dt.timedelta(days=6)) == sunday
    assert week_start(sunday + dt.timedelta(days=7)) == dt.date(2026, 11, 8)
    assert menu_period("2026.12.28~1.1 주간메뉴표") == (dt.date(2026, 12, 28), dt.date(2027, 1, 1))
    assert menu_period("2026.10.26~10.02") is None
    assert menu_period("2026.02.30~3.1") is None
    assert safe_url("https://postfiles.pstatic.net/menu.jpg")
    assert not safe_url("https://postfiles.pstatic.net.evil.example/menu.jpg")
    assert not safe_url("http://127.0.0.1/menu.jpg")
    for invalid in ("9:30", "24:00", "12:60", "-1:30", "11:30extra"):
        try:
            parse_time(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid clock accepted: {invalid}")

    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "bot.sqlite3"
        store = ScheduleStore(database)
        morning = dt.datetime(2026, 10, 30, 10, 0, tzinfo=KST)
        schedule = store.set(123, "11:30", morning)
        assert not is_due(schedule, morning)
        assert is_due(schedule, morning.replace(hour=11, minute=30))
        assert is_due(schedule, morning.replace(hour=11, minute=40))
        assert not is_due(schedule, morning.replace(hour=11, minute=40, second=1))
        assert not is_due(schedule, morning + dt.timedelta(days=1, hours=1, minutes=30))
        assert store.claim(morning.date()) and not store.claim(morning.date())
        store.mark_sent(morning.date(), 456)
        changed = store.set(789, "12:00", morning)
        assert changed.channel_id == 789 and changed.first_date == dt.date(2026, 11, 2)
        store.disable()
        assert store.get() is None and store.consumed(morning.date())
        store.close()
        store = ScheduleStore(database)
        assert store.consumed(morning.date())
        past = store.set(123, "09:00", morning)
        assert past.first_date == dt.date(2026, 11, 2)
        store.close()
        store = ScheduleStore(database)
        assert store.get() == past
        store.close()

    with Image.new("RGB", (1000, 750), "white") as image:
        draw = ImageDraw.Draw(image)
        draw.rectangle((25, 110, 980, 145), fill="#a77b66")
        draw.rectangle((170, 165, 980, 195), fill="#a77b66")
        for x in (25, 60, 170, 332, 494, 656, 818, 980):
            draw.line((x, 165, x, 600), fill="black", width=2)
        for y in (110, 145, 165, 195, 345, 420, 510, 600):
            draw.line((25, y, 980, y), fill="black", width=2)
        boxes = self_boxes(image)
        assert len(boxes) == 5 and boxes[0][0] == 171 and boxes[-1][2] == 982
        assert 164 <= boxes[0][1] <= 167 and 344 <= boxes[0][3] <= 348
    with Image.new("RGB", (1000, 750), "white") as blank:
        try:
            self_boxes(blank)
        except CropError:
            pass
        else:
            raise AssertionError("unrecognized table must fall back to original")
    print("핵심 확인 통과: 일~토 주차, 날짜 범위, 시간 검증, 재설정·재시작·중복 방지, 10분 지연, Self 경계와 원본 대체")


if __name__ == "__main__":
    main()
