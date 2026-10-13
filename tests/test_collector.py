import json
import os
from urllib.parse import parse_qs, urlsplit

from fnmatch import fnmatch

import pytest

from tiktok_categorizer.models import SavedVideo

from tiktok_categorizer.collector import (
    next_page_urls,
    _HEAVY_MEDIA_URLS,
    CollectionStopped,
    _skip_heavy_media,
    collect_favorites,
    is_saved_list_response,
    video_from_api_item,
    video_from_grid_link,
    videos_from_api_payload,
)


def test_api_item_becomes_a_saved_video():
    video = video_from_api_item({
        "id": "7301234567890123456",
        "desc": "fit check #ootd ",
        "author": {"uniqueId": "janestyle", "nickname": "Jane"},
        "createTime": 1700000000,
    })
    assert video.url == "https://www.tiktok.com/@janestyle/video/7301234567890123456"
    assert video.caption == "fit check #ootd"
    assert video.creator == "@janestyle"
    assert video.posted_at == "2023-11-14"


def test_photo_posts_link_to_the_photo_page():
    video = video_from_api_item({"id": "7301234567890123456", "author": {"uniqueId": "a"}, "imagePost": {}})
    assert video.url == "https://www.tiktok.com/@a/photo/7301234567890123456"


@pytest.mark.parametrize("item", [None, "x", {}, {"id": "abc"}])
def test_malformed_api_items_are_skipped(item):
    assert video_from_api_item(item) is None


def test_payload_reports_whether_more_pages_exist():
    videos, has_more = videos_from_api_payload({
        "itemList": [{"id": "7301234567890123456", "author": {"uniqueId": "a"}}, {"id": "bad"}],
        "hasMore": False,
    })
    assert [v.video_id for v in videos] == ["7301234567890123456"]
    assert has_more is False
    assert videos_from_api_payload("not a dict") == ([], True)


def test_only_the_favorites_request_is_captured():
    assert is_saved_list_response("https://www.tiktok.com/api/user/collect/item_list/?count=30&cursor=0")
    assert not is_saved_list_response("https://www.tiktok.com/api/post/item_list/?secUid=x")
    assert not is_saved_list_response("https://www.tiktok.com/api/favorite/item_list/?secUid=x")


def test_grid_link_reads_caption_from_thumbnail_alt_text():
    video = video_from_grid_link(
        "https://www.tiktok.com/@chef.ana/video/7301234567890123456?is_from_webapp=1",
        "5 minute poke bowl #recipe created by Ana Cooks with Ana's original sound",
    )
    assert video.url == "https://www.tiktok.com/@chef.ana/video/7301234567890123456"
    assert video.caption == "5 minute poke bowl #recipe"
    assert video.creator == "@chef.ana"
    assert video_from_grid_link("https://www.tiktok.com/@chef.ana", "") is None


# --- Browser test against a fake TikTok profile served locally -----------------

PROFILE_HTML = """<!doctype html><html><body style="margin:0">
<nav><a data-e2e="nav-profile" href="/@me">Profile</a></nav>
<div id="tabs"><p>Videos</p><p data-e2e="favorites-tab">Favorites</p></div>
<div id="grid">
  <a href="https://www.tiktok.com/@me/video/7000000000000000001"><img alt="my own post"></a>
</div>
<script>
let cursor = 0, hasMore = true, loading = false;
const API = '__API__';
async function loadMore() {
  if (loading || !hasMore) return;
  loading = true;
  const data = await (await fetch(API + '?count=3&cursor=' + cursor + '&X-Bogus=sig' + cursor)).json();
  for (const item of data.itemList) {
    const a = document.createElement('a');
    a.href = '/@' + item.author.uniqueId + '/video/' + item.id;
    a.style.cssText = 'display:block;height:400px';
    a.innerHTML = '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" alt="' + item.desc
      + ' created by ' + item.author.uniqueId + ' with a sound">';
    document.getElementById('grid').appendChild(a);
  }
  cursor = data.cursor; hasMore = data.hasMore; loading = false;
}
document.querySelector('[data-e2e=favorites-tab]').addEventListener('click', () => {
  document.getElementById('grid').innerHTML = '';
  loadMore();
  window.addEventListener('scroll', () => {
    if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 50) loadMore();
  });
});
</script></body></html>"""

CAPTIONS = ["fit check #ootd", "help fund the shelter #gofundme", "smooth #capcutedit", "lol"]


