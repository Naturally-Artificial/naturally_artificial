#!/usr/bin/env python3
"""
Pulls the YouTube channel feed and the Telegram channel archive, then writes
the results into the pages between the FEED markers.

Runs on stdlib only — no npm, no packages. Same script works locally and in
the Netlify build image.

    python3 build.py

Each generated region in an HTML file is delimited by:
    <!-- FEED:<name>:start -->  ... generated ...  <!-- FEED:<name>:end -->
Everything outside the markers is hand-written and never touched.
"""

import html
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent

# A deploy must never hang on a slow feed. Netlify kills a build that runs
# past its limit, and a killed build leaves the previous deploy live — so
# fetching is time-boxed and whatever isn't ready keeps its committed
# version instead.
BUDGET_SECONDS = 100
STARTED = time.monotonic()


def out_of_time():
    return time.monotonic() - STARTED > BUDGET_SECONDS

YT_CHANNEL_ID = "UCCSooyKwLmDjs0PHloaAzig"
YT_HANDLE = "https://www.youtube.com/@Naturally_Artificial"
YT_FEED = f"https://www.youtube.com/feeds/videos.xml?channel_id={YT_CHANNEL_ID}"
YT_PLAYLISTS = f"{YT_HANDLE}/playlists?view=1&sort=lad&flow=grid"

# Episodes come from the channel's playlists, never from the channel feed:
# the channel feed mixes in vertical Shorts, the playlists hold only the
# horizontal long-form videos. New playlists are picked up automatically.
def playlist_feed(pid):
    return f"https://www.youtube.com/feeds/videos.xml?playlist_id={pid}"

# The back catalogue before this date is older material that shouldn't appear
# on the Show page. Newer uploads are picked up automatically.
YT_MIN_DATE = datetime(2025, 12, 1, tzinfo=timezone.utc)

TG_CHANNEL = "naturally_artificial"
TG_WEB = f"https://t.me/s/{TG_CHANNEL}"
TG_MAX_POSTS = 500

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------


