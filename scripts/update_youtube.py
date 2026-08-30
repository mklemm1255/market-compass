#!/usr/bin/env python3
"""Refresh the featured-videos block on the Practical Income Investing homepage.

Pulls the channel's most recent long-form uploads and rewrites the markup
between the YT:START and YT:END markers in docs/index.html. No API key and
no secrets: everything comes from public pages.

Two independent sources are tried, in order:

  1. The channel's /videos tab. Long-form only (Shorts live on their own
     tab), newest first, which is exactly what the homepage wants.
  2. The channel's public RSS feed, reached via the UC... id scraped off
     the channel page. Stable and well-formed, but it mixes Shorts in
     with regular uploads.

Exit codes:
    0  index.html now matches the channel (whether or not anything changed)
    1  something went wrong; index.html was left untouched

Safe by design: unless a source returns the full complement of videos, the
file is not modified. A stale homepage is a far better failure mode than a
broken one. Every step logs what it saw, so a failed run explains itself
without needing a local repro — YouTube is not reachable from every
development sandbox, but it is from the Actions runner.
"""

from __future__ import annotations

import html
import json
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HANDLE = "practicalincomeinvesting"
CHANNEL_URL = f"https://www.youtube.com/@{HANDLE}"
VIDEOS_URL = f"{CHANNEL_URL}/videos"
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
INDEX = Path(__file__).resolve().parent.parent / "docs" / "index.html"

START = "<!-- YT:START"
END = "<!-- YT:END -->"
VIDEO_COUNT = 3
TIMEOUT = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    # Skips the EU consent interstitial, which serves markup with no video
    # data on it. Harmless everywhere else.
    "Cookie": "CONSENT=YES+cb; SOCS=CAI",
}

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")

# "videoId":"XXXXXXXXXXX" ... "title":{"runs":[{"text":"..."}]}
# The window is generous because the renderer inserts thumbnail and
# accessibility blobs between the two, but bounded so a title can never be
# stolen from the following video.
SCRAPE = re.compile(
    r'"videoId":"(?P<id>[A-Za-z0-9_-]{11})".{0,800}?'
    r'"title":\{"runs":\[\{"text":"(?P<title>(?:[^"\\]|\\.)*)"\}\]',
    re.DOTALL,
)

CHANNEL_ID_PATTERNS = (
    r'"channelId"\s*:\s*"(UC[\w-]{22})"',
    r'"externalId"\s*:\s*"(UC[\w-]{22})"',
    r'<meta itemprop="identifier" content="(UC[\w-]{22})"',
    r'channel/(UC[\w-]{22})',
)


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        body = r.read().decode("utf-8", errors="replace")
    log(f"  fetched {url} -> HTTP {r.status}, {len(body):,} bytes")
    return body


def local_name(tag: str) -> str:
    """'{http://www.w3.org/2005/Atom}entry' -> 'entry'."""
    return tag.rsplit("}", 1)[-1]


# --- source 1: the /videos tab -------------------------------------------


def videos_from_channel_page(page: str, n: int) -> list[dict]:
    """Pull ids and titles straight out of the ytInitialData blob."""
    out: list[dict] = []
    seen: set[str] = set()
    for m in SCRAPE.finditer(page):
        vid = m.group("id")
        if vid in seen:
            continue
        try:
            title = json.loads(f'"{m.group("title")}"').strip()
        except json.JSONDecodeError:
            continue
        if not title:
            continue
        seen.add(vid)
        out.append({"id": vid, "title": title})
        if len(out) == n:
            break
    return out


# --- source 2: the RSS feed ----------------------------------------------


def channel_id_candidates(page: str) -> list[str]:
    """Every UC... id on the channel page, best guesses first.

    The page carries ids for recommended and collaborating channels too, so
    this deliberately returns a list rather than committing to the first
    match — each candidate is validated by actually reading its feed.
    """
    found: list[str] = []
    for pattern in CHANNEL_ID_PATTERNS:
        for cid in re.findall(pattern, page):
            if cid not in found:
                found.append(cid)
    return found[:5]


def parse_feed(xml_text: str, n: int) -> list[dict]:
    """Namespace-agnostic Atom parse, so a namespace-URI change can't blank it."""
    root = ET.fromstring(xml_text)
    out: list[dict] = []
    for entry in root.iter():
        if local_name(entry.tag) != "entry":
            continue
        vid, title = "", ""
        for child in entry:
            name = local_name(child.tag)
            if name == "videoId" and child.text:
                vid = child.text.strip()
            elif name == "title" and child.text and not title:
                title = child.text.strip()
            elif name == "group":
                for g in child:
                    if local_name(g.tag) == "title" and g.text and not title:
                        title = g.text.strip()
            elif name == "link" and not vid:
                href = child.get("href", "")
                m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", href)
                if m:
                    vid = m.group(1)
        if not VIDEO_ID.fullmatch(vid) or not title:
            continue
        out.append({"id": vid, "title": title})
        if len(out) == n:
            break
    return out


