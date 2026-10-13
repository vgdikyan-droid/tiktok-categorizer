"""Read the Favorites (saved videos) tab of your own TikTok account.

TikTok has no public API for a user's saved videos, and phone apps cannot read
each other's data. The website shows the same Favorites list as the app, so we
open tiktok.com in a real browser window, let you log in yourself, then scroll
your Favorites tab and record each video.

Two sources are combined so a TikTok site change is less likely to break a run:
the JSON the page loads for the Favorites grid (captions included), and the
video links visible in the grid itself (captions read from thumbnail alt text).

Big collections (thousands of saves) used to exhaust the browser tab's memory,
because every video scrolled past stays on the page. So once the page has
loaded its first batch, we ask TikTok for the following batches directly from
the same tab, the request the page itself makes when you scroll, and nothing
piles up. If TikTok won't answer that way we scroll as before, skipping video
previews. Either way we reopen the tab if it crashes and report progress so it
can be saved as we go.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from tiktok_categorizer.models import SavedVideo, video_id_from_url

TIKTOK_HOME = "https://www.tiktok.com/"
DEFAULT_PROFILE_DIR = Path.home() / ".tiktok-categorizer" / "browser-profile"

# Cookies TikTok sets once you are logged in.
_SESSION_COOKIES = {"sessionid", "sessionid_ss", "sid_tt"}
_ALT_RE = re.compile(r"^(?P<caption>.*?)\s*created by (?P<creator>.+?) with .*$", re.S)
_CANONICAL_RE = re.compile(r"^/@(?P<user>[^/]+)/(?P<kind>video|photo)/(?P<id>\d{8,25})")

# Every grid link on the page, without marking anything (for the own-posts snapshot).
_ALL_GRID_LINKS_JS = """
() => Array.from(document.querySelectorAll('a[href*="/video/"], a[href*="/photo/"]'), a => a.href)
"""
# Returns grid links not read before (with thumbnail alt text), in page order,
# and marks them so later rounds only look at new tiles. A tile whose alt text
# hasn't appeared yet is retried a few times before being taken as it is.
_NEW_GRID_LINKS_JS = """
() => {
  const out = [];
  for (const a of document.querySelectorAll('a[href*="/video/"], a[href*="/photo/"]')) {
    if (a.dataset.tcSeen === a.href) continue;
    const img = a.querySelector('img[alt]');
    const alt = img ? img.alt : '';
    const tries = Number(a.dataset.tcTries || 0) + 1;
    if (alt || tries >= 3) { a.dataset.tcSeen = a.href; } else { a.dataset.tcTries = String(tries); }
    if (alt || tries >= 3) out.push({href: a.href, alt: alt});
  }
  return out;
}
"""
# Asks TikTok for one more page of your Favorites from inside your logged-in tab:
# the same request the page itself makes when you scroll down. On failure it
# returns what went wrong ({__status: ...} or {__error: ...}).
_FETCH_PAGE_JS = """
async (url) => {
  try {
    const response = await fetch(url, {credentials: 'include'});
    const text = await response.text();
    if (!response.ok) return {__status: response.status};
    try { return JSON.parse(text); } catch (e) { return {__status: response.status, __unreadable: true}; }
  } catch (e) {
    return {__error: String((e && e.message) || e)};
  }
}
"""
# Used only when scrolling: makes the page *show* just a few videos from each
# batch TikTok sends (the app still records every video from the network
# reply), so the page grows ~5x slower and doesn't run out of memory. The first
# few batches are shown in full so the page is tall enough to keep scrolling.
# Safe to run more than once.
_SHOW_FEWER_JS = """
() => {
  if (window.__tcShowFewer) return;
  window.__tcShowFewer = true;
  const KEEP = 6, MIN_TILES = 60;
  const isList = (u) => {
    try {
      const path = new URL(String(u), location.href).pathname;
      return path.includes('/api/') && path.includes('collect') && path.includes('item_list');
    } catch (e) { return false; }
  };
  const pageIsLong = () =>
    document.querySelectorAll('a[href*="/video/"], a[href*="/photo/"]').length >= MIN_TILES;
  const fewer = (data) => {
    if (data && Array.isArray(data.itemList) && data.itemList.length > KEEP) {
      return Object.assign({}, data, {itemList: data.itemList.slice(0, KEEP)});
    }
    return data;
  };
  const fewerText = (text) => {
    try { const data = JSON.parse(text); const less = fewer(data); return less === data ? text : JSON.stringify(less); }
    catch (e) { return text; }
  };

  const pageFetch = window.fetch;
  window.fetch = async function (input) {
    const response = await pageFetch.apply(this, arguments);
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    if (!isList(url) || !pageIsLong()) return response;
    try {
      const text = fewerText(await response.clone().text());
      const headers = new Headers(response.headers);
      headers.delete('content-length');
      headers.delete('content-encoding');
      const shorter = new Response(text, {status: response.status, statusText: response.statusText, headers});
      Object.defineProperty(shorter, 'url', {value: response.url});
      return shorter;
    } catch (e) {
      return response;
    }
  };

  const proto = XMLHttpRequest.prototype;
  const open = proto.open;
  proto.open = function (method, url) {
    this.__tcList = isList(url);
    this.__tcShown = undefined;
    return open.apply(this, arguments);
  };
  for (const name of ['responseText', 'response']) {
    const original = Object.getOwnPropertyDescriptor(proto, name);
    if (!original || !original.get) continue;
    Object.defineProperty(proto, name, {
      configurable: true,
      enumerable: original.enumerable,
      get() {
        const value = original.get.call(this);
        if (!this.__tcList || this.readyState !== 4) return value;
        if (this.__tcShown === undefined) this.__tcShown = pageIsLong();
        if (!this.__tcShown) return value;
        return typeof value === 'string' ? fewerText(value) : fewer(value);
      },
    });
  }
}
"""
_SCROLL_JS = """
() => {
  const links = document.querySelectorAll('a[href*="/video/"], a[href*="/photo/"]');
  if (links.length) links[links.length - 1].scrollIntoView({block: 'end'});
  window.scrollTo(0, document.body.scrollHeight);
}
"""
# Already at the bottom, scrolling down again changes nothing, so the page never
# asks for more. Backing up first makes it notice the next scroll. Pages that
# start loading well before the bottom need a bigger back-up, so it is given
# in screen heights (at least 600px).
_BACK_UP_JS = """
(screens) => {
  const links = document.querySelectorAll('a[href*="/video/"], a[href*="/photo/"]');
  if (links.length > 3) links[links.length - 4].scrollIntoView({block: 'end'});
  window.scrollBy(0, -Math.max(600, screens * window.innerHeight));
}
"""
# Video files (hover previews): not needed to read captions, and heavy on a long
# scroll. Thumbnails still load, because tiles may be sized by their images and
# the page only loads more videos as you reach the bottom. Scripts, data and
# puzzle images use other addresses.
_HEAVY_MEDIA_URLS = [
    "*/video/tos/*",
    "*/aweme/v1/play/*",
    "*mime_type=video*",
]
# The same in the newer URLPattern form, for Chrome versions that want it
# (by path only: a "?" in these patterns means "optional", not the query).
_HEAVY_MEDIA_URL_PATTERNS = [
    "*://*/video/tos/*",
    "*://*/aweme/v1/play/*",
]

Log = Callable[[str], None]
Progress = Callable[[list[SavedVideo]], None]


class CollectorError(RuntimeError):
    """A problem the person running the tool needs to fix or retry."""


class CollectionStopped(CollectorError):
    """Collecting stopped early; `videos` holds everything found until then."""

    def __init__(self, message: str, videos: list[SavedVideo]):
        super().__init__(message)
        self.videos = videos


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


_SIGNATURE_PARAMS = {"X-Bogus", "X-Gnarly", "_signature"}


def next_page_urls(template: str, cursor: Any) -> list[str]:
    """Ways to ask for the Favorites page at `cursor`, based on a request TikTok's
    own page made: without its old signature first (so the page can sign it
    afresh), then exactly as the page sent it apart from the cursor."""
    parts = urlsplit(template)
    query = parse_qsl(parts.query, keep_blank_values=True)

    def build(drop_signature: bool) -> str:
        params = [(k, str(cursor) if k == "cursor" else v) for k, v in query
                  if not (drop_signature and k in _SIGNATURE_PARAMS)]
        if not any(k == "cursor" for k, _ in params):
            params.append(("cursor", str(cursor)))
        return urlunsplit(parts._replace(query=urlencode(params)))

    return list(dict.fromkeys([build(True), build(False)]))


def _describe(answer: Any) -> str:
    """A short note on why a direct answer couldn't be used (shown when falling back)."""
    if not isinstance(answer, dict):
        return "no answer"
    if "__error" in answer:
        return "network error"
    if "__status" in answer:
        unreadable = ", unreadable reply" if answer.get("__unreadable") else ""
        return f"HTTP {answer['__status']}{unreadable}"
    code = answer.get("statusCode", answer.get("status_code"))
    if code not in (0, None):
        return f"TikTok code {code}"
    if not answer.get("itemList"):
        return "empty reply"
    return "no new videos in the reply"


