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
    check("too few videos exits 1", u.main() == 1)
    check("too few videos changes nothing", copy.read_text(encoding="utf-8") == after)

    def boom(n):
        raise RuntimeError("network on fire")

    u.collect = boom
    check("an exception exits 1", u.main() == 1)
    check("an exception changes nothing", copy.read_text(encoding="utf-8") == after)

print()
print(f"{len(fails)} failure(s)" + (": " + ", ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
