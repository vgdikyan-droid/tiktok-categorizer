"""Read the Favorites (saved videos) tab of your own TikTok account.

TikTok has no public API for a user's saved videos, and phone apps cannot read
each other's data. The website shows the same Favorites list as the app, so we
open tiktok.com in a real browser window, let you log in yourself, then scroll
your Favorites tab and record each video.

Two sources are combined so a TikTok site change is less likely to break a run:
the JSON the page loads for the Favorites grid (captions included), and the
video links visible in the grid itself (captions read from thumbnail alt text).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

from tiktok_categorizer.models import SavedVideo, video_id_from_url

TIKTOK_HOME = "https://www.tiktok.com/"
DEFAULT_PROFILE_DIR = Path.home() / ".tiktok-categorizer" / "browser-profile"

# Cookies TikTok sets once you are logged in.
_SESSION_COOKIES = {"sessionid", "sessionid_ss", "sid_tt"}
_ALT_RE = re.compile(r"^(?P<caption>.*?)\s*created by (?P<creator>.+?) with .*$", re.S)
_CANONICAL_RE = re.compile(r"^/@(?P<user>[^/]+)/(?P<kind>video|photo)/(?P<id>\d{8,25})")

# Gathers grid links with their thumbnail alt text, in page order.
_DOM_LINKS_JS = """
() => Array.from(document.querySelectorAll('a[href*="/video/"], a[href*="/photo/"]'))
  .map(a => { const img = a.querySelector('img[alt]'); return {href: a.href, alt: img ? img.alt : ''}; })
