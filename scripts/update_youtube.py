#!/usr/bin/env python3
"""Refresh the featured-videos block on the Practical Income Investing homepage.

Reads the channel's public RSS feed (no API key required), takes the most
recent uploads and rewrites the markup between the YT:START and YT:END
markers in docs/index.html.

Exit codes:
    0  index.html now matches the feed (whether or not anything changed)
    1  something went wrong; index.html was left untouched

Safe by design: if the feed cannot be fetched or parsed, or if fewer videos
come back than expected, the file is not modified. A stale homepage is a far
better failure mode than a broken one.
"""

from __future__ import annotations

import html
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HANDLE = "practicalincomeinvesting"
CHANNEL_URL = f"https://www.youtube.com/@{HANDLE}"
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
INDEX = Path(__file__).resolve().parent.parent / "docs" / "index.html"

START = "<!-- YT:START"
END = "<!-- YT:END -->"
VIDEO_COUNT = 3
TIMEOUT = 30
UA = "Mozilla/5.0 (compatible; PII-site-updater/1.0; +https://practicalincomeinvesting.com)"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", errors="replace")


def resolve_channel_id() -> str:
    """Turn the @handle into a UC... channel id by reading the channel page."""
    page = fetch(CHANNEL_URL)
    for pattern in (
        r'"channelId"\s*:\s*"(UC[\w-]{22})"',
        r'"externalId"\s*:\s*"(UC[\w-]{22})"',
        r'channel/(UC[\w-]{22})',
    ):
        m = re.search(pattern, page)
        if m:
            return m.group(1)
    raise RuntimeError("could not find a channel id on the channel page")


def latest_videos(channel_id: str, n: int) -> list[dict]:
    root = ET.fromstring(fetch(FEED_URL.format(cid=channel_id)))
    out: list[dict] = []
    for entry in root.findall("atom:entry", NS):
        vid = entry.findtext("yt:videoId", default="", namespaces=NS).strip()
        title = entry.findtext("atom:title", default="", namespaces=NS).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid) or not title:
            continue
        out.append({"id": vid, "title": title})
        if len(out) == n:
            break
    return out


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
        channel_id = resolve_channel_id()
        videos = latest_videos(channel_id, VIDEO_COUNT)
    except Exception as exc:  # network, parse, anything
        print(f"error: could not read the channel feed: {exc}", file=sys.stderr)
        print("index.html left unchanged", file=sys.stderr)
        return 1

    if len(videos) < VIDEO_COUNT:
        print(
            f"error: feed returned {len(videos)} usable videos, expected {VIDEO_COUNT}; "
            "index.html left unchanged",
            file=sys.stderr,
        )
        return 1

    updated = source[:start] + "\n" + render(videos) + "\n          " + source[end:]

    if updated == source:
        print("no change — homepage already shows the latest videos")
        return 0

    INDEX.write_text(updated, encoding="utf-8")
    print(f"updated {INDEX.name} with the latest {len(videos)} videos:")
    for v in videos:
        print(f"  {v['id']}  {v['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
