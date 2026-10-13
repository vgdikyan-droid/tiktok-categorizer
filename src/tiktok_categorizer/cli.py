"""tiktok-categorize: sort the videos saved in your TikTok account into a spreadsheet."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
from typing import Any

from tiktok_categorizer.captions import fill_missing_captions
from tiktok_categorizer.categories import (
    DEFAULT_CATEGORIES,
    CategoryConfigError,
    KeywordClassifier,
    load_categories,
)
from tiktok_categorizer.collector import (
    DEFAULT_PROFILE_DIR,
    CollectionStopped,
    CollectorError,
    collect_favorites,
    detect_username,
    open_tiktok_browser,
    wait_for_login,
)
from tiktok_categorizer.models import SavedVideo, read_video_list, save_video_list
from tiktok_categorizer.spreadsheet import write_workbook

# Results live outside the app's folder, so downloading a new version of the
# app never loses them (or a long collection that is still in progress).
DEFAULT_OUTPUT = Path.home() / "Documents" / "TikTok-Saved-Videos" / "tiktok_saved_videos.xlsx"


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
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT,
                        help="spreadsheet to write (default: %(default)s); the video list is saved "
                             "next to it as a .json file")
    parser.add_argument("--categories", type=Path,
                        help="JSON file with your own categories (see --print-default-categories)")
    parser.add_argument("--ai", action="store_true",
                        help="sort with Claude instead of keyword rules (needs an Anthropic API key)")
    parser.add_argument("--model", default=None,
                        help="Claude model for --ai (default: claude-opus-5-5)")
    parser.add_argument("--limit", type=int,
                        help="a quick test on your N most recent saves (saves no progress)")
    parser.add_argument("--from-list", type=Path, nargs="?", const=True,
                        help="re-sort the videos found by an earlier run instead of opening TikTok "
                             "again (optionally give the .json file to use)")
    parser.add_argument("--start-over", action="store_true",
                        help="ignore the progress of an unfinished earlier run and start from the top")
    parser.add_argument("--no-open", action="store_true",
                        help="don't open the spreadsheet when finished")
    parser.add_argument("--browser", choices=["chrome", "chromium"], default="chrome",
                        help="browser to open (default: your installed Google Chrome)")
    parser.add_argument("--browser-path", help="path to a Chrome/Chromium executable to use instead")
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR,
                        help="where the browser keeps your TikTok login (default: %(default)s)")
    parser.add_argument("--print-default-categories", action="store_true",
                        help="print the built-in categories as JSON (a starting point for --categories)")
    return parser


def _variant(output: Path, label: str) -> Path:
    return output.with_name(f"{output.stem} ({label}){output.suffix}")


def _progress_path(output: Path, username: str) -> Path:
    """Where an unfinished run for this account keeps its progress. Separate from
    the finished list, so a run that stops early never replaces a finished one."""
    safe = re.sub(r"[^\w.-]", "_", username.casefold())
    return output.with_name(f"{output.stem}.{safe}.in-progress.json")


def _unfinished_run(progress_path: Path, username: str) -> list[SavedVideo]:
    if not progress_path.exists():
        return []
    try:
        previous = read_video_list(progress_path)
    except (ValueError, OSError):
        return []
    same_account = (previous.username or "").casefold() == username.casefold()
    return previous.videos if same_account and not previous.complete else []


class _GracefulStop:
    """While the browser is open: the first Control-C finishes the current step and
    stops cleanly (progress saved, spreadsheet written); a second one quits at once."""

    def __init__(self) -> None:
        self._requested = False
        self._previous: Any = None

    def requested(self) -> bool:
        return self._requested

    def _handle(self, signum: int, frame: Any) -> None:
        if self._requested:
            # Quit right away. Raising here could land inside the browser
            # library's own work and freeze it; exiting makes its helper close
            # Chrome. Progress up to the last automatic save is kept.
            _log("\nQuitting.")
            os._exit(130)
        self._requested = True
        _log("\nStopping after this step... (if a question is waiting, press Enter; "
             "press Control-C again to quit at once)")

    def __enter__(self) -> _GracefulStop:
        if threading.current_thread() is threading.main_thread():
            self._previous = signal.signal(signal.SIGINT, self._handle)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._previous is not None:
            signal.signal(signal.SIGINT, self._previous)


@dataclass
class _Outcome:
    videos: list[SavedVideo]
    stopped: str | None = None  # why collecting stopped early, if it did
    progress_path: Path | None = None  # unfinished progress kept for the next run
    test_run: bool = False


def _collect(args: argparse.Namespace, list_path: Path, stop: _GracefulStop) -> _Outcome:
    _log("Opening TikTok in a browser window...")
    with open_tiktok_browser(args.profile_dir, browser=args.browser,
                             executable_path=args.browser_path, log=_log) as (context, page):
        wait_for_login(context, page, log=_log, should_stop=stop.requested)
        username = (args.user or detect_username(page) or "").lstrip("@").strip()
        if not username:
            username = input("What is your TikTok username? ").lstrip("@").strip()
        if not username:
            raise CollectorError("A TikTok username is needed to find your Favorites.")
        # A --limit run is a quick test: it neither continues nor saves progress.
        test_run = args.limit is not None
        progress_path = _progress_path(args.output, username)
        known = [] if args.start_over or test_run else _unfinished_run(progress_path, username)

        def save_progress(videos: list[SavedVideo]) -> None:
            if videos and not test_run:
                save_video_list(progress_path, videos, username, complete=False)

        _log(f"Reading the Favorites tab of @{username}...")
        try:
            videos = collect_favorites(page, username, limit=args.limit, known=known,
                                       on_progress=save_progress, should_stop=stop.requested, log=_log)
        except CollectionStopped as stopped:
            save_progress(stopped.videos)
            kept = progress_path if not test_run and progress_path.exists() else None
            return _Outcome(stopped.videos, str(stopped), kept, test_run)
        if not test_run:
            save_video_list(list_path, videos, username, complete=True)
            progress_path.unlink(missing_ok=True)
    return _Outcome(videos, test_run=test_run)


def _list_to_resort(args: argparse.Namespace, list_path: Path) -> Path:
    if args.from_list is not True:
        return args.from_list
    if list_path.exists():
        return list_path
    unfinished = sorted(list_path.parent.glob(f"{list_path.stem}.*.in-progress.json"),
                        key=lambda path: path.stat().st_mtime)
    if unfinished:
        return unfinished[-1]
    raise ValueError(f"No saved video list yet ({list_path}). Run tiktok-categorize first.")


def run(args: argparse.Namespace) -> int:
    if args.print_default_categories:
        print(json.dumps({"categories": DEFAULT_CATEGORIES}, indent=2))
        return 0
    if args.limit is not None and args.limit < 1:
        _log("--limit must be at least 1")
        return 2

    list_path = args.output.with_suffix(".json")
    try:
        categories = load_categories(args.categories)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.from_list:
            videos = read_video_list(_list_to_resort(args, list_path)).videos
            outcome = _Outcome(videos[: args.limit] if args.limit else videos)
        else:
            with _GracefulStop() as stop:
                outcome = _collect(args, list_path, stop)
    except (CategoryConfigError, CollectorError, ValueError, OSError) as exc:
        _log(f"Error: {exc}")
        return 1

    videos = outcome.videos
    if not videos:
        _log(outcome.stopped or "No saved videos were found. Check that the browser is logged in to "
             "the right account and that your Favorites tab shows videos, then try again.")
        return 1
    _log(f"Found {len(videos)} saved videos.")
    finished = not args.from_list and not outcome.stopped and not outcome.test_run
    if finished:
        _log(f"Saved the video list to {list_path} (re-sort it any time with --from-list).")

    # A partial or test result never replaces your full spreadsheet.
    output = args.output
    if outcome.test_run:
        output = _variant(args.output, "test")
    elif outcome.stopped:
        output = _variant(args.output, "so far")

    try:
        filled = fill_missing_captions(videos)
        if filled:
            _log(f"Looked up {filled} missing captions.")
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
            keyword_labels = KeywordClassifier(categories)
            labels = [keyword_labels.classify(video.caption) for video in videos]
        counts = write_workbook(output, list(zip(videos, labels)), [c.name for c in categories])
    except KeyboardInterrupt:
        if finished or args.from_list:
            _log("\nStopped. Your videos are saved; to finish sorting them, run: "
                 f"tiktok-categorize --from-list{' --ai' if args.ai else ''}")
        else:
            _log("\nStopped.")
        return 130
    except OSError as exc:
        _log(f"Could not write {output}: {exc} (is it open in Excel or Numbers?)")
        return 1

    if finished:
        _variant(args.output, "so far").unlink(missing_ok=True)
    _log(f"\nWrote {output}")
    for name, count in counts.items():
        if count:
            _log(f"  {name}: {count}")
    if outcome.stopped:
        _log(f"\nThis isn't all of your saved videos yet: {outcome.stopped}")
        if outcome.progress_path:
            _log("Your progress is saved. Run the same command again to carry on from where it stopped.")
        elif outcome.test_run:
            _log("This was a test run (--limit), so progress isn't saved. "
                 "Run without --limit to collect everything.")
    if sys.platform == "darwin" and not args.no_open:
        subprocess.run(["open", str(output)], check=False)
    return 1 if outcome.stopped else 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(build_parser().parse_args(argv))
    except KeyboardInterrupt:
        _log("\nStopped.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