def _page_of(cursor: int, per_page: int = 3, total: int = 10) -> dict:
    ids = range(cursor, min(cursor + per_page, total))
    return {
        "itemList": [
            {"id": str(7100000000000000000 + i), "desc": f"{CAPTIONS[i % 4]} {i}",
             "author": {"uniqueId": f"creator{i}"}, "createTime": 1700000000}
            for i in ids
        ],
        "cursor": cursor + per_page,
        "hasMore": cursor + per_page < total,
        "statusCode": 0,
    }


def _unexpected_prompt(message):
    raise AssertionError(f"unexpected prompt: {message}")


@pytest.fixture
def browser_page():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(executable_path=os.environ.get("TIKTOK_TEST_CHROMIUM"))
        except sync_api.Error as exc:
            if os.environ.get("TIKTOK_REQUIRE_BROWSER_TESTS"):
                raise
            pytest.skip(f"no Chromium available: {exc}")
        context = browser.new_context(viewport={"width": 800, "height": 600})
        yield context.new_page()
        browser.close()


def _serve_fake_tiktok(page, api_path: str, total: int = 10, *, signed_only: bool = False,
                       refuse_direct_from: int | None = None, soft_block: bool = False,
                       flaky: tuple = ()) -> list:
    """Serve a fake profile. The page signs its own data requests (like TikTok's
    X-Bogus); requests the app makes itself are "direct". Knobs for how TikTok
    might treat direct requests: signed_only refuses them all with an empty body,
    refuse_direct_from refuses them from that cursor on with an error, soft_block
    answers them with an empty "end of list", and flaky fails the first direct
    request for those cursors. Returns the cursors of all data requests received.
    """
    requests = []
    failed_once = set()

    def handle(route):
        url = urlsplit(route.request.url)
        if url.path == "/@me":
            route.fulfill(content_type="text/html", body=PROFILE_HTML.replace("__API__", api_path))
        elif url.path == api_path:
            query = parse_qs(url.query)
            cursor = int(query.get("cursor", ["0"])[0])
            requests.append(cursor)
            direct = query.get("X-Bogus") != [f"sig{cursor}"]
            if direct and signed_only:
                route.fulfill(status=200, body="")  # TikTok's way of refusing
                return
            if direct and refuse_direct_from is not None and cursor >= refuse_direct_from:
                route.fulfill(content_type="application/json",
                              body=json.dumps({"statusCode": 10201, "status_msg": "verify"}))
                return
            if direct and soft_block:
                route.fulfill(content_type="application/json",
                              body=json.dumps({"cursor": "0", "hasMore": False, "statusCode": 0}))
                return
            if direct and cursor in flaky and cursor not in failed_once:
                failed_once.add(cursor)
                route.fulfill(status=500, body="")
                return
            route.fulfill(content_type="application/json", body=json.dumps(_page_of(cursor, total=total)))
        else:
            route.fulfill(status=404, body="")

    page.context.route("https://www.tiktok.com/**", handle)
    return requests


def test_collects_every_page_of_favorites_from_the_api(browser_page):
    _serve_fake_tiktok(browser_page, "/api/user/collect/item_list/")
    videos = collect_favorites(browser_page, "me", scroll_pause=0.3, idle_rounds=3,
                               ask=_unexpected_prompt, log=lambda _: None)

    assert len(videos) == 10
    assert videos[0].caption == "fit check #ootd 0"
    assert videos[0].creator == "@creator0"
    assert videos[0].posted_at == "2023-11-14"  # only the API knows post dates
    assert all("7000000000000000001" not in v.url for v in videos)  # own post excluded


def test_falls_back_to_the_visible_grid_when_the_api_is_unrecognised(browser_page):
    _serve_fake_tiktok(browser_page, "/api/some/renamed/endpoint/")
    prompts = []
    videos = collect_favorites(browser_page, "me", scroll_pause=0.3, idle_rounds=3,
                               ask=lambda m: prompts.append(m) or "", log=lambda _: None)

    assert len(videos) == 10
    # Without TikTok's "no more pages" signal, you confirm the end of the list.
    assert len(prompts) == 1 and "10 found" in prompts[0]
    assert videos[1].caption == "help fund the shelter #gofundme 1"
    assert videos[1].posted_at is None


