#!/usr/bin/env python3
"""Refresh the featured-videos block on the Practical Income Investing homepage.

Pulls the channel's most recent long-form uploads and rewrites the markup
between the YT:START and YT:END markers in docs/index.html. No API key and
no secrets: everything comes from public pages.

Four sources are tried, in order. Only the first works from CI, and the
reason is worth recording, because it is not obvious and it cost four runs
to establish:

  1. The YouTube Data API v3, keyed by the YOUTUBE_API_KEY secret. Reads
     the uploads playlist and drops Shorts by duration.
  2. The channel's /videos tab, scraped from its ytInitialData blob.
  3. The InnerTube browse endpoint the site itself calls.
  4. The channel's public RSS feed.

Sources 2 through 4 all fail from a GitHub Actions runner, and not by
erroring — each returns HTTP 200 with the video data simply absent. The
channel page comes back as ~795 KB of genuine, correctly-titled markup
containing zero video ids and a captcha reference; InnerTube answers with
an empty result; the RSS feed returns a valid document that names the
right channel and lists no entries. YouTube withholds video data from
datacenter IP ranges. No amount of parsing fixes that, which is why the
API key is not optional in CI. The scrapers are kept because they work
fine when this is run from a normal connection.

Exit codes:
    0  index.html now matches the channel (whether or not anything changed),
       or no API key is configured and there is nothing to be done about it
    1  a source was available but the refresh failed; index.html untouched

Safe by design: unless a source returns the full complement of videos, the
file is not modified. A stale homepage is a far better failure mode than a
broken one. Every step logs what it saw, so a failed run explains itself
without needing a local repro — YouTube is not reachable from every
development sandbox, but it is from the Actions runner.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HANDLE = "practicalincomeinvesting"
CHANNEL_URL = f"https://www.youtube.com/@{HANDLE}"
VIDEOS_URL = f"{CHANNEL_URL}/videos"
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
# The uploads playlist of channel UCxxx is always UUxxx. Same feed endpoint,
# different key, and it sometimes answers when the channel_id form returns
# an empty document.
UPLOADS_FEED_URL = "https://www.youtube.com/feeds/videos.xml?playlist_id={pid}"
# The endpoint youtube.com itself calls to fill the videos grid. It answers
# with the same renderer objects the page embeds, so the same walk reads it.
INNERTUBE_URL = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false"
INNERTUBE_CLIENT = {
    "clientName": "WEB",
    "clientVersion": "2.20240710.01.00",
    "hl": "en",
    "gl": "US",
}
# base64 of the protobuf selecting the channel's Videos tab.
VIDEOS_TAB_PARAMS = "EgZ2aWRlb3PyBgQKAjoA"

# YouTube Data API v3. The only source that works from a datacenter IP —
# see the module docstring. Key comes from the YOUTUBE_API_KEY secret.
API_ROOT = "https://www.googleapis.com/youtube/v3/"
API_KEY_ENV = "YOUTUBE_API_KEY"
# Confirmed against the channel's own feed, which reports this id as
# belonging to 'Practical Income Investing'. Used if the handle lookup
# fails; the API is asked for it first regardless.
KNOWN_CHANNEL_ID = "UCv2cwLeljAJ0fC9vOwdwmQQ"
# Uploads to consider before filtering Shorts out, so a run of Shorts
# cannot starve the three slots.
API_SCAN = 25
# Anything at or under this is a Short, and a vertical thumbnail looks
# wrong in the homepage's 100x60 tile.
SHORT_MAX_SECONDS = 180
ISO_DURATION = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
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

# Where the page keeps its data blob. raw_decode finds the end of the object
# for us, so no brace counting and no guessing at the trailing punctuation.
INITIAL_DATA_MARKERS = (
    "var ytInitialData = ",
    'window["ytInitialData"] = ',
    "ytInitialData = ",
)

# Keys that hold an eleven-character video id. YouTube has been migrating
# grid tiles from videoRenderer ("videoId") to lockupViewModel
# ("contentId"); both shapes turn up depending on the rollout.
ID_KEYS = ("videoId", "contentId")

# Subtrees that carry titles belonging to something other than the video —
# the channel, a playlist the video sits in, an overflow menu item.
TITLE_SKIP_KEYS = frozenset({
    "thumbnail", "thumbnails", "thumbnailOverlays", "menu", "badges",
    "owner", "ownerText", "avatar", "channelThumbnail",
    "shortBylineText", "longBylineText", "navigationEndpoint",
})

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


def post_json(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    headers = dict(HEADERS)
    headers["Content-Type"] = "application/json"
    headers["X-YouTube-Client-Name"] = "1"
    headers["X-YouTube-Client-Version"] = INNERTUBE_CLIENT["clientVersion"]
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read().decode("utf-8", errors="replace")
    log(f"  posted {url} -> HTTP {r.status}, {len(raw):,} bytes")
    return json.loads(raw)


def local_name(tag: str) -> str:
    """'{http://www.w3.org/2005/Atom}entry' -> 'entry'."""
    return tag.rsplit("}", 1)[-1]


# --- source 1: the YouTube Data API ---------------------------------------


def api_get(endpoint: str, key: str, **params) -> dict:
    """GET a Data API endpoint. The key is never written to the log."""
    query = urllib.parse.urlencode({**params, "key": key})
    url = API_ROOT + endpoint + "?" + query
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read().decode("utf-8", errors="replace")
    redacted = API_ROOT + endpoint + "?" + urllib.parse.urlencode(params)
    log(f"  GET {redacted} -> HTTP {r.status}, {len(raw):,} bytes")
    return json.loads(raw)


def duration_seconds(value: str) -> int:
    m = ISO_DURATION.fullmatch(value or "")
    if not m:
        return 0
    days, hours, minutes, seconds = (int(x or 0) for x in m.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def uploads_playlist_id(key: str) -> str:
    """The channel's uploads playlist, which holds every upload in order."""
    try:
        data = api_get("channels", key, part="contentDetails", forHandle="@" + HANDLE)
        items = data.get("items") or []
        if items:
            uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
            log(f"  uploads playlist: {uploads}")
            return uploads
        log("  handle lookup returned no channel")
    except (urllib.error.URLError, ValueError, KeyError, OSError) as exc:
        log(f"  handle lookup failed ({exc})")
    fallback = "UU" + KNOWN_CHANNEL_ID[2:]
    log(f"  falling back to the known uploads playlist: {fallback}")
    return fallback


