from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image, UnidentifiedImageError


log = logging.getLogger(__name__)
KST = dt.timezone(dt.timedelta(hours=9), "KST")
BLOG = "https://blog.naver.com/megafs01"
IMAGE_HOSTS = {"postfiles.pstatic.net", "blogfiles.pstatic.net", "blogthumb.pstatic.net"}
HOSTS = IMAGE_HOSTS | {"blog.naver.com", "m.blog.naver.com", "rss.blog.naver.com"}
MAX_BYTES = 8 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 16_000_000
PERIOD = re.compile(
    r"(20\d{2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{1,2})\s*"
    r"[~～–-]\s*(?:(20\d{2})\s*[./-]\s*)?(\d{1,2})\s*[./-]\s*(\d{1,2})"
)


class MenuError(RuntimeError):
    pass


class CropError(MenuError):
    pass


def week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=(day.weekday() + 1) % 7)


def menu_period(title: str) -> tuple[dt.date, dt.date] | None:
    match = PERIOD.search(title)
    if not match:
        return None
    year, month, day, end_year, end_month, end_day = match.groups()
    try:
        start = dt.date(int(year), int(month), int(day))
        finish_year = int(end_year or year)
        if not end_year and int(end_month) < int(month):
            finish_year += 1
        end = dt.date(finish_year, int(end_month), int(end_day))
    except ValueError:
        return None
    if not 0 <= (end - start).days <= 6 or week_start(start) != week_start(end):
        return None
    return start, end


def safe_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        return (
            parsed.scheme == "https" and parsed.hostname in HOSTS
            and parsed.port in (None, 443) and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not safe_url(newurl):
            raise MenuError("메뉴 출처가 아닌 주소로 이동하는 요청을 차단했습니다.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, limit: int = MAX_BYTES) -> bytes:
    if not safe_url(url):
        raise MenuError("메뉴 출처 주소가 올바르지 않습니다.")
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36",
        "Referer": "https://m.blog.naver.com/megafs01",
        "Accept-Language": "ko-KR,ko;q=0.9",
    })
    try:
        with urllib.request.build_opener(SafeRedirect()).open(request, timeout=15) as response:
            if int(response.headers.get("Content-Length", "0")) > limit:
                raise MenuError("메뉴 자료가 허용 크기를 넘었습니다.")
            content = response.read(limit + 1)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise MenuError("네이버 메뉴 자료를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.") from exc
    if len(content) > limit:
        raise MenuError("메뉴 자료가 허용 크기를 넘었습니다.")
    return content


class ContentImage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.url: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag != "img" or self.url:
            return
        attributes = dict(attrs)
        for name in ("data-lazy-src", "data-original", "data-src", "src"):
            candidate = attributes.get(name)
            if candidate and safe_url(candidate) and urllib.parse.urlsplit(candidate).hostname in IMAGE_HOSTS:
                self.url = candidate.split("?", 1)[0] + "?type=w2000"
                return


def find_post(week: dt.date) -> tuple[str, str] | None:
    posts = []
    try:
        root = ET.fromstring(download("https://rss.blog.naver.com/megafs01.xml", 1024 * 1024))
        posts = [(item.findtext("title", ""), item.findtext("guid", "")) for item in root.findall("./channel/item")]
    except (MenuError, ET.ParseError):
        log.warning("RSS 조회 실패, 메뉴 카테고리 목록으로 재시도합니다.")
    found = select_post(posts, week)
    if found:
        return found
    # RSS에는 다른 지점 소식도 섞이므로, 누락된 주는 메뉴 카테고리에서 찾는다.
    url = "https://m.blog.naver.com/api/blogs/megafs01/post-list?categoryNo=41&itemCount=10"
    try:
        payload = json.loads(download(url, 2 * 1024 * 1024))
        if payload.get("isSuccess") is not True:
            raise ValueError("unsuccessful post list")
        items = payload["result"]["items"]
        if not isinstance(items, list):
            raise ValueError("invalid post list")
        posts = [
            (str(item["titleWithInspectMessage"]), f"{BLOG}/{item['logNo']}")
            for item in items if isinstance(item, dict)
        ]
    except (ValueError, TypeError, KeyError) as exc:
        raise MenuError("블로그 게시물 목록을 해석하지 못했습니다.") from exc
    return select_post(posts, week)


def select_post(posts: list[tuple[str, str]], week: dt.date) -> tuple[str, str] | None:
    for title, url in posts:
        if "메가스터디 구내식당" not in title or "주간메뉴표" not in title:
            continue
        period = menu_period(title)
        if period and week_start(period[0]) == week and re.fullmatch(re.escape(BLOG) + r"/\d+", url):
            return title, url
    return None


def position_runs(positions: list[int]) -> list[tuple[int, int]]:
    groups: list[tuple[int, int]] = []
    for position in positions:
        if not groups or position > groups[-1][1] + 2:
            groups.append((position, position))
        else:
            groups[-1] = groups[-1][0], position
    return groups


