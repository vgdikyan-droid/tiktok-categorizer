"""Category definitions and the free, offline keyword/hashtag classifier.

Each category has a name, a description (used by the optional AI mode), and
two term lists:

- keywords match whole words or phrases in the caption, and hashtags too;
- hashtags match only hashtags (for words like "edit" that mean something else
  in plain captions, e.g. "Edit: thanks for 1M views").

Hashtags are often run together ("#outfitinspo", "#capcutedit"), so a term also
matches a hashtag that starts or ends with it. When several terms match one
hashtag, the longest wins, so "#badcredit" counts as money, not as an "edit".
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any

UNCATEGORIZED = "Uncategorized"
_HASHTAG_RE = re.compile(r"#(\w+)")
_MIN_AFFIX_LEN = 4
_EXACT_BONUS = 1000


@dataclass(frozen=True)
class Category:
    name: str
    description: str = ""
    keywords: tuple[str, ...] = field(default_factory=tuple)
    hashtags: tuple[str, ...] = field(default_factory=tuple)


DEFAULT_CATEGORIES: list[dict[str, Any]] = [
    {
        "name": "Fashion",
        "description": "Outfits, styling, clothing hauls, shoes, accessories, thrifting, fashion trends",
        "keywords": ["fashion", "outfit", "outfits", "ootd", "style", "styling", "streetwear",
                     "thrift", "thrifting", "haul", "lookbook", "fit check", "wardrobe",
                     "capsule wardrobe", "sneakers", "jewelry", "handbag", "what i wore",
                     "outfit ideas", "how to style"],
        "hashtags": ["fashiontiktok", "outfitinspo", "ootd", "fitcheck", "styletips", "grwmoutfit",
                     "thrifted", "streetstyle", "mensfashion", "womensfashion"],
    },
    {
        "name": "Fundraisers & Causes",
        "description": "Fundraisers, GoFundMe and donation drives, charities, mutual aid, volunteering, awareness campaigns",
        "keywords": ["fundraiser", "fundraising", "gofundme", "go fund me", "donate", "donation",
                     "donations", "charity", "nonprofit", "non profit", "mutual aid",
                     "raise money", "raising money", "volunteer", "relief fund", "spread awareness"],
        "hashtags": ["gofundme", "fundraiser", "charity", "mutualaid", "donate", "nonprofit"],
    },
    {
        "name": "Cool Edits",
        "description": "Video edits, transitions, VFX, fan edits, CapCut / After Effects / Alight Motion work, impressive editing",
        "keywords": ["capcut", "after effects", "alight motion", "velocity edit", "smooth transition",
                     "vfx", "cgi", "motion graphics", "video editing", "fan edit", "edit tutorial"],
        "hashtags": ["edit", "edits", "fanedit", "capcutedit", "aftereffects", "videoedit",
                     "editing", "velocity", "transition", "alightmotion"],
    },
    {
        "name": "Work & Career",
        "description": "Jobs, careers, interviews, resumes, office life, business, entrepreneurship, productivity",
        "keywords": ["career", "jobs", "new job", "job search", "job interview", "interview tips",
                     "resume", "cover letter", "linkedin", "corporate", "office", "9 to 5", "9-5",
                     "coworker", "my boss",
                     "salary", "promotion", "remote work", "work from home", "wfh", "hiring",
                     "entrepreneur", "small business", "business owner", "restaurant owner",
                     "hospitality", "productivity", "excel"],
        "hashtags": ["careertok", "corporatetiktok", "corporatelife", "worklife", "jobsearch",
                     "smallbusiness", "entrepreneur", "productivity", "worktok"],
    },
    {
        "name": "Food & Recipes",
        "description": "Recipes, cooking, baking, meal prep, restaurants and food reviews",
        "keywords": ["recipe", "recipes", "cooking", "baking", "meal prep", "dinner idea",
                     "dinner ideas", "easy dinner", "foodie", "food review", "what i eat",
                     "air fryer", "breakfast", "lunch ideas", "dessert"],
        "hashtags": ["foodtok", "recipe", "cooking", "baking", "foodie", "easyrecipe", "mealprep"],
    },
    {
        "name": "Fitness & Health",
        "description": "Workouts, gym, running, yoga, pilates, nutrition, health tips",
        "keywords": ["workout", "gym", "fitness", "exercise", "pilates", "yoga", "run club",
                     "marathon", "hiit", "protein", "weight loss", "strength training", "stretching"],
        "hashtags": ["gymtok", "fittok", "workout", "fitness", "gym", "pilates"],
    },
    {
        "name": "Beauty & Skincare",
        "description": "Makeup, skincare, hair, nails, get-ready-with-me routines",
        "keywords": ["makeup", "skincare", "skin care", "grwm", "get ready with me", "hairstyle",
                     "hair tutorial", "nails", "lipstick", "foundation", "beauty"],
        "hashtags": ["beautytok", "makeup", "skincare", "grwm", "hairtok", "nailart"],
    },
    {
        "name": "Home & DIY",
        "description": "Home decor, organizing, cleaning, renovation, crafts and DIY projects",
        "keywords": ["diy", "home decor", "decor", "organization", "organizing", "cleaning",
                     "renovation", "apartment", "interior design", "crafts", "room makeover"],
        "hashtags": ["homedecor", "cleantok", "diy", "organization", "apartmenttok", "homehacks"],
    },
    {
        "name": "Travel",
        "description": "Trips, destinations, itineraries, hotels, travel tips",
        "keywords": ["travel", "vacation", "trip", "itinerary", "hotel", "places to visit",
                     "things to do in", "road trip", "travel tips", "hidden gem"],
        "hashtags": ["traveltok", "travel", "wanderlust", "vacation", "bucketlist"],
    },
    {
        "name": "Money & Finance",
        "description": "Budgeting, investing, saving, credit, side hustles, taxes",
        "keywords": ["finance", "investing", "budget", "budgeting", "money tips", "stocks",
                     "credit score", "credit card", "savings", "side hustle", "taxes",
                     "personal finance"],
        "hashtags": ["moneytok", "fintok", "personalfinance", "investing", "budgeting", "sidehustle",
                     "credit"],
    },
    {
        "name": "Comedy & Memes",
        "description": "Funny videos, skits, memes, pranks",
        "keywords": ["funny", "comedy", "meme", "memes", "skit", "prank", "joke", "jokes"],
        "hashtags": ["comedy", "funny", "memes", "skit", "humor", "relatable"],
    },
    {
        "name": "Music & Dance",
        "description": "Songs, covers, singing, dances and choreography",
        "keywords": ["dance", "dancing", "choreography", "song", "song cover", "singing", "music"],
        "hashtags": ["dancechallenge", "dance", "music", "singing", "cover"],
    },
    {
        "name": "Learning & Tips",
        "description": "Tutorials, how-tos, explainers, life hacks, books, science and history",
        "keywords": ["tutorial", "how to", "explained", "life hack", "lifehack", "did you know",
                     "history", "science", "learn", "study tips", "book recommendations"],
        "hashtags": ["learnontiktok", "booktok", "lifehack", "studytok", "edutok", "tips"],
    },
    {
        "name": "Pets & Animals",
        "description": "Dogs, cats and other pets or animals",
        "keywords": ["my dog", "my cat", "dogs", "cats", "puppy", "kitten", "pets", "animals"],
        "hashtags": ["dogsoftiktok", "catsoftiktok", "petsoftiktok", "puppy", "animals"],
    },
]


class CategoryConfigError(ValueError):
    pass


def _terms(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
        raise CategoryConfigError(f"{where} must be a list of text values")
    return tuple(t.strip() for t in value if t.strip())


def parse_categories(data: Any) -> list[Category]:
    entries = data.get("categories") if isinstance(data, dict) else data
    if not isinstance(entries, list) or not entries:
        raise CategoryConfigError("expected a non-empty list of categories")
    categories: list[Category] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise CategoryConfigError(f"category #{index} must be an object")
        name = str(entry.get("name") or "").strip()
        if not name:
            raise CategoryConfigError(f"category #{index} has no name")
        if name.casefold() in seen or name.casefold() == UNCATEGORIZED.casefold():
            raise CategoryConfigError(f"category name {name!r} is duplicated or reserved")
        seen.add(name.casefold())
        categories.append(Category(
            name=name,
            description=str(entry.get("description") or "").strip(),
            keywords=_terms(entry.get("keywords"), f"{name}: keywords"),
            hashtags=_terms(entry.get("hashtags"), f"{name}: hashtags"),
        ))
    return categories


def load_categories(path: Path | None) -> list[Category]:
    if path is None:
        return parse_categories(DEFAULT_CATEGORIES)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CategoryConfigError(f"{path} is not valid JSON: {exc}") from exc
    return parse_categories(data)


def _squash(term: str) -> str:
    return "".join(ch for ch in term.casefold() if ch.isalnum())


def _phrase_pattern(term: str) -> re.Pattern[str]:
    words = [re.escape(w) for w in term.casefold().split()]
    return re.compile(r"(?<!\w)" + r"[\s\-]+".join(words) + r"(?:s|es)?(?!\w)")


class KeywordClassifier:
    def __init__(self, categories: list[Category]):
        self.categories = categories
        self._phrases = [
            [(term, _phrase_pattern(term)) for term in category.keywords]
            for category in categories
        ]
        self._tag_terms = [
            {_squash(t) for t in (*category.keywords, *category.hashtags) if _squash(t)}
            for category in categories
        ]

    def _hashtag_strength(self, hashtag: str, term: str) -> int:
        if hashtag in (term, term + "s"):
            return _EXACT_BONUS + len(term)
        if len(term) >= _MIN_AFFIX_LEN and (
            hashtag.startswith(term) or hashtag.endswith(term) or hashtag.endswith(term + "s")
        ):
            return len(term)
        return 0

    def classify(self, caption: str) -> list[str]:
        """Matching category names, strongest first; [UNCATEGORIZED] if none."""
        text = caption.casefold()
        hashtags = [_squash(tag) for tag in _HASHTAG_RE.findall(text)]
        plain = _HASHTAG_RE.sub(" ", text)
        matched: list[set[str]] = [set() for _ in self.categories]

        for index, phrases in enumerate(self._phrases):
            for term, pattern in phrases:
                if pattern.search(plain):
                    matched[index].add(_squash(term))

        for hashtag in hashtags:
            best, owners = 0, []
            for index, terms in enumerate(self._tag_terms):
                for term in terms:
                    strength = self._hashtag_strength(hashtag, term)
                    if strength > best:
                        best, owners = strength, [(index, term)]
                    elif strength and strength == best:
                        owners.append((index, term))
            for index, term in owners:
                matched[index].add(term)

        ranked = sorted(
            (i for i in range(len(self.categories)) if matched[i]),
            key=lambda i: (-len(matched[i]), i),
        )
        return [self.categories[i].name for i in ranked] or [UNCATEGORIZED]
