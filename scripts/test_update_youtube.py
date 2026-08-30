#!/usr/bin/env python3
"""Offline checks for update_youtube.py.

No network: the fixtures below stand in for YouTube's markup so the parsing
and splicing can be exercised anywhere, including sandboxes that cannot
reach youtube.com. Run it with `python scripts/test_update_youtube.py`.

The point of these is the fail-safe. This script rewrites the live homepage,
so the cases that matter most are the ones where it must do nothing at all.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update_youtube as u  # noqa: E402

REAL_INDEX = Path(__file__).resolve().parent.parent / "docs" / "index.html"

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


# --- Data API --------------------------------------------------------------

check("duration: minutes and seconds", u.duration_seconds("PT12M34S") == 754)
check("duration: seconds only (a Short)", u.duration_seconds("PT58S") == 58)
check("duration: hours", u.duration_seconds("PT1H2M3S") == 3723)
check("duration: exact minutes", u.duration_seconds("PT3M") == 180)
check("duration: garbage is zero, not an exception", u.duration_seconds("banana") == 0)
check("duration: empty is zero", u.duration_seconds("") == 0)

_api_calls: list[tuple] = []


def fake_api(responses):
    """Stand in for api_get, recording what was asked for."""
    def call(endpoint, key, **params):
        _api_calls.append((endpoint, key, params))
        return responses[endpoint]
    return call


def upload(vid, title):
    return {"snippet": {"title": title, "resourceId": {"videoId": vid}}}


REAL = ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
SHORTS = ["ddddddddddd", "eeeeeeeeeee"]
RESPONSES = {
    "channels": {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUv2cwLeljAJ0fC9vOwdwmQQ"}}}]},
    "playlistItems": {"items": [
        upload(SHORTS[0], "A Short"),
        upload(REAL[0], "Real video one &amp; only"),
        upload(SHORTS[1], "Another Short"),
        upload("fffffffffff", "Deleted video"),
        upload("", "No id at all"),
        upload(REAL[1], "Real video two"),
        upload(REAL[2], "Real video three"),
    ]},
    "videos": {"items": (
        [{"id": v, "contentDetails": {"duration": "PT11M4S"}} for v in REAL]
        + [{"id": v, "contentDetails": {"duration": "PT47S"}} for v in SHORTS]
        + [{"id": "fffffffffff", "contentDetails": {"duration": "PT9M"}}]
    )},
}

_real_api_get = u.api_get
os.environ["YOUTUBE_API_KEY"] = "test-key"
u.api_get = fake_api(RESPONSES)
got = u.videos_from_data_api(3)
check("Data API returns n long-form videos", [v["id"] for v in got] == REAL, str([v["id"] for v in got]))
check("Shorts are filtered out by duration", not any(v["id"] in SHORTS for v in got))
check("a deleted upload is skipped", all(v["id"] != "fffffffffff" for v in got))
check("an upload with no id is skipped", all(v["id"] for v in got))
check("titles are HTML-unescaped from the API", got[0]["title"] == "Real video one & only", got[0]["title"])
check("the uploads playlist is resolved from the handle",
      any(c[0] == "channels" and c[2].get("forHandle") == "@practicalincomeinvesting" for c in _api_calls))
check("the key is passed but never part of the logged params",
      all("key" not in c[2] for c in _api_calls))

# If durations can't be read, keep everything rather than returning nothing.
u.api_get = fake_api({**RESPONSES, "videos": {"items": []}})
got = u.videos_from_data_api(3)
check("an unreadable duration lookup does not empty the result", len(got) == 3, str(len(got)))

# If the handle lookup fails, fall back to the verified channel's playlist.
def failing_channels(endpoint, key, **params):
    if endpoint == "channels":
        raise OSError("handle lookup down")
    return RESPONSES[endpoint]

u.api_get = failing_channels
check("a failed handle lookup falls back and still works",
      [v["id"] for v in u.videos_from_data_api(3)] == REAL)

u.api_get = _real_api_get
os.environ.pop("YOUTUBE_API_KEY", None)
check("no key configured means no Data API call, and no crash", u.videos_from_data_api(3) == [])


# --- channel-page scrape ---------------------------------------------------
#
# Two tile shapes, because YouTube is mid-migration from videoRenderer to
# lockupViewModel and the /videos grid serves either one. The blob below is
# trimmed but structurally faithful to both.

LOCKUP = {
    "lockupViewModel": {
        "contentId": "aaaaaaaaaaa",
        "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
        "contentImage": {"thumbnailViewModel": {"image": {"sources": [{"url": "x"}]}}},
        "metadata": {
            "lockupMetadataViewModel": {
                "title": {"content": 'Lockup — a "quoted" title'},
                "metadata": {"contentMetadataViewModel": {"metadataRows": []}},
            }
        },
    }
}

RENDERER = {
    "richItemRenderer": {
        "content": {
            "videoRenderer": {
                "videoId": "bbbbbbbbbbb",
                "thumbnail": {"thumbnails": [{"url": "x"}]},
                "title": {
                    "runs": [{"text": "Renderer title"}],
                    "accessibility": {"accessibilityData": {"label": "Renderer title, 5 minutes"}},
                },
                "ownerText": {"runs": [{"text": "Practical Income Investing"}]},
                "menu": {
                    "menuRenderer": {
                        "items": [{
                            "menuServiceItemRenderer": {
                                "text": {"runs": [{"text": "Add to queue"}]},
                                "serviceEndpoint": {
                                    "addToPlaylistServiceEndpoint": {"videoId": "zzzzzzzzzzz"}
                                },
                            }
                        }]
                    }
                },
            }
        }
    }
}


def lockup(vid, title):
    return {"lockupViewModel": {"contentId": vid,
                                "metadata": {"lockupMetadataViewModel": {"title": {"content": title}}}}}


DATA = {
    "header": {"pageHeaderRenderer": {
        "pageTitle": "Practical Income Investing",
        "channelId": "UCv2cwLeljAJ0fC9vOwdwmQQ",
    }},
    "contents": [
        LOCKUP,
        RENDERER,
        lockup("aaaaaaaaaaa", "Lockup again (dupe)"),
        {"richItemRenderer": {"content": {"videoRenderer": {"videoId": "ccccccccccc"}}}},
        lockup("ddddddddddd", "Fourth video"),
        lockup("eeeeeeeeeee", "Fifth video"),
    ],
}
PAGE = "<script>var ytInitialData = " + json.dumps(DATA) + ";</script><div>trailing markup</div>"

got = u.videos_from_channel_page(PAGE, 3)
ids = [v["id"] for v in got]
check("scrape returns exactly n", len(got) == 3, str(len(got)))
check("both tile shapes parsed, in order, deduped",
      ids == ["aaaaaaaaaaa", "bbbbbbbbbbb", "ddddddddddd"], str(ids))
check("lockupViewModel title read from title.content",
      got[0]["title"] == 'Lockup — a "quoted" title', repr(got[0]["title"]))
check("videoRenderer title read from title.runs", got[1]["title"] == "Renderer title")
check("an overflow-menu videoId is not mistaken for a video", "zzzzzzzzzzz" not in ids)
check("a tile with no title is skipped rather than mistitled", "ccccccccccc" not in ids)
check("the channel's own name is not used as a video title",
      all(v["title"] != "Practical Income Investing" for v in got))
check("a 24-char channel id is never taken for a video id",
      all(v["id"] != "UCv2cwLeljAJ0fC9vOwdwmQQ" for v in got))

check("window[] assignment is also recognised",
      len(u.videos_from_channel_page('window["ytInitialData"] = ' + json.dumps(DATA) + ";", 3)) == 3)
check("a page with no data blob yields nothing", u.videos_from_channel_page("<html>nope</html>", 3) == [])
check("a page with a corrupt data blob yields nothing",
      u.videos_from_channel_page("var ytInitialData = {not json;", 3) == [])

# --- channel id candidates -------------------------------------------------

IDPAGE = ('"channelId":"UCaaaaaaaaaaaaaaaaaaaaaa"'
          '"channelId":"UCbbbbbbbbbbbbbbbbbbbbbb"'
          '"externalId":"UCcccccccccccccccccccccc"'
          'href="/channel/UCaaaaaaaaaaaaaaaaaaaaaa"')
check("candidates deduped, in priority order",
      u.channel_id_candidates(IDPAGE) == ["UCaaaaaaaaaaaaaaaaaaaaaa",
                                          "UCbbbbbbbbbbbbbbbbbbbbbb",
                                          "UCcccccccccccccccccccccc"])
check("no candidates on a page without any", u.channel_id_candidates("nothing here") == [])

# --- feed parsing ----------------------------------------------------------

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
  <title>Practical Income Investing</title>
  <entry>
    <yt:videoId>H8CdiQIhQ9U</yt:videoId>
    <title>Covered Call &amp; 179% Yield</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=H8CdiQIhQ9U"/>
    <media:group><media:title>ignored</media:title></media:group>
  </entry>
  <entry>
    <yt:videoId>zzCo_8tMIOc</yt:videoId>
    <title>Second</title>
  </entry>
  <entry>
    <title>Broken - no id anywhere</title>
  </entry>
  <entry>
    <link rel="alternate" href="https://www.youtube.com/watch?v=ELDB3sVn6Po"/>
    <media:group><media:title>Title from media:group</media:title></media:group>
  </entry>
</feed>
"""
got = u.parse_feed(FEED, 3)
check("feed yields 3 usable entries", len(got) == 3, str(len(got)))
check("feed skips the entry with no id",
      [v["id"] for v in got] == ["H8CdiQIhQ9U", "zzCo_8tMIOc", "ELDB3sVn6Po"])
