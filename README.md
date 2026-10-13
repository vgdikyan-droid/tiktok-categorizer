# TikTok Saved-Video Categorizer

Goes through the videos you've saved on TikTok, sorts them into categories
(Fashion, Fundraisers & Causes, Cool Edits, Work & Career, and more), and makes
a spreadsheet with a clickable link to every video.

The spreadsheet has:

- **Summary**: how many videos are in each category. Click a category to jump to its tab.
- **All Videos**: every saved video with its category, caption, creator, post date and link.
- **One tab per category**: for example, a *Fashion* tab that lists only fashion videos.
  A video that fits two categories (say, a thrift haul for charity) shows up in both.

## How it works

TikTok doesn't let other apps read your saved videos. There is no official
API for it, and phones keep each app's data private. The TikTok **website**
shows the same Saved list as the app, though: it's the **Favorites** tab on
your profile. So this tool:

1. opens TikTok in a browser window on your computer,
2. waits while **you** log in (scanning the QR code with the TikTok app on your phone is easiest),
3. opens your profile's Favorites tab and scrolls through it, reading each video's link and caption,
4. sorts the videos and writes the spreadsheet.

Nothing is downloaded, and you never type your password into this tool. The
browser remembers your login in a folder on your computer
(`~/.tiktok-categorizer/browser-profile`), so you only log in once.

## One-time setup

You need a computer (Mac, Windows or Linux) with **Python 3.10 or newer** and,
ideally, **Google Chrome**.

```bash
git clone https://github.com/vgdikyan-droid/restaurant-ops-mcp.git
cd restaurant-ops-mcp/tiktok-categorizer
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[ai]"
```

If you don't have Google Chrome, also run `playwright install chromium`.

## Run it

```bash
tiktok-categorize --limit 30       # a quick test run on your 30 most recent saves
tiktok-categorize                  # everything
```

A browser window opens. Log in if asked, then leave the window alone while it
scrolls. When it finishes you'll have:

- `tiktok_saved_videos.xlsx`, the spreadsheet. It opens in Excel or Numbers,
  or you can upload it to Google Sheets.
- `tiktok_saved_videos.json`, the list of videos it found, so you can re-sort without
  scrolling TikTok again (see below).

Options worth knowing:

| Option | What it does |
|---|---|
| `--user yourname` | Your TikTok username, if it isn't detected automatically |
| `-o my_saves.xlsx` | Choose the spreadsheet's name/location |
| `--limit 100` | Only read your 100 most recent saves |
| `--ai` | Sort with Claude for much better results (see below) |
| `--categories my_categories.json` | Use your own categories |
| `--from-list tiktok_saved_videos.json` | Re-sort a previous run's videos without opening TikTok |

## Better sorting with Claude (`--ai`)

By default, videos are sorted with built-in keyword and hashtag rules. These
are free and work offline, but captions are short and slangy, so some videos
end up in *Uncategorized*. With `--ai`, Claude reads each caption and picks
the best categories, which works much better.

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # Windows PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."
tiktok-categorize --ai
```

- Only each video's caption and creator handle are sent to Anthropic. Your
  login and browsing stay on your computer.
- The default model is `claude-opus-5-5`. A rough guide is about $1 per
  1,000 videos. `--model claude-haiku-5-5` costs a few cents for the same
  number, with somewhat less careful sorting.
- If Claude can't be reached, or declines a batch, those videos are sorted
  with the keyword rules instead and the run still finishes.
- Already scrolled once? `tiktok-categorize --from-list tiktok_saved_videos.json --ai`
  re-sorts without opening TikTok again.

## Your own categories

Print the built-in categories, edit them, and pass the file back:

```bash
tiktok-categorize --print-default-categories > my_categories.json
# edit my_categories.json: rename, delete or add categories
tiktok-categorize --from-list tiktok_saved_videos.json --categories my_categories.json
```

Each category has:

- `name`: becomes the tab name.
- `description`: what Claude reads in `--ai` mode.
- `keywords`: words or phrases matched in captions and hashtags.
- `hashtags`: matched only in hashtags. Use this for words like `edit` that
  mean something else in a plain caption ("Edit: thanks for 1M views").

Hashtags often run words together, so a term also matches hashtags that start
or end with it (`outfit` matches `#outfitinspo`).

## Good to know

- **TikTok changes its website from time to time.** The tool reads both the
  data the Favorites page loads and the video grid you can see, so a small
  change shouldn't break it. If it can't find the Favorites tab (for example,
  because TikTok is in another language), it asks you to click the tab
  yourself and then carries on.
- **If TikTok shows a puzzle or captcha,** solve it in the browser window. The
  tool keeps waiting.
- **Run it now and then, not constantly.** It scrolls at a normal pace and only
  reads your own Favorites, but TikTok's terms discourage automated browsing,
  so use it at your own discretion.
- Only the videos in Favorites are read. Separate *Collections* folders and
  saved sounds or effects are not included.
- Your spreadsheet and video lists are ignored by git (`.gitignore`), so they
  won't be committed by accident.

## Development

```bash
pip install -e ".[ai,dev]"
playwright install chromium
pytest
```

The browser tests drive a real Chromium against a fake TikTok profile page
served locally, covering paging, the grid fallback, `--limit`, and the
"click Favorites yourself" prompt. Set `TIKTOK_TEST_CHROMIUM` to use a
specific Chromium binary.