def test_limit_stops_early(browser_page):
    _serve_fake_tiktok(browser_page, "/api/user/collect/item_list/")
    videos = collect_favorites(browser_page, "me", limit=4, scroll_pause=0.3, idle_rounds=3,
                               ask=_unexpected_prompt, log=lambda _: None)
    assert len(videos) == 4


def test_asks_you_to_click_favorites_when_the_tab_is_not_found(browser_page):
    _serve_fake_tiktok(browser_page, "/api/user/collect/item_list/", signed_only=True)
    # A redesigned (or non-English) profile page: the tab keeps working but is unrecognisable.
    browser_page.add_init_script(
        "document.addEventListener('DOMContentLoaded', () => {"
        "  const tab = document.querySelector('[data-e2e=favorites-tab]');"
        "  tab.removeAttribute('data-e2e'); tab.id = 'saved'; tab.textContent = 'Guardados';"
        "});"
    )
    prompts = []

    def ask(message):
        prompts.append(message)
        browser_page.click("#saved")  # what the person does in the browser window
        return ""

    videos = collect_favorites(browser_page, "me", scroll_pause=0.3, idle_rounds=3, ask=ask, log=lambda _: None)
    assert len(prompts) == 1
    assert len(videos) == 10


def test_a_stalled_page_can_be_resumed(browser_page):
    _serve_fake_tiktok(browser_page, "/api/user/collect/item_list/", signed_only=True)
    browser_page.add_init_script("window.__paused = true;")
    browser_page.add_init_script(
        "document.addEventListener('DOMContentLoaded', () => {"
        "  const original = loadMore;"
        "  loadMore = () => (window.__paused && cursor >= 6) ? undefined : original();"
        "});"
    )
    answers = []

    def ask(message):
        assert len(answers) < 3, "kept asking without loading more"
        # Like solving a puzzle in the browser: loading starts working again.
        browser_page.evaluate("window.__paused = false")
        answers.append("more")
        return "more"

    videos = collect_favorites(browser_page, "me", scroll_pause=0.3, idle_rounds=3, ask=ask, log=lambda _: None)
    assert answers == ["more"]
    assert len(videos) == 10


API = "/api/user/collect/item_list/"


def _crash(page) -> None:
    """Crash the tab the way running out of memory does."""
    try:
        page.goto("chrome://crash", timeout=5000)
    except Exception:
        pass


def _found(message: str) -> int:
    return int(message.split("found ")[1].split()[0]) if "so far" in message else 0


def test_progress_is_reported_as_it_goes(browser_page):
    _serve_fake_tiktok(browser_page, API, total=10)
    saves = []
    collect_favorites(browser_page, "me", scroll_pause=0.3, idle_rounds=3, save_every=3,
                      on_progress=lambda videos: saves.append(len(videos)),
                      ask=_unexpected_prompt, log=lambda _: None)
    assert len(saves) >= 2 and saves == sorted(saves) and saves[-1] >= 9


def test_a_crashed_tab_is_reopened_and_nothing_is_lost(browser_page):
    _serve_fake_tiktok(browser_page, API, total=30)
    crashed, saves, logs = [], [], []

    def log(message):
        logs.append(message)
        if not crashed and _found(message) >= 12:
            crashed.append(_found(message))
            _crash(browser_page)

    videos = collect_favorites(browser_page, "me", scroll_pause=0.3, idle_rounds=3,
                               on_progress=lambda v: saves.append(len(v)),
                               ask=_unexpected_prompt, log=log)
    assert crashed
    assert len({v.key for v in videos}) == len(videos) == 30
    assert crashed[0] in saves  # saved the moment the tab crashed
    assert any("crashed after" in m for m in logs)


def test_gives_up_after_repeated_crashes_but_keeps_what_it_found(browser_page):
    _serve_fake_tiktok(browser_page, API, total=60)

    def log(message):
        if _found(message):
            _crash(browser_page.context.pages[-1])

    with pytest.raises(CollectionStopped) as stopped:
        collect_favorites(browser_page, "me", scroll_pause=0.3, idle_rounds=3, max_restarts=2,
                          ask=_unexpected_prompt, log=log)
    assert 0 < len(stopped.value.videos) < 60
    assert "kept crashing" in str(stopped.value)


