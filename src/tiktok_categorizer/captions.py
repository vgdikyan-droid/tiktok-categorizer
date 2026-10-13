"""Look up captions TikTok's grid did not show, via TikTok's public oEmbed endpoint."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from tiktok_categorizer.models import SavedVideo

OEMBED_URL = "https://www.tiktok.com/oembed?url="
_USER_AGENT = "Mozilla/5.0 (compatible; tiktok-categorizer/0.1)"

HttpGet = Callable[[str], tuple[int, bytes]]


def urllib_get(url: str) -> tuple[int, bytes]:
    request = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, response.read(1_000_000)
    except HTTPError as exc:
        return exc.code, b""


def lookup_caption(
    video: SavedVideo,
    http_get: HttpGet = urllib_get,
    *,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Fill in caption and creator from oEmbed. Returns True when found."""
    for attempt in range(retries):
        try:
            status, body = http_get(OEMBED_URL + quote(video.url, safe=""))
        except (URLError, OSError):
            status, body = 0, b""
        if status == 200:
            try:
                data = json.loads(body)
            except ValueError:
                return False
            if not isinstance(data, dict) or "title" not in data:
                return False
            video.caption = str(data.get("title") or "").strip()
            author_url = str(data.get("author_url") or "")
            handle = author_url.rsplit("/@", 1)[-1] if "/@" in author_url else ""
            video.creator = video.creator or (f"@{handle}" if handle else str(data.get("author_name") or ""))
            return True
        if status not in (0, 429) and status < 500:
            return False  # deleted, private, or not a video TikTok will describe
        sleep(2**attempt)
    return False


def fill_missing_captions(
    videos: list[SavedVideo], http_get: HttpGet = urllib_get, *, workers: int = 4
) -> int:
    """Look up videos that have neither caption nor creator. Returns how many were filled."""
    missing = [v for v in videos if not v.caption and not v.creator]
    if not missing:
        return 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return sum(pool.map(lambda v: lookup_caption(v, http_get), missing))
