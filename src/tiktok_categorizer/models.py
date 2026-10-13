"""The saved-video record shared by the collector, classifiers and spreadsheet."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

_VIDEO_ID_RE = re.compile(r"/(?:video|photo|v)/(\d{8,25})")


def video_id_from_url(url: str) -> str | None:
    match = _VIDEO_ID_RE.search(url)
    return match.group(1) if match else None


@dataclass
class SavedVideo:
    url: str
    caption: str = ""
    creator: str = ""
    posted_at: str | None = None  # YYYY-MM-DD, when the creator posted it

    @property
    def video_id(self) -> str | None:
        return video_id_from_url(self.url)

    @property
    def key(self) -> str:
        return self.video_id or self.url

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SavedVideo:
        known = {f.name for f in fields(cls)}
        if not isinstance(data.get("url"), str) or not data["url"]:
            raise ValueError(f"saved video entry has no url: {data!r}")
        return cls(**{k: v for k, v in data.items() if k in known})


def save_video_list(path: Path, videos: list[SavedVideo], username: str | None) -> None:
    payload = {
        "username": username,
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "videos": [video.to_dict() for video in videos],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_video_list(path: Path) -> list[SavedVideo]:
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("videos") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError(f"{path} does not contain a list of saved videos")
    return [SavedVideo.from_dict(entry) for entry in entries]