check("atom:title wins over media:title", got[0]["title"] == "Covered Call & 179% Yield")
check("link href works as an id fallback", got[2]["title"] == "Title from media:group")
# This is the bug that took the first version down: a hardcoded namespace URI.
check("namespace-agnostic", len(u.parse_feed(FEED.replace("2015", "2077"), 3)) == 3)
check("a valid but empty feed yields nothing, and does not throw",
      u.parse_feed('<feed xmlns="http://www.w3.org/2005/Atom"><title>x</title></feed>', 3) == [])

# --- splicing, against a copy of the real homepage -------------------------

with tempfile.TemporaryDirectory() as tmp:
    copy = Path(tmp) / "index.html"
    shutil.copy(REAL_INDEX, copy)
    before = copy.read_text(encoding="utf-8")

    u.INDEX = copy
    u.collect = lambda n: [
        {"id": "H8CdiQIhQ9U", "title": 'A "quoted" & <angled> title'},
        {"id": "zzCo_8tMIOc", "title": "Second"},
        {"id": "ELDB3sVn6Po", "title": "Third"},
    ]
    rc = u.main()
    after = copy.read_text(encoding="utf-8")

    check("main() exits 0", rc == 0, f"rc={rc}")
    check("the real docs/index.html was never touched",
          REAL_INDEX.read_text(encoding="utf-8") == before)

    s, e = after.find(u.START), after.find(u.END)
    check("both markers survive", s != -1 and e > s)
    check("exactly one marker pair", after.count(u.START) == 1 and after.count(u.END) == 1)
    block = after[after.find("-->", s) + 3:e]
    check("3 anchors between the markers", block.count("<a href=") == 3, str(block.count("<a href=")))
    check("titles are HTML-escaped", "&quot;quoted&quot; &amp; &lt;angled&gt;" in block)
    check("no raw markup leaks out of a title", "<angled>" not in block)
    check("YT:END indentation preserved", after[:e].endswith("          "))
    check("nothing outside the block changed",
          before[:before.find(u.START)] == after[:after.find(u.START)]
          and before[before.find(u.END):] == after[after.find(u.END):])

    check("a second run with the same videos is a no-op",
          u.main() == 0 and copy.read_text(encoding="utf-8") == after)

    u.collect = lambda n: [{"id": "H8CdiQIhQ9U", "title": "Only one"}]
    os.environ["YOUTUBE_API_KEY"] = "test-key"
    check("too few videos exits 1 when a key was available", u.main() == 1)
    check("too few videos changes nothing", copy.read_text(encoding="utf-8") == after)

    # Without a key there is nothing CI could have done, so warn rather than
    # paint the schedule red every morning — but still change nothing.
    os.environ.pop("YOUTUBE_API_KEY", None)
    check("too few videos exits 0 when no key is configured", u.main() == 0)
    check("the no-key path still changes nothing", copy.read_text(encoding="utf-8") == after)

    def boom(n):
        raise RuntimeError("network on fire")

    u.collect = boom
    os.environ["YOUTUBE_API_KEY"] = "test-key"
    check("an exception exits 1", u.main() == 1)
    check("an exception changes nothing", copy.read_text(encoding="utf-8") == after)
    os.environ.pop("YOUTUBE_API_KEY", None)

print()
print(f"{len(fails)} failure(s)" + (": " + ", ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
