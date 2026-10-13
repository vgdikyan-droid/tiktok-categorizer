import json
import os
from urllib.parse import parse_qs, urlsplit

import pytest

from tiktok_categorizer.collector import (
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
  const data = await (await fetch(API + '?cursor=' + cursor)).json();
  for (const item of data.itemList) {
    const a = document.createElement('a');
    a.href = '/@' + item.author.uniqueId + '/video/' + item.id;
    a.style.cssText = 'display:block;height:400px';
    a.innerHTML = '<img alt="' + item.desc + ' created by ' + item.author.uniqueId + ' with a sound">';
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
        page = browser.new_page(viewport={"width": 800, "height": 600})
        yield page
        browser.close()


def _serve_fake_tiktok(page, api_path: str) -> None:
    def handle(route):
        url = urlsplit(route.request.url)
        if url.path == "/@me":
            route.fulfill(content_type="text/html", body=PROFILE_HTML.replace("__API__", api_path))
        elif url.path == api_path:
            cursor = int(parse_qs(url.query).get("cursor", ["0"])[0])
            route.fulfill(content_type="application/json", body=json.dumps(_page_of(cursor)))
        else:
            route.fulfill(status=404, body="")

    page.route("https://www.tiktok.com/**", handle)


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
    _serve_fake_tiktok(browser_page, "/api/user/collect/item_list/")
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
    _serve_fake_tiktok(browser_page, "/api/user/collect/item_list/")
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