def videos_from_feed(page: str, n: int) -> list[dict]:
    candidates = channel_id_candidates(page)
    if not candidates:
        log("  no UC... channel id found on the channel page")
        return []
    log(f"  channel id candidates: {', '.join(candidates)}")
    for cid in candidates:
        try:
            videos = parse_feed(fetch(FEED_URL.format(cid=cid)), n)
        except (urllib.error.URLError, ET.ParseError, OSError) as exc:
            log(f"  {cid}: feed unusable ({exc})")
            continue
        log(f"  {cid}: {len(videos)} usable entries")
        if len(videos) >= n:
            return videos
    return []


# --- rendering ------------------------------------------------------------


def render(videos: list[dict]) -> str:
    rows = []
    for v in videos:
        vid = v["id"]
        title = html.escape(v["title"], quote=True)
        rows.append(
            f'          <a href="https://youtu.be/{vid}" target="_blank" rel="noopener" '
            'style="display:flex; gap:12px; align-items:center; text-decoration:none;">\n'
            '            <div style="position:relative; width:100px; height:60px; border-radius:6px; '
            'overflow:hidden; flex-shrink:0; border:1px solid rgba(109,192,64,0.2);">\n'
            f'              <img src="https://img.youtube.com/vi/{vid}/mqdefault.jpg" alt="" '
            'loading="lazy" style="width:100%; height:100%; object-fit:cover;" />\n'
            '              <div style="position:absolute; inset:0; display:flex; align-items:center; '
            'justify-content:center; background:rgba(0,0,0,0.25);">\n'
            '                <div style="width:22px; height:22px; background:rgba(255,0,0,0.92); '
            'border-radius:50%; display:flex; align-items:center; justify-content:center; '
            'padding-left:2px; font-size:0.55rem; color:#fff;">▶</div>\n'
            "              </div>\n"
            "            </div>\n"
            "            <div>\n"
            '              <div style="font-size:0.82rem; font-weight:600; color:#fff; '
            f'margin-bottom:4px; line-height:1.4;">{title}</div>\n'
            '              <div style="font-size:0.72rem; color:rgba(255,255,255,0.35);">'
            "Practical Income Investing</div>\n"
            "            </div>\n"
            "          </a>"
        )
    return "\n".join(rows)


def collect(n: int) -> list[dict]:
    """Try each source in turn; the first to deliver n videos wins."""
    log(f"source 1: {VIDEOS_URL}")
    try:
        page = fetch(VIDEOS_URL)
    except (urllib.error.URLError, OSError) as exc:
        log(f"  channel page unreachable ({exc})")
        page = ""

    if page:
        videos = videos_from_channel_page(page, n)
        log(f"  scraped {len(videos)} videos from the page")
        if len(videos) >= n:
            log("using source 1 (channel /videos tab, long-form only)")
            return videos

        log("source 2: RSS feed")
        videos = videos_from_feed(page, n)
        if len(videos) >= n:
            log("using source 2 (RSS feed)")
            return videos

    return []


def main() -> int:
    try:
        source = INDEX.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read {INDEX}: {exc}", file=sys.stderr)
        return 1

    start = source.find(START)
    end = source.find(END)
    if start == -1 or end == -1 or end < start:
        print("error: YT:START / YT:END markers missing or out of order", file=sys.stderr)
        return 1
    start = source.find("-->", start)
    if start == -1:
        print("error: malformed YT:START marker", file=sys.stderr)
        return 1
    start += len("-->")

    try:
        videos = collect(VIDEO_COUNT)
    except Exception as exc:  # noqa: BLE001 - never let a surprise rewrite the page
        print(f"error: could not read the channel: {exc!r}", file=sys.stderr)
        print("index.html left unchanged", file=sys.stderr)
        return 1

    if len(videos) < VIDEO_COUNT:
        print(
            f"error: every source came up short — got {len(videos)} videos, "
            f"expected {VIDEO_COUNT}; index.html left unchanged",
            file=sys.stderr,
        )
        return 1

    for v in videos:
        log(f"  {v['id']}  {v['title']}")

    updated = source[:start] + "\n" + render(videos) + "\n          " + source[end:]

    if updated == source:
        log("no change — homepage already shows the latest videos")
        return 0

    INDEX.write_text(updated, encoding="utf-8")
    log(f"updated {INDEX.name} with the latest {len(videos)} videos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