"""
_SCROLL_JS = """
() => {
  const links = document.querySelectorAll('a[href*="/video/"], a[href*="/photo/"]');
  if (links.length) links[links.length - 1].scrollIntoView({block: 'end'});
  window.scrollTo(0, document.body.scrollHeight);
}
"""
# Already at the bottom, scrolling down again changes nothing, so the page never
# asks for more. Backing up a little first makes it notice the next scroll.
_BACK_UP_JS = """
() => {
  const links = document.querySelectorAll('a[href*="/video/"], a[href*="/photo/"]');
  if (links.length > 3) links[links.length - 4].scrollIntoView({block: 'end'});
  window.scrollBy(0, -600);
}
"""

Log = Callable[[str], None]


class CollectorError(RuntimeError):
    """A problem the person running the tool needs to fix or retry."""


def is_saved_list_response(url: str) -> bool:
    """True for the JSON request behind the Favorites grid (internally "collect")."""
    path = urlsplit(url).path
    return "/api/" in path and "collect" in path and "item_list" in path


def video_from_api_item(item: Any) -> SavedVideo | None:
    if not isinstance(item, dict):
        return None
    video_id = str(item.get("id") or "")
    if not video_id.isdigit():
        return None
    author = item.get("author")
    handle = author.get("uniqueId", "") if isinstance(author, dict) else str(author or "")
    kind = "photo" if item.get("imagePost") is not None else "video"
    posted_at = None
    try:
        created = int(item.get("createTime") or 0)
    except (TypeError, ValueError):
        created = 0
    if created > 0:
        posted_at = datetime.fromtimestamp(created, tz=timezone.utc).date().isoformat()
    return SavedVideo(
        url=f"https://www.tiktok.com/@{handle}/{kind}/{video_id}",
        caption=str(item.get("desc") or "").strip(),
        creator=f"@{handle}" if handle else "",
        posted_at=posted_at,
    )


def videos_from_api_payload(payload: Any) -> tuple[list[SavedVideo], bool]:
    """Return the videos in one page of the Favorites JSON and whether more pages exist."""
    if not isinstance(payload, dict):
        return [], True
    items = payload.get("itemList") or payload.get("items") or []
    videos = [v for v in (video_from_api_item(item) for item in items) if v]
    return videos, bool(payload.get("hasMore", True))


def video_from_grid_link(href: str, alt: str) -> SavedVideo | None:
    parts = urlsplit(href)
    match = _CANONICAL_RE.match(parts.path)
    if match:
        url = f"https://www.tiktok.com/@{match['user']}/{match['kind']}/{match['id']}"
        creator = f"@{match['user']}"
    elif video_id_from_url(parts.path):
        url = f"https://www.tiktok.com{parts.path}"
        creator = ""
    else:
        return None
    caption = alt.strip()
    alt_match = _ALT_RE.match(caption)
    if alt_match:
        caption = alt_match["caption"].strip()
        creator = creator or alt_match["creator"].strip()
    return SavedVideo(url=url, caption=caption, creator=creator)


def _add(found: dict[str, SavedVideo], video: SavedVideo) -> bool:
    existing = found.get(video.key)
    if existing is None:
        found[video.key] = video
        return True
    # A later source may know more (the JSON has full captions and post dates).
    if not existing.caption and video.caption:
        existing.caption = video.caption
    if not existing.creator and video.creator:
        existing.creator = video.creator
    if not existing.posted_at and video.posted_at:
        existing.posted_at = video.posted_at
    return False


def _grid_links(page: Any) -> list[dict[str, str]]:
    return page.evaluate(_DOM_LINKS_JS)


def _open_favorites_tab(page: Any) -> bool:
    from playwright.sync_api import Error as PlaywrightError

    name = re.compile(r"^\s*favou?rites\s*$", re.I)
    for locator in (
        page.locator('[data-e2e="favorites-tab"]'),
        page.get_by_role("tab", name=name),
        page.get_by_text(name),
    ):
        try:
            locator.first.click(timeout=4000)
            return True
        except PlaywrightError:
            continue
    return False


def collect_favorites(
    page: Any,
    username: str,
    *,
    base_url: str = TIKTOK_HOME,
    limit: int | None = None,
    scroll_pause: float = 1.5,
    idle_rounds: int = 6,
    ask: Callable[[str], str] = input,
    log: Log = print,
) -> list[SavedVideo]:
    """Scroll the Favorites tab of `username` until no new videos appear."""
    pending: list[Any] = []
    page.on("response", lambda r: pending.append(r) if is_saved_list_response(r.url) else None)

    page.goto(urljoin(base_url, f"@{username}"), wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    # Links on the profile before switching tabs are your own posts, not saves.
    own_posts = {link["href"] for link in _grid_links(page)}

    if not _open_favorites_tab(page):
        ask(
            "Couldn't find the Favorites tab automatically. In the browser window, "
            "click the Favorites tab (bookmark icon) on your profile, then press Enter here..."
        )
    page.wait_for_timeout(2500)

    found: dict[str, SavedVideo] = {}
    has_more = True
    idle = 0
    while True:
        new = 0
        while pending:
            response = pending.pop(0)
            try:
                payload = response.json()
            except Exception:  # an aborted or non-JSON response: the next page retries
                continue
            videos, has_more = videos_from_api_payload(payload)
            new += sum(_add(found, video) for video in videos)
        for link in _grid_links(page):
            if link["href"] in own_posts:
                continue
            video = video_from_grid_link(link["href"], link.get("alt", ""))
            if video:
                new += _add(found, video)

        if limit and len(found) >= limit:
            break
        idle = 0 if new else idle + 1
        if not has_more and idle >= 2:
            break  # TikTok said this was the last page
        if idle >= idle_rounds:
            answer = ask(
                f"No new videos for a while ({len(found)} found). That's usually the end of your "
                "Favorites. If the browser is showing a puzzle or stopped loading, sort it out and "
                "type 'more' to keep going; otherwise press Enter to finish: "
            )
            if answer.strip().lower() not in ("more", "m"):
                break
            idle = 0
        if new:
            log(f"  found {len(found)} saved videos so far...")
        else:
            page.evaluate(_BACK_UP_JS)
            page.wait_for_timeout(300)
        page.evaluate(_SCROLL_JS)
        page.wait_for_timeout(int(scroll_pause * 1000))

    videos = list(found.values())
    return videos[:limit] if limit else videos


def is_logged_in(context: Any) -> bool:
    return any(
        cookie.get("name") in _SESSION_COOKIES and cookie.get("value")
        for cookie in context.cookies(TIKTOK_HOME)
    )


def wait_for_login(
    context: Any, page: Any, *, timeout_s: float = 600, poll_s: float = 2, log: Log = print
) -> None:
    page.goto(TIKTOK_HOME, wait_until="domcontentloaded")
    if is_logged_in(context):
        return
    log(
        "Log in to TikTok in the browser window that just opened.\n"
        "Tip: choose 'Use QR code' and scan it with the TikTok app on your phone.\n"
        "Waiting for you to finish logging in..."
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if is_logged_in(context):
            log("Logged in.")
            return
        page.wait_for_timeout(int(poll_s * 1000))
    raise CollectorError("Timed out waiting for you to log in to TikTok. Run the command again.")


def detect_username(page: Any) -> str | None:
    from playwright.sync_api import Error as PlaywrightError

    try:
        href = page.locator('[data-e2e="nav-profile"]').first.get_attribute("href", timeout=5000)
    except PlaywrightError:
        return None
    match = re.search(r"/@([^/?#]+)", href or "")
    return match.group(1) if match else None


@contextmanager
def open_tiktok_browser(
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    *,
    browser: str = "chrome",
    executable_path: str | None = None,
    log: Log = print,
) -> Iterator[tuple[Any, Any]]:
    """Open a visible browser that remembers your TikTok login between runs."""
    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError as exc:
        raise CollectorError(
            "Playwright is not installed. Run: pip install playwright && playwright install chromium"
        ) from exc

    profile_dir.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": False,
        "viewport": {"width": 1280, "height": 900},
    }
    if executable_path:
        options["executable_path"] = executable_path
    elif browser == "chrome":
        options["channel"] = "chrome"

    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(**options)
        except PlaywrightError as exc:
            if options.pop("channel", None) is None:
                raise CollectorError(f"Could not start the browser: {exc}") from exc
            log("Google Chrome was not found; using Playwright's Chromium instead.")
            try:
                context = playwright.chromium.launch_persistent_context(**options)
            except PlaywrightError as exc2:
                raise CollectorError(
                    "Could not start a browser. Install Google Chrome, or run: "
                    "playwright install chromium"
                ) from exc2
        try:
            page = context.pages[0] if context.pages else context.new_page()
            yield context, page
        except PlaywrightError as exc:
            raise CollectorError(f"The browser closed or stopped responding ({exc}).") from exc
        finally:
            try:
                context.close()
            except PlaywrightError:
                pass  # already closed by the person running the tool