def test_continues_an_unfinished_run(browser_page):
    _serve_fake_tiktok(browser_page, API, total=12)
    known = [SavedVideo(url=f"https://www.tiktok.com/@creator{i}/video/{7100000000000000000 + i}",
                        caption=f"from last time {i}", creator=f"@creator{i}") for i in range(9)]
    logs = []
    videos = collect_favorites(browser_page, "me", known=known, scroll_pause=0.3, idle_rounds=3,
                               ask=_unexpected_prompt, log=logs.append)
    assert len(videos) == 12
    assert videos[0].caption == "from last time 0" and videos[0].posted_at == "2023-11-14"
    assert videos[11].caption == "lol 11"
    assert "9 videos already found" in logs[0]


def test_video_files_are_blocked_but_thumbnails_page_code_and_puzzles_are_not(browser_page):
    reached = []

    def handle(route):
        reached.append(route.request.url.split("/")[2])
        if route.request.url.endswith("/@me"):
            route.fulfill(content_type="text/html", body=(
                '<img src="https://p16-sign-va.tiktokcdn.com/obj/tos-maliva-p-0068/cover.jpeg">'
                '<img src="https://p19-common-sign.tiktokcdn-us.com/tos-useast5-p-0068/a~tplv-x.image">'
                '<video src="https://v16-webapp-prime.tiktok.com/video/tos/useast2a/v.mp4"></video>'
                '<script src="https://lf16-tiktok-web.tiktokcdn-us.com/obj/tiktok-web/app.js"></script>'
                '<img src="https://p16-security-va.ibyteimg.com/img/security-captcha/puzzle.png">'))
        else:
            route.fulfill(status=200, body="")

    browser_page.context.route("**/*", handle)
    assert _skip_heavy_media(browser_page, lambda _: None)
    browser_page.goto("https://www.tiktok.com/@me")
    browser_page.wait_for_timeout(500)
    assert "v16-webapp-prime.tiktok.com" not in reached
    assert {"p16-sign-va.tiktokcdn.com", "p19-common-sign.tiktokcdn-us.com",
            "lf16-tiktok-web.tiktokcdn-us.com", "p16-security-va.ibyteimg.com"} <= set(reached)


@pytest.mark.parametrize("url", [
    "https://p16-sign-va.tiktokcdn.com/obj/tos-maliva-p-0068/cover.jpeg",
    "https://lf16-tiktok-web.tiktokcdn-us.com/obj/tiktok-web/tiktok/webapp/main/webapp-desktop/app.js",
    "https://sf16-website-login.neutral.ttwstatic.com/obj/tiktok_web_login_static/login.js",
    "https://www.tiktok.com/api/user/collect/item_list/?count=30&cursor=0",
    "https://www.tiktok.com/@someone/video/7301234567890123456",
    "https://p16-security-va.ibyteimg.com/img/security-captcha-oversea-usa/3d_2385_puzzle.png",
])
def test_block_list_leaves_pages_images_scripts_data_and_puzzles_alone(url):
    assert not any(fnmatch(url, pattern) for pattern in _HEAVY_MEDIA_URLS)


def test_fetches_later_batches_directly_so_the_page_never_grows(browser_page):
    requests = _serve_fake_tiktok(browser_page, API, total=300)
    videos = collect_favorites(browser_page, "me", scroll_pause=0.3, idle_rounds=3,
                               ask=_unexpected_prompt, log=lambda _: None)
    assert len({v.key for v in videos}) == len(videos) == 300
    assert videos[299].caption == "lol 299"
    assert requests == list(range(0, 300, 3))  # each batch asked for once, in order
    assert browser_page.evaluate("document.querySelectorAll('a[href*=\"/video/\"]').length") <= 4


def test_scrolls_instead_when_tiktok_refuses_direct_requests(browser_page):
    requests = _serve_fake_tiktok(browser_page, API, total=30, signed_only=True)
    logs = []
    videos = collect_favorites(browser_page, "me", scroll_pause=0.3, idle_rounds=3,
                               ask=_unexpected_prompt, log=logs.append)
    assert len({v.key for v in videos}) == len(videos) == 30
    assert sum("by scrolling" in m for m in logs) == 1
    assert requests.count(3) == 3  # one page-made request plus the two refused direct tries