def _page_cursor(payload: Any) -> Any:
    return payload.get("cursor") if isinstance(payload, dict) else None


def _is_last_page(payload: Any) -> bool:
    """An empty final page TikTok confirms as the end (statusCode 0, hasMore false)."""
    return (isinstance(payload, dict) and payload.get("statusCode", payload.get("status_code")) == 0
            and not payload.get("hasMore", True))


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


class _Collected:
    """Videos found so far, kept across tab restarts and saved every so often."""

    def __init__(self, known: list[SavedVideo], on_progress: Progress | None, save_every: int):
        self.found: dict[str, SavedVideo] = {}
        for video in known:
            self.add(video)
        self.known_keys = set(self.found)
        self.known = len(self.found)
        self._on_progress = on_progress
        self._save_every = save_every
        self._saved_at = len(self.found)

    def add(self, video: SavedVideo) -> bool:
        existing = self.found.get(video.key)
        if existing is None:
            self.found[video.key] = video
            return True
        # A later source may know more (the JSON has full captions and post dates).
        if not existing.caption and video.caption:
            existing.caption = video.caption
        if not existing.creator and video.creator:
            existing.creator = video.creator
        if not existing.posted_at and video.posted_at:
            existing.posted_at = video.posted_at
        return False

    def videos(self) -> list[SavedVideo]:
        return list(self.found.values())

    def save(self, *, force: bool = False) -> None:
        if self._on_progress and (force or len(self.found) - self._saved_at >= self._save_every):
            self._on_progress(self.videos())
            self._saved_at = len(self.found)