def videos_from_data_api(n: int) -> list[dict]:
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        log(f"  no {API_KEY_ENV} configured — skipping the Data API")
        return []

    playlist = uploads_playlist_id(key)
    try:
        listing = api_get(
            "playlistItems", key,
            part="snippet", playlistId=playlist, maxResults=str(API_SCAN),
        )
    except (urllib.error.URLError, ValueError, OSError) as exc:
        log(f"  playlistItems failed ({exc})")
        return []

    candidates: list[dict] = []
    for item in listing.get("items") or []:
        snippet = item.get("snippet") or {}
        vid = (snippet.get("resourceId") or {}).get("videoId", "")
        title = html.unescape((snippet.get("title") or "").strip())
        # A deleted or private upload keeps its slot but loses its title.
        if VIDEO_ID.fullmatch(vid) and title and title.lower() not in ("deleted video", "private video"):
            candidates.append({"id": vid, "title": title})
    log(f"  {len(candidates)} uploads returned")
    if not candidates:
        return []

    # One extra call tells us which of those are Shorts.
    long_form = candidates
    try:
        details = api_get(
            "videos", key,
            part="contentDetails", id=",".join(c["id"] for c in candidates),
        )
        lengths = {
            item["id"]: duration_seconds((item.get("contentDetails") or {}).get("duration", ""))
            for item in details.get("items") or []
        }
        filtered = [c for c in candidates if lengths.get(c["id"], 0) > SHORT_MAX_SECONDS]
        log(f"  {len(candidates) - len(filtered)} of those are Shorts")
        # Only trust the filter if it left us enough to work with; a bad
        # duration read should not be able to empty the list.
        if len(filtered) >= n:
            long_form = filtered
        else:
            log("  too few long-form videos after filtering — keeping Shorts in")
    except (urllib.error.URLError, ValueError, OSError) as exc:
        log(f"  duration lookup failed, Shorts not filtered ({exc})")

    return long_form[:n]