def self_boxes(image: Image.Image) -> list[tuple[int, int, int, int]]:
    width, height = image.size
    if width < 600 or height < 400:
        raise CropError("메뉴표 해상도가 너무 낮습니다.")
    # shortcut: 현재 표의 Self 행 위치를 탐색 기준으로 삼는다. 양식이 바뀌면 원본으로 대체하고 기준을 갱신한다.
    band_top, band_bottom = round(height * 0.30), round(height * 0.44)
    with image.convert("L") as gray, gray.point([0] * 160 + [255] * 96) as binary:
        vertical = []
        for x in range(width):
            with binary.crop((x, band_top, x + 1, band_bottom)) as strip:
                if strip.histogram()[0] >= (band_bottom - band_top) * 0.93:
                    vertical.append(x)
        columns = [(start + end + 1) // 2 for start, end in position_runs(vertical)][-6:]
        if len(columns) != 6:
            raise CropError("요일 열 경계를 확인하지 못했습니다.")
        average = (columns[-1] - columns[0]) / 5
        if average < width * 0.10 or any(abs(b - a - average) > average * 0.06 for a, b in zip(columns, columns[1:])):
            raise CropError("요일 열 간격이 예상과 다릅니다.")
        horizontal = []
        for y in range(round(height * 0.10), round(height * 0.62)):
            with binary.crop((columns[0], y, columns[-1], y + 1)) as strip:
                if strip.histogram()[0] >= (columns[-1] - columns[0]) * 0.55:
                    horizontal.append(y)
    rules = []
    for start, end in position_runs(horizontal):
        # 색칠된 날짜 행은 두꺼운 띠로 잡히므로, 띠의 양 끝을 경계로 사용한다.
        rules.extend((start, end) if end - start > height * 0.01 else ((start + end + 1) // 2,))
    above = [y for y in rules if y < band_top]
    below = [y for y in rules if y > band_bottom]
    if len(above) < 2 or not below:
        raise CropError("날짜 행과 Self 행의 경계를 확인하지 못했습니다.")
    top, header_bottom, bottom = above[-2], above[-1], below[0]
    if not height * 0.015 <= header_bottom - top <= height * 0.08 or not height * 0.12 <= bottom - header_bottom <= height * 0.35:
        raise CropError("Self 행의 높이가 예상과 다릅니다.")
    return [(left, top, right + 1, bottom + 1) for left, right in zip(columns, columns[1:])]


def atomic_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


@dataclass(frozen=True)
class WeeklyMenu:
    title: str
    url: str
    full: Path
    days: tuple[Path, ...] | None


class MenuSource:
    def __init__(self, directory: Path):
        self.directory = directory
        self.cached: dict[dt.date, tuple[float, WeeklyMenu | None]] = {}

    def get_week(self, week: dt.date, *, refresh: bool = False) -> WeeklyMenu | None:
        cached = self.cached.get(week)
        if not refresh and cached and time.monotonic() - cached[0] < 900:
            return cached[1]
        post = find_post(week)
        if post is None:
            self.cached[week] = time.monotonic(), None
            return None
        title, url = post
        parser = ContentImage()
        post_number = url.rsplit("/", 1)[1]
        body_url = f"https://blog.naver.com/PostView.naver?blogId=megafs01&logNo={post_number}"
        parser.feed(download(body_url, 2 * 1024 * 1024).decode("utf-8"))
        if not parser.url:
            raise MenuError("주간메뉴표 게시물에서 이미지를 찾지 못했습니다.")
        digest = hashlib.sha256(parser.url.encode()).hexdigest()[:16]
        folder = self.directory / week.isoformat()
        folder.mkdir(parents=True, exist_ok=True)
        existing = list(folder.glob(f"full-{digest}.*"))
        existing = [path for path in existing if path.suffix in {".jpg", ".png", ".webp"}]
        content = existing[0].read_bytes() if existing else download(parser.url)
        try:
            with Image.open(io.BytesIO(content)) as image:
                if image.width * image.height > Image.MAX_IMAGE_PIXELS:
                    raise MenuError("메뉴표 해상도가 허용 범위를 넘었습니다.")
                extension = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(image.format)
                if extension is None or getattr(image, "is_animated", False):
                    raise MenuError("지원하지 않는 메뉴표 이미지 형식입니다.")
                image.load()
                full = folder / f"full-{digest}{extension}"
                if not full.exists():
                    atomic_bytes(full, content)
                days = tuple(folder / f"{digest}-{day}.png" for day in range(5))
                if not all(path.exists() for path in days):
                    try:
                        boxes = self_boxes(image)
                    except CropError:
                        log.warning("Self 크롭 경계 검출 실패: %s", url)
                        days = None
                    else:
                        day_paths = list(days)
                        for index, (path, box) in enumerate(zip(days, boxes)):
                            left, top, right, bottom = box
                            with image.crop((left + 2, bottom - 3, right - 2, bottom + 2)).convert("L") as border:
                                coverage = max(
                                    sum(border.crop((0, row, border.width, row + 1)).histogram()[:160]) / border.width
                                    for row in range(border.height)
                                )
                            # 휴무 그림처럼 행이 합쳐진 셀은 일부만 자르지 않고 주간 원본을 보여준다.
                            if coverage < 0.70:
                                day_paths[index] = full
                                continue
                            with image.crop(box) as cropped:
                                output = io.BytesIO()
                                display_width = max(440, cropped.width)
                                with cropped.resize((display_width, round(cropped.height * display_width / cropped.width)), Image.Resampling.LANCZOS) as display:
                                    display.save(output, format="PNG")
                                if output.tell() > MAX_BYTES:
                                    raise MenuError("요일별 메뉴 이미지가 허용 크기를 넘었습니다.")
                                atomic_bytes(path, output.getvalue())
                        days = tuple(day_paths)
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
            raise MenuError("메뉴표 이미지 파일을 읽지 못했습니다.") from exc
        result = WeeklyMenu(title, url, full, days)
        self.cached[week] = time.monotonic(), result
        self.cached = {key: value for key, value in self.cached.items() if abs((key - week).days) <= 14}
        return result
