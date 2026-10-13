"""tiktok-categorize: sort the videos saved in your TikTok account into a spreadsheet."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from tiktok_categorizer.captions import fill_missing_captions
from tiktok_categorizer.categories import (
    DEFAULT_CATEGORIES,
    CategoryConfigError,
    KeywordClassifier,
    load_categories,
)
from tiktok_categorizer.collector import (
    DEFAULT_PROFILE_DIR,
    CollectorError,
    collect_favorites,
    detect_username,
    open_tiktok_browser,
    wait_for_login,
)
from tiktok_categorizer.models import SavedVideo, load_video_list, save_video_list
from tiktok_categorizer.spreadsheet import write_workbook


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tiktok-categorize",
        description=(
            "Open TikTok in a browser, read the videos saved in your Favorites, sort them "
            "into categories (fashion, fundraisers, cool edits, work, ...) and write a "
            "spreadsheet with a link to each video."
        ),
    )
    parser.add_argument("--user", help="your TikTok username (without @); detected automatically if omitted")
    parser.add_argument("-o", "--output", type=Path, default=Path("tiktok_saved_videos.xlsx"),
                        help="spreadsheet to write (default: %(default)s)")
    parser.add_argument("--categories", type=Path,
                        help="JSON file with your own categories (see --print-default-categories)")
    parser.add_argument("--ai", action="store_true",
                        help="sort with Claude instead of keyword rules (needs an Anthropic API key)")
    parser.add_argument("--model", default=None,
                        help="Claude model for --ai (default: claude-opus-5-5)")
    parser.add_argument("--limit", type=int, help="only read the first N saved videos (good for a test run)")
    parser.add_argument("--from-list", type=Path,
                        help="re-sort videos saved by an earlier run (the .json next to the spreadsheet) "
                             "instead of opening TikTok again")
    parser.add_argument("--browser", choices=["chrome", "chromium"], default="chrome",
                        help="browser to open (default: your installed Google Chrome)")
    parser.add_argument("--browser-path", help="path to a Chrome/Chromium executable to use instead")
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR,
                        help="where the browser keeps your TikTok login (default: %(default)s)")
    parser.add_argument("--print-default-categories", action="store_true",
                        help="print the built-in categories as JSON (a starting point for --categories)")
    return parser


def _collect(args: argparse.Namespace) -> tuple[list[SavedVideo], str | None]:
    if args.from_list:
        videos = load_video_list(args.from_list)
        return (videos[: args.limit] if args.limit else videos), None
    _log("Opening TikTok in a browser window...")
    with open_tiktok_browser(args.profile_dir, browser=args.browser,
                             executable_path=args.browser_path, log=_log) as (context, page):
        wait_for_login(context, page, log=_log)
        username = (args.user or detect_username(page) or "").lstrip("@").strip()
        if not username:
            username = input("What is your TikTok username? ").lstrip("@").strip()
        if not username:
            raise CollectorError("A TikTok username is needed to find your Favorites.")
        _log(f"Reading the Favorites tab of @{username}...")
        videos = collect_favorites(page, username, limit=args.limit, log=_log)
    return videos, username


def run(args: argparse.Namespace) -> int:
    if args.print_default_categories:
        print(json.dumps({"categories": DEFAULT_CATEGORIES}, indent=2))
        return 0
    if args.limit is not None and args.limit < 1:
        _log("--limit must be at least 1")
        return 2

    try:
        categories = load_categories(args.categories)
        videos, username = _collect(args)
    except (CategoryConfigError, CollectorError, ValueError, OSError) as exc:
        _log(f"Error: {exc}")
        return 1

    if not videos:
        _log("No saved videos were found. Check that the browser is logged in to the right "
             "account and that your Favorites tab shows videos, then try again.")
        return 1
    _log(f"Found {len(videos)} saved videos.")

    if not args.from_list:
        list_path = args.output.with_suffix(".json")
        save_video_list(list_path, videos, username)
        _log(f"Saved the video list to {list_path} (re-sort later with --from-list).")

    filled = fill_missing_captions(videos)
    if filled:
        _log(f"Looked up {filled} missing captions.")

    keyword_labels = KeywordClassifier(categories)
    labels: list[list[str]] | None = None
    if args.ai:
        from tiktok_categorizer.ai import DEFAULT_MODEL, AIClassifier, AIUnavailable

        try:
            classifier = AIClassifier(categories, model=args.model or DEFAULT_MODEL, log=_log)
            labels = classifier.classify_all(videos)
            if classifier.fallback_batches:
                _log(f"{classifier.fallback_batches} batch(es) used keyword rules instead of Claude.")
        except AIUnavailable as exc:
            _log(f"{exc}\nSorting with keyword rules instead.")
    if labels is None:
        labels = [keyword_labels.classify(video.caption) for video in videos]

    try:
        counts = write_workbook(args.output, list(zip(videos, labels)), [c.name for c in categories])
    except OSError as exc:
        _log(f"Could not write {args.output}: {exc} (is it open in Excel?)")
        return 1

    _log(f"\nWrote {args.output}")
    for name, count in counts.items():
        if count:
            _log(f"  {name}: {count}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(build_parser().parse_args(argv))
    except KeyboardInterrupt:
        _log("\nStopped.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