def test_direct_requests_reuse_the_pages_own_request():
    template = ("https://www.tiktok.com/api/user/collect/item_list/?aid=1988&count=30&cursor=0"
                "&secUid=MS4w&msToken=abc&X-Bogus=DFSz&X-Gnarly=xyz")
    fresh, as_sent = next_page_urls(template, 1699999999000)
    assert "cursor=1699999999000" in fresh and "X-Bogus" not in fresh and "X-Gnarly" not in fresh
    assert "secUid=MS4w" in fresh and "msToken=abc" in fresh and "count=30" in fresh
    assert "cursor=1699999999000" in as_sent and "X-Bogus=DFSz" in as_sent
    assert next_page_urls("https://www.tiktok.com/api/user/collect/item_list/?count=30", 7) == [
        "https://www.tiktok.com/api/user/collect/item_list/?count=30&cursor=7"]


def test_falling_back_partway_carries_on_instead_of_claiming_the_end(browser_page):
    # Direct requests work for many batches, then TikTok starts refusing them.
    _serve_fake_tiktok(browser_page, API, total=90, refuse_direct_from=45)
    logs = []
    videos = collect_favorites(browser_page, "me", scroll_pause=0.3, idle_rounds=3,
                               ask=_unexpected_prompt, log=logs.append)
    assert len({v.key for v in videos}) == len(videos) == 90
    assert sum("by scrolling" in m for m in logs) == 1


def test_an_unproven_empty_answer_is_not_taken_as_the_end(browser_page):
    # TikTok answering the app's requests with an empty "end of list" must not end the run.
    _serve_fake_tiktok(browser_page, API, total=30, soft_block=True)
    videos = collect_favorites(browser_page, "me", scroll_pause=0.3, idle_rounds=3,
                               ask=_unexpected_prompt, log=lambda _: None)
    assert len({v.key for v in videos}) == len(videos) == 30


def test_a_hiccup_is_retried_without_giving_up_on_direct_requests(browser_page):
    requests = _serve_fake_tiktok(browser_page, API, total=30, flaky=(9,))
    logs = []
    videos = collect_favorites(browser_page, "me", scroll_pause=0.3, idle_rounds=3,
                               ask=_unexpected_prompt, log=logs.append)
    assert len(videos) == 30
    assert requests.count(9) == 2
    assert not any("by scrolling" in m for m in logs)


def test_a_stop_request_ends_cleanly_with_everything_found(browser_page):
    _serve_fake_tiktok(browser_page, API, total=60)
    found = []

    def log(message):
        found.append(_found(message))

    with pytest.raises(CollectionStopped) as stopped:
        collect_favorites(browser_page, "me", scroll_pause=0.3, idle_rounds=3,
                          should_stop=lambda: max(found, default=0) >= 15,
                          ask=_unexpected_prompt, log=log)
    assert str(stopped.value) == "Stopped."
    assert 15 <= len(stopped.value.videos) < 60


def test_a_stall_while_catching_up_is_not_called_the_end(browser_page):
    _serve_fake_tiktok(browser_page, API, total=12, signed_only=True)
    browser_page.add_init_script(
        "document.addEventListener('DOMContentLoaded', () => {"
        "  const original = loadMore;"
        "  loadMore = () => (cursor >= 3) ? undefined : original();"
        "});")
    known = [SavedVideo(url=f"https://www.tiktok.com/@creator{i}/video/{7100000000000000000 + i}")
             for i in range(9)]
    prompts = []
    with pytest.raises(CollectionStopped) as stopped:
        collect_favorites(browser_page, "me", known=known, scroll_pause=0.3, idle_rounds=3,
                          ask=lambda m: prompts.append(m) or "", log=lambda _: None)
    assert "passing 3 of the 9 videos found last time" in prompts[0]
    assert "end of your Favorites" not in prompts[0]
    assert len(stopped.value.videos) == 9


def test_crash_recovery_in_a_visible_browser_like_the_apps(tmp_path):
    """The app uses a visible, persistent browser, where closing the last tab can quit it."""
    if not os.environ.get("DISPLAY"):
        pytest.skip("needs a display (run under xvfb-run)")
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(tmp_path / "profile"), headless=False, viewport={"width": 800, "height": 600},
            executable_path=os.environ.get("TIKTOK_TEST_CHROMIUM"))
        page = context.pages[0] if context.pages else context.new_page()
        _serve_fake_tiktok(page, API, total=30)
        crashed = []

        def log(message):
            if not crashed and _found(message) >= 12:
                crashed.append(True)
                _crash(page)

        videos = collect_favorites(page, "me", scroll_pause=0.3, idle_rounds=3,
                                   ask=_unexpected_prompt, log=log)
        context.close()
    assert crashed and len(videos) == 30