def get(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def head_ok(url, timeout=8):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def best_thumb(vid):
    """hqdefault is 4:3 and letterboxes 16:9 video, so prefer the widescreen
    sizes. maxresdefault only exists when the upload was HD."""
    hi = f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg"
    return hi if head_ok(hi) else f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"


def summarize(description, limit=190):
    """First useful sentence of a YouTube description.

    Descriptions carry timecodes, link dumps and hashtag blocks after the
    intro, so those are dropped. English is preferred where the channel
    published a bilingual description; otherwise the intro is used as-is.
    """
    if not description:
        return ""

    starts_junk = re.compile(r"^(?:\d{1,2}:\d{2}|[#@▫️•·*])")
    # any link-ish token means the line is a credits/links block, not prose
    has_link = re.compile(
        r"https?://|www\.|\w+\.(?:com|ru|by|io|me|org|net|dev|ai)\b", re.I
    )
    paras = []
    for raw in description.split("\n"):
        p = raw.strip()
        if len(p) < 40 or starts_junk.match(p) or has_link.search(p):
            continue
        paras.append(p)
    if not paras:
        return ""

    def latin_ratio(s):
        lat = len(re.findall(r"[A-Za-z]", s))
        cyr = len(re.findall(r"[А-Яа-яЁё]", s))
        return lat / max(lat + cyr, 1)

    chosen = next((p for p in paras if latin_ratio(p) > 0.7), paras[0])

    if len(chosen) > limit:
        chosen = chosen[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"
    return chosen


def human_count(n):
    """1660 -> 1.7K, 806 -> 806"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1000:
        return f"{n / 1000:.1f}K".replace(".0K", "K")
    return str(n)


def ago(dt):
    """Relative age, in the same voice YouTube uses."""
    days = (datetime.now(timezone.utc) - dt).days
    if days < 1:
        return "today"
    if days == 1:
        return "1 day ago"
    if days < 30:
        return f"{days} days ago"
    months = days // 30
    if months < 12:
        return f"{months} month{'s' if months > 1 else ''} ago"
    years = days // 365
    return f"{years} year{'s' if years > 1 else ''} ago"


# --------------------------------------------------------------------------
# YouTube
# --------------------------------------------------------------------------


def discover_playlists():
    """Playlist ids listed on the channel's Playlists tab."""
    try:
        page = get(YT_PLAYLISTS)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"  playlists unreachable ({exc})")
        return []
    seen, ids = set(), []
    for pid in re.findall(r'"playlistId":"(PL[A-Za-z0-9_-]{16,})"', page):
        if pid not in seen:
            seen.add(pid)
            ids.append(pid)
    return ids


def parse_feed(xml_text):
    root = ET.fromstring(xml_text)
    out = []
    for e in root.findall("atom:entry", NS):
        vid = e.findtext("yt:videoId", "", NS)
        if not vid:
            continue
        group = e.find("media:group", NS)
        views = ""
        description = ""
        if group is not None:
            description = group.findtext("media:description", "", NS) or ""
            community = group.find("media:community", NS)
            if community is not None:
                stats = community.find("media:statistics", NS)
                if stats is not None:
                    views = stats.get("views", "")
        published = e.findtext("atom:published", "", NS)
        try:
            dt = datetime.fromisoformat(published)
        except ValueError:
            dt = datetime.now(timezone.utc)
        out.append(
            {
                "id": vid,
                "title": e.findtext("atom:title", "", NS).strip(),
                "url": f"https://www.youtube.com/watch?v={vid}",
                "thumb": best_thumb(vid),
                "summary": summarize(description),
                "views": human_count(views),
                "date": dt,
            }
        )
    return out


def fetch_youtube():
    playlists = discover_playlists()
    if not playlists:
        print("  no playlists found — falling back to the channel feed")
        videos = parse_feed(get(YT_FEED))
    else:
        videos, seen = [], set()
        for pid in playlists:
            try:
                batch = parse_feed(get(playlist_feed(pid)))
            except (urllib.error.URLError, TimeoutError, ET.ParseError) as exc:
                print(f"  playlist {pid} skipped ({exc})")
                continue
            fresh = [v for v in batch if v["id"] not in seen]
            seen.update(v["id"] for v in fresh)
            videos.extend(fresh)
            print(f"  playlist {pid}: +{len(fresh)}")

    videos = [v for v in videos if v["date"] >= YT_MIN_DATE]
    videos.sort(key=lambda v: v["date"], reverse=True)
    return videos


def render_episode(v, with_summary=True):
    views = f"{v['views']} views • " if v["views"] else ""
    summary = ""
    if with_summary and v["summary"]:
        summary = (
            f'\n            <p class="episode__desc">'
            f"{html.escape(v['summary'], quote=False)}</p>"
        )
    # width/height match the 16:9 thumbnail so the box is reserved correctly
    # before the image loads
    return f"""        <a class="episode" href="{v['url']}" target="_blank" rel="noopener">
          <img class="episode__thumb" src="{v['thumb']}" alt="" loading="lazy" width="1280" height="720">
          <div class="episode__meta">
            <h3 class="episode__title">{html.escape(v['title'], quote=False)}</h3>{summary}
            <p class="episode__views">{views}{ago(v['date'])}</p>
          </div>
        </a>"""


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

# Telegram post text is user-authored HTML. Keep only inline formatting we
# trust and drop the rest, so nothing from the channel can inject markup.
TG_ALLOWED = {"a", "b", "strong", "i", "em", "u", "s", "code", "pre", "br"}


def sanitize(fragment):
    fragment = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", fragment)
    fragment = fragment.replace("<br/>", "<br>").replace("<br />", "<br>")

    def keep(m):
        raw, closing, tag = m.group(0), m.group(1), m.group(2).lower()
        if tag not in TG_ALLOWED:
            return ""
        if closing:
            return f"</{tag}>"
        if tag == "br":
            return "<br>"
        if tag == "a":
            href = re.search(r'href="([^"]*)"', raw)
            url = href.group(1) if href else "#"
            if not url.startswith(("http://", "https://", "/")):
                return ""
            return f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">'
        return f"<{tag}>"

    return re.sub(r"<(/?)([a-zA-Z0-9-]+)[^>]*>", keep, fragment)


# Telegram CDN urls run to several hundred characters, so match inside the
# whole tag rather than a fixed-width window after the class name.
def bg_url(block, marker):
    m = re.search(
        marker + r"[^>]*background-image:url\('([^']+)'\)", block, re.S
    )
    return m.group(1) if m else ""


def parse_posts(page_html):
    posts = []
    blocks = page_html.split('class="tgme_widget_message_wrap')
    for b in blocks[1:]:
        m = re.search(r'data-post="[^/]+/(\d+)"', b)
        if not m:
            continue
        pid = int(m.group(1))

        text = ""
        tm = re.search(
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>\s*(?=<div|<a|</div)',
            b,
            re.S,
        )
        if tm:
            text = sanitize(tm.group(1)).strip()

        photo = bg_url(b, "tgme_widget_message_photo_wrap")
        video_thumb = bg_url(b, "tgme_widget_message_video_thumb")

        duration = ""
        dm = re.search(r'js-message_video_duration">([^<]+)<', b)
        if dm:
            duration = dm.group(1)

        dt = None
        tmm = re.search(r'<time datetime="([^"]+)"', b)
        if tmm:
            try:
                dt = datetime.fromisoformat(tmm.group(1))
            except ValueError:
                pass

        vm2 = re.search(r'tgme_widget_message_views">([^<]+)<', b)

        posts.append(
            {
                "id": pid,
                "url": f"https://t.me/{TG_CHANNEL}/{pid}",
                "text": text,
                "photo": photo,
                "video_thumb": video_thumb,
                "duration": duration,
                "date": dt,
                "views": vm2.group(1).strip() if vm2 else "",
            }
        )
    return posts


def fetch_telegram(limit=TG_MAX_POSTS):
    """Returns (posts, complete). complete is False when the walk was cut
    short, in which case the caller must not overwrite the committed feed
    with a shorter one."""
    seen, collected, before = set(), [], None
    complete = True
    while len(collected) < limit:
        if out_of_time():
            print(f"  telegram: out of time at {len(collected)} posts")
            complete = False
            break
        url = TG_WEB if before is None else f"{TG_WEB}?before={before}"
        try:
            page = get(url)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"  telegram: stopped at {len(collected)} posts ({exc})")
            complete = False
            break

        batch = [p for p in parse_posts(page) if p["id"] not in seen]
        if not batch:
            break
        for p in batch:
            seen.add(p["id"])
        collected.extend(batch)

        oldest = min(p["id"] for p in batch)
        if before is not None and oldest >= before:
            break
        before = oldest
        print(f"  telegram: {len(collected)} posts…")
        if oldest <= 1:
            break

    collected.sort(key=lambda p: p["id"], reverse=True)
    return collected[:limit], complete