def _skip_heavy_media(page: Any, log: Log) -> Any:
    """Stop this tab loading video files. Returns the session doing it."""
    from playwright.sync_api import Error as PlaywrightError

    try:
        session = page.context.new_cdp_session(page)
        session.send("Network.enable")
        try:
            session.send("Network.setBlockedURLs", {"urls": _HEAVY_MEDIA_URLS})
        except PlaywrightError:
            session.send("Network.setBlockedURLs", {"urlPatterns": [
                {"urlPattern": pattern, "block": True} for pattern in _HEAVY_MEDIA_URL_PATTERNS]})
        return session
    except PlaywrightError:
        log("  (couldn't switch off video previews in this browser; carrying on with them)")
        return None


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


def _page_directly(
    fetch: Callable[[str], Any],
    template: str,
    cursor: Any,
    collected: _Collected,
    seen_here: set[str],
    *,
    limit: int | None,
    pause: float,
    wait: Callable[[float], None],
    stop_check: Callable[[], None],
    log: Log,
) -> bool | str:
    """Fetch the rest of the Favorites a page at a time, without scrolling.

    Returns True at the end of the list (or the limit); otherwise TikTok doesn't
    answer this way and it returns a short reason, so scrolling should carry on
    instead. A request form is
    trusted only after it has returned real videos: refusals can look like an
    empty last page, and must never end the run early.
    """
    variant: int | None = None  # which form of the request works, once proven
    while True:
        urls = next_page_urls(template, cursor)
        payload, videos, has_more, reason = None, [], True, "no answer"
        if variant is None:
            tries = [(index, 0.0) for index in range(len(urls))]
        else:
            tries = [(variant, 0.0), (variant, 2.0), (variant, 5.0)]  # ride out a hiccup
        for index, delay in tries:
            stop_check()
            if delay:
                wait(delay)
            answer = fetch(urls[index])
            reason = _describe(answer)
            found, more = videos_from_api_payload(answer)
            if found and any(v.key not in seen_here for v in found):
                payload, videos, has_more, variant = answer, found, more, index
                break
            if (not found and variant is not None and _is_last_page(answer)
                    and str(_page_cursor(answer)) not in ("0", "", "None")):
                payload, has_more = answer, False
                break
        if payload is None:
            return reason
        before = len(collected.found)
        for video in videos:
            seen_here.add(video.key)
            collected.add(video)
        collected.save()
        if len(collected.found) > before:
            log(f"  found {len(collected.found)} saved videos so far...")
        if not has_more or (limit and len(collected.found) >= limit):
            return True
        cursor = _page_cursor(payload)
        if cursor is None:
            return "no position for the next batch"
        wait(pause)