# --- source 2: the /videos tab -------------------------------------------


def initial_data(page: str) -> dict | list | None:
    """Pull the ytInitialData object out of the page and parse it."""
    decoder = json.JSONDecoder()
    for marker in INITIAL_DATA_MARKERS:
        i = page.find(marker)
        if i == -1:
            continue
        try:
            obj, _ = decoder.raw_decode(page, i + len(marker))
        except ValueError:
            continue
        return obj
    return None


def title_text(value) -> str:
    """Read a title out of whichever shape YouTube wrapped it in."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("content", "simpleText"):
            if isinstance(value.get(key), str):
                return value[key].strip()
        runs = value.get("runs")
        if isinstance(runs, list):
            return "".join(
                r["text"] for r in runs if isinstance(r, dict) and isinstance(r.get("text"), str)
            ).strip()
    return ""


def find_title(node, depth: int = 0) -> str:
    """Nearest title inside a tile. Depth-limited so it stays local to the tile."""
    if depth > 6:
        return ""
    if isinstance(node, dict):
        for key in ("title", "headline"):
            found = title_text(node.get(key))
            if found:
                return found
        for key, value in node.items():
            if key in TITLE_SKIP_KEYS:
                continue
            found = find_title(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = find_title(value, depth + 1)
            if found:
                return found
    return ""


def walk_for_videos(node, out: list[dict], seen: set[str]) -> None:
    """Collect id/title pairs in document order, outermost tile first.

    Structural rather than regex-based: the exact renderer YouTube uses for
    a grid tile changes without notice, but a tile has always been an
    object carrying a video id with the title somewhere beneath it.
    """
    if isinstance(node, list):
        for value in node:
            walk_for_videos(value, out, seen)
        return
    if not isinstance(node, dict):
        return

    vid = ""
    for key in ID_KEYS:
        value = node.get(key)
        if isinstance(value, str) and VIDEO_ID.fullmatch(value):
            vid = value
            break

    if vid:
        title = find_title(node)
        if title:
            if vid not in seen:
                seen.add(vid)
                out.append({"id": vid, "title": title})
            # Don't descend: ids below this point belong to the tile's
            # overflow menu and playlist endpoints, not to sibling videos.
            return

    for value in node.values():
        walk_for_videos(value, out, seen)


def videos_from_channel_page(page: str, n: int) -> list[dict]:
    data = initial_data(page)
    if data is None:
        log("  no ytInitialData object on the page")
        return []
    out: list[dict] = []
    walk_for_videos(data, out, set())
    return out[:n]


def diagnose_page(page: str) -> None:
    """Explain an empty scrape, so the next fix does not need a guess.

    The interesting question is always the same: did YouTube send a page
    with videos on it that we failed to read, or a page with no videos on
    it at all (a consent wall, a bot check, a JS-only shell)?
    """
    title = re.search(r"<title>(.*?)</title>", page, re.DOTALL)
    log(f"  page title: {title.group(1).strip()[:120] if title else '(none)'}")

    markers = [m for m in INITIAL_DATA_MARKERS if m in page]
    log(f"  data blob markers present: {markers or 'none'}")

    counts = {
        key: page.count(f'"{key}"')
        for key in ("videoId", "contentId", "lockupViewModel", "videoRenderer",
                    "gridVideoRenderer", "richItemRenderer", "reelItemRenderer")
    }
    log("  renderer keys: " + ", ".join(f"{k}={v}" for k, v in counts.items()))

    raw_ids: list[str] = []
    for key in ID_KEYS:
        for cid in re.findall(rf'"{key}":"([A-Za-z0-9_-]{{11}})"', page):
            if cid not in raw_ids:
                raw_ids.append(cid)
    log(f"  raw ids in the html: {len(raw_ids)}{' -> ' + ', '.join(raw_ids[:5]) if raw_ids else ''}")

    for needle in ("consent.youtube.com", "Sign in to confirm", "not a bot",
                   "captcha", "unusual traffic", "Before you continue"):
        if needle.lower() in page.lower():
            log(f"  interstitial signal: {needle!r} appears on the page")

    data = initial_data(page)
    if isinstance(data, dict):
        log(f"  ytInitialData top-level keys: {', '.join(sorted(data)[:12])}")


# --- source 3: the InnerTube browse endpoint ------------------------------


def videos_from_innertube(channel_id: str, n: int) -> list[dict]:
    payload = {
        "context": {"client": dict(INNERTUBE_CLIENT)},
        "browseId": channel_id,
        "params": VIDEOS_TAB_PARAMS,
    }
    try:
        data = post_json(INNERTUBE_URL, payload)
    except (urllib.error.URLError, ValueError, OSError) as exc:
        log(f"  innertube unusable ({exc})")
        return []
    out: list[dict] = []
    walk_for_videos(data, out, set())
    log(f"  innertube returned {len(out)} videos")
    return out[:n]


# --- source 4: the RSS feed ----------------------------------------------


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


def feed_owner(root) -> str:
    """The channel name the feed claims, for telling a wrong id from a starved one."""
    for child in root:
        if local_name(child.tag) == "title" and child.text:
            return child.text.strip()
    return "?"


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


def try_feed(url: str, n: int) -> list[dict]:
    try:
        xml_text = fetch(url)
        root = ET.fromstring(xml_text)
    except (urllib.error.URLError, ET.ParseError, OSError) as exc:
        log(f"  feed unusable ({exc})")
        return []
    videos = parse_feed(xml_text, n)
    log(f"  feed belongs to {feed_owner(root)!r}, {len(videos)} usable entries")
    return videos


def videos_from_feed(candidates: list[str], n: int) -> list[dict]:
    for cid in candidates:
        for url in (FEED_URL.format(cid=cid), UPLOADS_FEED_URL.format(pid="UU" + cid[2:])):
            videos = try_feed(url, n)
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
    log("source 1: YouTube Data API")
    videos = videos_from_data_api(n)
    if len(videos) >= n:
        log("using source 1 (Data API)")
        return videos

    log(f"source 2: channel page {VIDEOS_URL}")
    try:
        page = fetch(VIDEOS_URL)
    except (urllib.error.URLError, OSError) as exc:
        log(f"  channel page unreachable ({exc})")
        page = ""

    if page:
        videos = videos_from_channel_page(page, n)
        log(f"  scraped {len(videos)} videos from the page")
        if len(videos) >= n:
            log("using source 2 (channel page)")
            return videos
        diagnose_page(page)

    candidates = channel_id_candidates(page) if page else [KNOWN_CHANNEL_ID]
    log(f"channel id candidates: {', '.join(candidates)}")

    log("source 3: InnerTube browse")
    for cid in candidates:
        videos = videos_from_innertube(cid, n)
        if len(videos) >= n:
            log(f"using source 3 (InnerTube, {cid})")
            return videos

    log("source 4: RSS feed")
    videos = videos_from_feed(candidates, n)
    if len(videos) >= n:
        log("using source 4 (RSS feed)")
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
        if not os.environ.get(API_KEY_ENV, "").strip():
            # Not a fault worth a red X every morning: the one source that
            # works from CI has not been given a key yet.
            print(
                f"::warning::Homepage videos not refreshed — no {API_KEY_ENV} secret is set, "
                "and YouTube serves no video data to GitHub's IP ranges. "
                "docs/index.html left unchanged."
            )
            return 0
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