def render_post(p, index):
    media = ""
    if p["video_thumb"]:
        badge = f'<span class="tg-post__duration">{html.escape(p["duration"])}</span>' if p["duration"] else ""
        media = (
            f'<div class="tg-post__media tg-post__media--video">'
            f'<img src="{html.escape(p["video_thumb"], quote=True)}" alt="" loading="lazy">'
            f'<span class="tg-post__play" aria-hidden="true"></span>{badge}</div>'
        )
    elif p["photo"]:
        media = (
            f'<div class="tg-post__media">'
            f'<img src="{html.escape(p["photo"], quote=True)}" alt="" loading="lazy"></div>'
        )

    stamp = ""
    if p["date"]:
        stamp = f'<time datetime="{p["date"].isoformat()}">{p["date"].strftime("%d %b %Y")}</time>'
    views = f'<span class="tg-post__views">{html.escape(p["views"])} views</span>' if p["views"] else ""

    body = f'<div class="tg-post__text">{p["text"]}</div>' if p["text"] else ""

    return f"""        <article class="tg-post" data-i="{index}">
          {media}
          {body}
          <footer class="tg-post__foot">{stamp}{views}<a class="tg-post__link" href="{p['url']}" target="_blank" rel="noopener">Open in Telegram</a></footer>
        </article>"""


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


def inject(path, name, content):
    f = ROOT / path
    src = f.read_text(encoding="utf-8")
    start, end = f"<!-- FEED:{name}:start -->", f"<!-- FEED:{name}:end -->"
    pattern = re.compile(
        re.escape(start) + r".*?" + re.escape(end), re.S
    )
    if not pattern.search(src):
        sys.exit(f"marker FEED:{name} not found in {path}")
    f.write_text(
        pattern.sub(lambda _: f"{start}\n{content}\n        {end}", src),
        encoding="utf-8",
    )
    print(f"  wrote {name} → {path}")


def main():
    print("YouTube…")
    videos = fetch_youtube()
    print(f"  {len(videos)} videos")

    print("Telegram…")
    posts, tg_complete = fetch_telegram()
    print(f"  {len(posts)} posts{'' if tg_complete else ' (incomplete — keeping the committed feed)'}")

    if not videos and not posts:
        sys.exit("nothing fetched — leaving pages untouched")

    if videos:
        inject(
            "index.html",
            "episodes",
            "\n".join(render_episode(v) for v in videos[:4]),
        )
        inject(
            "show.html",
            "episodes",
            "\n".join(render_episode(v) for v in videos),
        )

    if posts and tg_complete:
        inject(
            "telegram.html",
            "posts",
            "\n".join(render_post(p, i) for i, p in enumerate(posts)),
        )
        inject(
            "telegram.html",
            "count",
            f'        <span class="tg-count">{len(posts)} posts</span>',
        )

    print("done")


if __name__ == "__main__":
    main()