def _scroll_favorites(
    page: Any,
    username: str,
    collected: _Collected,
    *,
    base_url: str,
    limit: int | None,
    scroll_pause: float,
    idle_rounds: int,
    ask: Callable[[str], str],
    stop_check: Callable[[], None],
    log: Log,
) -> None:
    """Scroll one tab's Favorites to the end, adding everything to `collected`."""
    pending: list[Any] = []
    # The app's own direct requests (see _page_directly) must not count as what
    # the page has shown, or scrolling would think it is further along than it is.
    own_requests: set[str] = set()
    fetching = [False]

    def on_response(response: Any) -> None:
        if (is_saved_list_response(response.url) and not fetching[0]
                and response.request.url not in own_requests):
            pending.append(response)

    def fetch(url: str) -> Any:
        own_requests.add(url)
        fetching[0] = True
        try:
            return page.evaluate(_FETCH_PAGE_JS, url)
        finally:
            fetching[0] = False

    page.on("response", on_response)
    blocker = _skip_heavy_media(page, log)  # kept in a variable so it stays active while scrolling

    page.goto(urljoin(base_url, f"@{username}"), wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    # Links on the profile before switching tabs are your own posts, not saves.
    own_posts = set(page.evaluate(_ALL_GRID_LINKS_JS))

    if not _open_favorites_tab(page):
        ask(
            "Couldn't find the Favorites tab automatically. In the browser window, "
            "click the Favorites tab (bookmark icon) on your profile, then press Enter here..."
        )
        stop_check()
    page.wait_for_timeout(2500)

    seen_here: set[str] = set()  # everything this tab has seen, including videos already known
    scrolled: set[str] = set()  # what the page itself has shown: how far scrolling has got
    has_more = True
    template, cursor, direct = None, None, True
    idle = rounds = 0
    while True:
        stop_check()
        rounds += 1
        before_scrolled, before_found = len(scrolled), len(collected.found)
        while pending:
            response = pending.pop(0)
            try:
                payload = response.json()
            except Exception:  # an aborted or non-JSON response: the next page retries
                continue
            videos, has_more = videos_from_api_payload(payload)
            template, cursor = response.url, _page_cursor(payload)
            for video in videos:
                seen_here.add(video.key)
                scrolled.add(video.key)
                collected.add(video)
        if direct and template and has_more and cursor is not None:
            result = _page_directly(fetch, template, cursor, collected, seen_here, limit=limit,
                                    pause=scroll_pause / 2, log=log, stop_check=stop_check,
                                    wait=lambda seconds: page.wait_for_timeout(int(seconds * 1000)))
            if result is True:
                break
            direct = False
            log(f"  TikTok wants this done by scrolling ({result}); carrying on that way. To save "
                "memory, the browser will now show only a few videos from each batch; the app "
                "still records all of them.")
        if not direct:
            page.evaluate(_SHOW_FEWER_JS)  # again after any reload; it only installs once
        for link in page.evaluate(_NEW_GRID_LINKS_JS):
            if link["href"] in own_posts:
                continue
            video = video_from_grid_link(link["href"], link.get("alt", ""))
            if video:
                seen_here.add(video.key)
                scrolled.add(video.key)
                collected.add(video)
        collected.save()

        if limit and len(collected.found) >= limit:
            break
        moved = len(scrolled) > before_scrolled
        idle = 0 if moved else idle + 1
        if not has_more and idle >= 2:
            break  # TikTok said this was the last page
        if idle >= idle_rounds:
            passed = len(collected.known_keys & seen_here)
            if passed < collected.known:
                answer = ask(
                    f"The page stopped loading after passing {passed} of the {collected.known} videos "
                    "found last time. If the browser is showing a puzzle or stopped loading, sort it out "
                    "and type 'more' to keep going; otherwise press Enter to stop for now (nothing is "
                    "lost, and the next run carries on): "
                )
                stop_check()
                if answer.strip().lower() not in ("more", "m"):
                    raise CollectionStopped("Stopped before getting past the videos found last time.",
                                            collected.videos())
            else:
                answer = ask(
                    f"No new videos for a while ({len(collected.found)} found). That's usually the end "
                    "of your Favorites. If the browser is showing a puzzle or stopped loading, sort it "
                    "out and type 'more' to keep going; otherwise press Enter to finish: "
                )
                stop_check()
                if answer.strip().lower() not in ("more", "m"):
                    break
            idle = 0
        if len(collected.found) > before_found:
            log(f"  found {len(collected.found)} saved videos so far...")
        elif moved and rounds % 10 == 0:
            log(f"  catching up: passed {len(scrolled)} of the {len(collected.found)} videos found earlier...")
        if not moved:
            # While showing fewer videos the page grows only a row or two per
            # batch, so a page that loads early can stay "at the bottom": back
            # up further each quiet round (3, 6, 9... screens) to wake it up.
            screens = 0 if direct else 3 * min(idle, 4)
            page.evaluate(_BACK_UP_JS, screens)
            page.wait_for_timeout(500 if screens else 300)
        page.evaluate(_SCROLL_JS)
        page.wait_for_timeout(int(scroll_pause * 1000))


def collect_favorites(
    page: Any,
    username: str,
    *,
    base_url: str = TIKTOK_HOME,
    limit: int | None = None,
    scroll_pause: float = 1.5,
    idle_rounds: int = 6,
    known: list[SavedVideo] | None = None,
    on_progress: Progress | None = None,
    save_every: int = 100,
    max_restarts: int = 3,
    should_stop: Callable[[], bool] = lambda: False,
    ask: Callable[[str], str] = input,
    log: Log = print,
) -> list[SavedVideo]:
    """Scroll the Favorites tab of `username` until no new videos appear.

    `known` are videos found by an earlier, unfinished run; they are kept and
    scrolled past. `on_progress` receives the full list every `save_every` new
    videos. If the tab crashes, a new one picks up where it left off, up to
    `max_restarts` times. `should_stop` is checked between steps (for a clean
    Control-C). If collecting stops early for any reason, the CollectionStopped
    error carries everything found until then.
    """
    from playwright.sync_api import Error as PlaywrightError

    collected = _Collected(known or [], on_progress, save_every)

    def stop_check() -> None:
        if should_stop():
            raise CollectionStopped("Stopped.", collected.videos())

    if collected.known:
        log(f"Continuing from last time: {collected.known} videos already found. "
            "Scrolling past those first...")
    restarts = 0
    crashed_at = -1
    try:
        while True:
            crashes: list[Any] = []
            page.on("crash", lambda _page: crashes.append(True))
            try:
                _scroll_favorites(page, username, collected, base_url=base_url, limit=limit,
                                  scroll_pause=scroll_pause, idle_rounds=idle_rounds, ask=ask,
                                  stop_check=stop_check, log=log)
                break
            except PlaywrightError as exc:
                if not crashes and "crash" not in str(exc).lower():
                    raise
                collected.save(force=True)
                if restarts >= max_restarts or len(collected.found) == crashed_at:
                    raise CollectionStopped(
                        f"TikTok's page kept crashing (after {len(collected.found)} videos).",
                        collected.videos(),
                    ) from exc
                restarts += 1
                crashed_at = len(collected.found)
                log(f"The TikTok tab crashed after {len(collected.found)} videos. "
                    f"Reopening it and carrying on (attempt {restarts} of {max_restarts})...")
                # Open the new tab before closing the crashed one: closing a
                # browser's last tab can quit the browser.
                crashed_page, page = page, page.context.new_page()
                try:
                    crashed_page.close()
                except PlaywrightError:
                    pass
    except KeyboardInterrupt:
        raise CollectionStopped("Stopped.", collected.videos()) from None
    except PlaywrightError as exc:
        raise CollectionStopped(
            f"The browser closed or stopped responding ({exc}).", collected.videos()) from exc

    videos = collected.videos()
    return videos[:limit] if limit else videos


def is_logged_in(context: Any) -> bool:
    return any(
        cookie.get("name") in _SESSION_COOKIES and cookie.get("value")
        for cookie in context.cookies(TIKTOK_HOME)
    )


def wait_for_login(
    context: Any,
    page: Any,
    *,
    timeout_s: float = 600,
    poll_s: float = 2,
    log: Log = print,
    should_stop: Callable[[], bool] = lambda: False,
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
        if should_stop():
            raise CollectorError("Stopped.")
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
def _helper_ignores_control_c() -> Iterator[None]:
    """Start Playwright's helper program in its own session (Mac/Linux).

    Control-C in Terminal goes to every program started from it. Without this,
    the helper that drives Chrome would quit mid-step, and the app couldn't
    stop cleanly (save progress, write the spreadsheet, close Chrome).
    """
    if os.name != "posix":
        yield
        return
    original = asyncio.create_subprocess_exec

    async def in_own_session(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("start_new_session", True)
        return await original(*args, **kwargs)

    asyncio.create_subprocess_exec = in_own_session
    try:
        yield
    finally:
        asyncio.create_subprocess_exec = original


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

    with _helper_ignores_control_c():
        playwright = sync_playwright().start()
    interrupted = False
    try:
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
        except KeyboardInterrupt:
            interrupted = True
            raise
        finally:
            if not interrupted:  # after a hard stop, more browser calls can hang
                try:
                    context.close()
                except Exception:
                    pass  # already closed by the person running the tool
    finally:
        if not interrupted:
            try:
                playwright.stop()
            except Exception:
                pass
