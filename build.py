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
import json
import re
import subprocess
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

# The site's own address. Change this one line when a custom domain lands —
# the canonical links, the social cards, robots.txt and the sitemap are all
# regenerated from it. No trailing slash.
SITE_URL = "https://naturallyartif.netlify.app"

# Netlify serves these without the .html, and that extensionless form is the
# one that must appear in canonical links and the sitemap; otherwise search
# engines index both spellings of every page.
PAGE_PATHS = {
    "index.html": "/",
    "about.html": "/about",
    "show.html": "/show",
    "speaking.html": "/speaking",
    "contact.html": "/contact",
    "presskit.html": "/presskit",
    "privacy.html": "/privacy",
    "terms.html": "/terms",
}

# Legal pages belong in the sitemap but shouldn't compete with the real
# content, so they carry a lower priority.
LOW_PRIORITY = {"privacy.html", "terms.html"}

OG_IMAGE = "/assets/og-image.jpg"  # 1200x630

# Cache-busting suffix for the generated pages' stylesheet links. Keep it in
# step with the ?v= the hand-written pages carry.
ASSET_V = 43

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
                # the raw count and the owning channel, for the press kit:
                # playlists can hold other people's videos, and those are
                # Sergey's guest appearances, not episodes of the show.
                "views_n": int(views) if views.isdigit() else 0,
                "channel": e.findtext("yt:channelId", "", NS),
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

    videos.sort(key=lambda v: v["date"], reverse=True)
    recent = [v for v in videos if v["date"] >= YT_MIN_DATE]
    return recent, videos


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


TG_MEDIA_DIR = ROOT / "assets" / "tg"


def archive_media(pid, url, suffix=""):
    """Pull a post's image into the repo and hand back its path on the site.

    Telegram serves media from time-limited CDN links. The ones committed a
    week ago now 404, and the same picture comes back under a different
    address — so a post meant to keep one URL for good cannot point at them.
    Each image is fetched once and then lives here.

    Files already on disk are never re-fetched, so a build costs one request
    per genuinely new post, and on Netlify — where everything is already
    committed — usually none at all.
    """
    if not url:
        return ""
    name = f"{pid}{suffix}.jpg"
    dest = TG_MEDIA_DIR / name
    rel = f"assets/tg/{name}"
    if dest.exists():
        return rel
    if out_of_time():
        return ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"  media {pid}: {exc}")
        return ""
    # a truncated or error body is worse than no image at all
    if len(data) < 512 or not data.startswith(b"\xff\xd8"):
        return ""
    TG_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return rel


def archive_all_media(posts):
    """Localise every post's media, reporting how much was newly fetched."""
    fetched = 0
    for p in posts:
        if p["video_thumb"]:
            before = (TG_MEDIA_DIR / f"{p['id']}-v.jpg").exists()
            p["video_thumb"] = archive_media(p["id"], p["video_thumb"], "-v")
            fetched += 0 if before else bool(p["video_thumb"])
        elif p["photo"]:
            before = (TG_MEDIA_DIR / f"{p['id']}.jpg").exists()
            p["photo"] = archive_media(p["id"], p["photo"])
            fetched += 0 if before else bool(p["photo"])
    if fetched:
        print(f"  archived {fetched} new images")
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
    return archive_all_media(collected[:limit]), complete


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
# the blog: one permanent page per Telegram post
# --------------------------------------------------------------------------

BLOG_DIR = ROOT / "blog"
SLUG_FILE = BLOG_DIR / "slugs.json"
SLUG_MAX = 60


def post_plain(p):
    """The post as plain text, for titles, descriptions and slugs."""
    t = re.sub(r"<br\s*/?>", " ", p.get("text", ""))
    t = re.sub(r"<[^>]+>", "", t)
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def slugify(text, pid):
    words = [w for w in re.sub(r"[^a-z0-9]+", "-", text.lower()).split("-") if w]
    out = ""
    for w in words:
        if out and len(out) + 1 + len(w) > SLUG_MAX:
            break
        out = f"{out}-{w}" if out else w
    return out or f"post-{pid}"


def assign_slugs(posts):
    """Fix each post's address the first time we see it, and never move it.

    The slug comes from the opening words — that is what makes it readable and
    worth anything to a search engine. But Telegram posts can be edited after
    publication, and a slug that tracked the text would relocate the page and
    break every link ever made to it. So the mapping is written down once, in
    slugs.json, and honoured from then on regardless of what the text becomes.
    """
    try:
        ledger = json.loads(SLUG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ledger = {}

    taken = set(ledger.values())
    added = 0
    for p in posts:
        key = str(p["id"])
        if key in ledger:
            p["slug"] = ledger[key]
            continue
        base = slugify(post_plain(p)[:140], p["id"])
        slug, n = base, 2
        while slug in taken:
            slug = f"{base}-{n}"
            n += 1
        ledger[key] = p["slug"] = slug
        taken.add(slug)
        added += 1

    if added:
        BLOG_DIR.mkdir(parents=True, exist_ok=True)
        SLUG_FILE.write_text(
            json.dumps(ledger, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"  {added} new slugs recorded")
    return posts


def post_title(p, limit=65):
    """Telegram posts carry no title, so the opening sentence becomes one."""
    plain = post_plain(p)
    if not plain:
        return f"Post {p['id']}"
    cut = re.split(r"(?<=[.!?])\s", plain)[0]
    if len(cut) > limit:
        cut = cut[:limit].rsplit(" ", 1)[0] + "…"
    return cut


def post_body_excerpt(p, limit=120):
    """What comes after the sentence that became the title.

    The index prints both, and drawing them from the same opening sentence
    made every entry say itself twice.
    """
    plain = post_plain(p)
    head = re.split(r"(?<=[.!?])\s", plain)[0]
    rest = plain[len(head):].strip()
    src = rest or plain
    if len(src) <= limit:
        return src
    return src[:limit].rsplit(" ", 1)[0] + "…"


def post_summary(p, limit=155):
    plain = post_plain(p)
    if not plain:
        return "A post from the Naturally Artificial Telegram channel."
    if len(plain) <= limit:
        return plain
    return plain[:limit].rsplit(" ", 1)[0] + "…"


# Generated pages address everything from the site root: they live one folder
# down, and root-relative paths spare every template a ../ that would have to
# change the moment a page moves.
BLOG_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <meta name="description" content="{desc}">
  <link rel="canonical" href="{canonical}">
  <meta name="robots" content="index, follow, max-image-preview:large">
  <meta property="og:type" content="{og_type}">
  <meta property="og:site_name" content="Naturally Artificial">
  <meta property="og:locale" content="en_US">
  <meta property="og:url" content="{canonical}">
  <meta property="og:title" content="{title}">
  <meta property="og:description" content="{desc}">
  <meta property="og:image" content="{image}">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{title}">
  <meta name="twitter:description" content="{desc}">
  <meta name="twitter:image" content="{image}">
  <script type="application/ld+json">{jsonld}</script>
  <link rel="icon" href="/assets/favicon/favicon.ico" sizes="32x32">
  <link rel="icon" type="image/png" sizes="32x32" href="/assets/favicon/favicon-32.png">
  <link rel="icon" type="image/png" sizes="512x512" href="/assets/favicon/favicon-512.png">
  <link rel="apple-touch-icon" href="/assets/favicon/favicon-180.png">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;700&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/css/styles.css?v={v}">
  <link rel="stylesheet" href="/css/blog.css?v={v}">
</head>
<body id="top" class="blog-page">

  <header class="site-header site-header--dark">
    <a class="site-header__logo" href="/" aria-label="Naturally Artificial — home">
      <img src="/assets/logo.svg" alt="Naturally Artificial">
    </a>
    <nav class="site-header__nav" aria-label="Main navigation">
      <a href="/about">About</a>
      <a href="/show">Show</a>
      <a href="/blog/"{blog_current}>Blog</a>
      <a href="/speaking">Speaking</a>
    </nav>
    <a class="site-header__contact" href="/contact">Contact Sergey</a>
    <button class="nav-toggle" aria-label="Toggle navigation" aria-expanded="false"><span></span></button>
  </header>

  <main>
"""

BLOG_FOOT = """  </main>

  <footer class="site-footer">
    <div class="site-footer__inner">
      <div class="site-footer__top">
        <p class="site-footer__heading">Join the founder-to-founder<br>talks in AI era</p>
        <a class="site-footer__book" href="{yt}" target="_blank" rel="noopener">Subscribe on YouTube</a>
      </div>
      <hr class="site-footer__divider">
      <div class="site-footer__cols">
        <div class="site-footer__col">
          <p class="site-footer__col-title">Navigate</p>
          <ul>
            <li><a href="/show">About Naturally Artificial</a></li>
            <li><a href="/about">About Sergey Osipov</a></li>
            <li><a href="/blog/">Blog</a></li>
            <li><a href="/contact">Contact</a></li>
          </ul>
        </div>
        <div class="site-footer__col">
          <p class="site-footer__col-title">Naturally Artificial</p>
          <ul>
            <li><a href="https://t.me/naturally_artificial" target="_blank" rel="noopener">Telegram</a></li>
            <li><a href="{yt}" target="_blank" rel="noopener">YouTube</a></li>
            <li><a href="https://open.spotify.com/show/033U1gDnjee2ValRcN3vWX" target="_blank" rel="noopener">Spotify</a></li>
            <li><a href="https://www.linkedin.com/company/naturally-artificial/" target="_blank" rel="noopener">LinkedIn</a></li>
            <li><a href="https://www.instagram.com/naturally__artificial/" target="_blank" rel="noopener">Instagram</a></li>
          </ul>
        </div>
        <div class="site-footer__col">
          <p class="site-footer__col-title">Sergey Osipov</p>
          <ul>
            <li><a href="https://www.linkedin.com/in/osipov/" target="_blank" rel="noopener">LinkedIn</a></li>
            <li><a href="https://www.instagram.com/os_on_the_island/" target="_blank" rel="noopener">Instagram</a></li>
          </ul>
        </div>
        <div class="site-footer__col site-footer__col--right">
          <p class="site-footer__col-title">Links</p>
          <ul>
            <li><a href="/presskit">Press kit</a></li>
          </ul>
        </div>
      </div>
      <div class="site-footer__bottom">
        <p>Copyright © Joox LTD</p>
        <a class="back-to-top" href="#top">Back to top <img src="/assets/arrow-up.svg" alt=""></a>
        <span class="site-footer__legal">
          <a href="/privacy">Privacy Policy</a>
          <a href="/terms">Terms of Use</a>
        </span>
      </div>
    </div>
  </footer>

  <script src="/js/main.js?v={v}"></script>
</body>
</html>
"""


def post_media_html(p, lazy=True):
    load = ' loading="lazy"' if lazy else ""
    if p.get("video_thumb"):
        badge = (
            f'<span class="tg-post__duration">{html.escape(p["duration"])}</span>'
            if p.get("duration")
            else ""
        )
        return (
            f'<div class="tg-post__media tg-post__media--video">'
            f'<img src="/{p["video_thumb"]}" alt=""{load}>'
            f'<span class="tg-post__play" aria-hidden="true"></span>{badge}</div>'
        )
    if p.get("photo"):
        return f'<div class="tg-post__media"><img src="/{p["photo"]}" alt=""{load}></div>'
    return ""


def render_post_page(p, newer, older):
    title = post_title(p)
    desc = post_summary(p)
    canonical = f"{SITE_URL}/blog/{p['slug']}"
    media = p.get("photo") or p.get("video_thumb")
    image = f"{SITE_URL}/{media}" if media else f"{SITE_URL}{OG_IMAGE}"
    iso = p["date"].isoformat() if p.get("date") else ""

    data = {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "@id": f"{canonical}#post",
        "headline": title,
        "description": desc,
        "url": canonical,
        "mainEntityOfPage": canonical,
        "inLanguage": "en",
        "image": image,
        "author": {"@type": "Person", "name": "Sergey Osipov"},
        "publisher": {
            "@type": "Organization",
            "name": "Naturally Artificial",
            "url": f"{SITE_URL}/",
        },
        "isPartOf": {"@type": "Blog", "@id": f"{SITE_URL}/blog/#blog"},
    }
    if iso:
        data["datePublished"] = iso

    head = BLOG_HEAD.format(
        title=html.escape(f"{title} — Naturally Artificial", quote=True),
        desc=html.escape(desc, quote=True),
        canonical=canonical,
        og_type="article",
        image=image,
        jsonld=json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        v=ASSET_V,
        blog_current=' class="active" aria-current="page"',
    )

    stamp = (
        f'<time datetime="{iso}">{p["date"].strftime("%d %B %Y")}</time>'
        if p.get("date")
        else ""
    )
    views = (
        f'<span class="post-views">{html.escape(p["views"])} views</span>'
        if p.get("views")
        else ""
    )

    nav = []
    if older:
        nav.append(
            f'<a class="post-nav__prev" href="/blog/{older["slug"]}">'
            f'<span>Older</span>{html.escape(post_title(older, 48))}</a>'
        )
    if newer:
        nav.append(
            f'<a class="post-nav__next" href="/blog/{newer["slug"]}">'
            f'<span>Newer</span>{html.escape(post_title(newer, 48))}</a>'
        )

    body = f"""    <article class="post container">
      <p class="post__back"><a href="/blog/">All posts</a></p>
      <h1 class="post__title">{html.escape(title)}</h1>
      <p class="post__meta">{stamp}{views}</p>
      {post_media_html(p, lazy=False)}
      <div class="post__text">{p.get("text", "")}</div>
      <p class="post__source"><a href="{p['url']}" target="_blank" rel="noopener">Open in Telegram</a></p>
    </article>

    <nav class="post-nav container" aria-label="More posts">
      {"".join(nav)}
    </nav>
"""
    return head + body + BLOG_FOOT.format(yt=YT_HANDLE, v=ASSET_V)


def render_blog_index(posts):
    canonical = f"{SITE_URL}/blog/"
    desc = (
        "Every post from the Naturally Artificial Telegram channel — on AI "
        "disruption, roll-ups, raising capital in the EU, tech hubs and relocation."
    )
    data = {
        "@context": "https://schema.org",
        "@type": "Blog",
        "@id": f"{canonical}#blog",
        "name": "Naturally Artificial",
        "url": canonical,
        "description": desc,
        "inLanguage": "en",
        "publisher": {
            "@type": "Organization",
            "name": "Naturally Artificial",
            "url": f"{SITE_URL}/",
        },
    }
    head = BLOG_HEAD.format(
        title="Blog — Naturally Artificial",
        desc=html.escape(desc, quote=True),
        canonical=canonical,
        og_type="website",
        image=f"{SITE_URL}{OG_IMAGE}",
        jsonld=json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        v=ASSET_V,
        blog_current=' class="active" aria-current="page"',
    )

    # every entry is a real link in the markup — the batching below is for
    # readers, never for crawlers
    items = []
    for i, p in enumerate(posts):
        stamp = (
            f'<time datetime="{p["date"].isoformat()}">{p["date"].strftime("%d %b %Y")}</time>'
            if p.get("date")
            else ""
        )
        hidden = " blog-item--hidden" if i >= 30 else ""
        items.append(
            f'        <li class="blog-item{hidden}">\n'
            f'          <a href="/blog/{p["slug"]}">\n'
            f'            <h2 class="blog-item__title">{html.escape(post_title(p, 90))}</h2>\n'
            f'            <p class="blog-item__excerpt">{html.escape(post_body_excerpt(p))}</p>\n'
            f"          </a>\n"
            f'          <p class="blog-item__meta">{stamp}</p>\n'
            f"        </li>"
        )

    body = f"""    <section class="blog-head container">
      <p class="blog-head__label">Blog</p>
      <h1 class="blog-head__title">Everything from the channel</h1>
      <p class="blog-head__sub">{html.escape(desc)}</p>
    </section>

    <section class="blog-list container">
      <ol class="blog-items" id="blog-items">
{chr(10).join(items)}
      </ol>
      <button class="blog-more" id="blog-more" type="button">Show more posts</button>
    </section>
"""
    return head + body + BLOG_FOOT.format(yt=YT_HANDLE, v=ASSET_V)


def render_home_posts(posts, count=5):
    """The five newest posts on the home page, linking into the blog rather
    than out to Telegram — the point of the blog is that the site keeps them."""
    out = []
    for p in posts[:count]:
        stamp = (
            f'<time datetime="{p["date"].isoformat()}">{p["date"].strftime("%d %b %Y")}</time>'
            if p.get("date")
            else ""
        )
        out.append(
            f'        <li class="home-post">\n'
            f'          <a href="blog/{p["slug"]}.html">\n'
            f'            <h3 class="home-post__title">{html.escape(post_title(p, 90))}</h3>\n'
            f'            <p class="home-post__excerpt">{html.escape(post_body_excerpt(p, 140))}</p>\n'
            f"          </a>\n"
            f'          <p class="home-post__meta">{stamp}</p>\n'
            f"        </li>"
        )
    return "\n".join(out)


def build_blog(posts):
    """One file per post, plus the index. Files for posts that no longer exist
    are removed, so a deleted Telegram post does not linger as a stale page."""
    BLOG_DIR.mkdir(parents=True, exist_ok=True)
    ordered = sorted(posts, key=lambda p: p["id"], reverse=True)

    for i, p in enumerate(ordered):
        newer = ordered[i - 1] if i > 0 else None
        older = ordered[i + 1] if i + 1 < len(ordered) else None
        (BLOG_DIR / f"{p['slug']}.html").write_text(
            render_post_page(p, newer, older), encoding="utf-8"
        )

    # The index lives inside the folder, not beside it: a blog.html sitting
    # next to a blog/ directory leaves the server to guess which one /blog
    # means, and it guessed a redirect.
    (BLOG_DIR / "index.html").write_text(render_blog_index(ordered), encoding="utf-8")

    keep = {f"{p['slug']}.html" for p in ordered} | {"slugs.json", "index.html"}
    removed = 0
    for f in BLOG_DIR.iterdir():
        if f.name not in keep:
            f.unlink()
            removed += 1
    print(f"  wrote {len(ordered)} post pages + blog/index.html"
          f"{f', removed {removed} stale' if removed else ''}")
    return ordered


# --------------------------------------------------------------------------
# press kit numbers
# --------------------------------------------------------------------------

TG_ABOUT = f"https://t.me/{TG_CHANNEL}"


def telegram_subscribers():
    try:
        page = get(TG_ABOUT, timeout=12)
    except (urllib.error.URLError, TimeoutError):
        return ""
    m = re.search(r'tgme_page_extra">([\d\s ,\.]+?)\s*subscribers?<', page)
    return re.sub(r"[\s ]", ",", m.group(1).strip()) if m else ""


def show_stats(videos):
    """What the show itself does, as opposed to what the channel does.

    Only the channel's own long-form videos count here. The median leads
    because two outliers pull the mean far above a typical episode, and a
    sponsor who is quoted the mean finds that out on their first report.
    """
    own = [v for v in videos if v["channel"] == YT_CHANNEL_ID and v["views_n"]]
    if not own:
        return {}
    counts = sorted(v["views_n"] for v in own)
    mid = len(counts) // 2
    median = counts[mid] if len(counts) % 2 else (counts[mid - 1] + counts[mid]) // 2

    # Cadence over the current run, not over the channel's whole life: the
    # early months were sporadic, and averaging them in reports a slower show
    # than the one a partner would actually be joining.
    current = [v for v in own if v["date"] >= YT_MIN_DATE] or own
    span_days = (max(v["date"] for v in current) - min(v["date"] for v in current)).days
    per_month = len(current) / (span_days / 30.44) if span_days > 30 else len(current)
    return {
        "episodes": len(own),
        "views": f"{sum(counts):,}",
        "median": f"{median:,}",
        "mean": f"{sum(counts) // len(counts):,}",
        "per_month": f"{per_month:.1f}".rstrip("0").rstrip("."),
    }


def render_stats(videos):
    show = show_stats(videos)
    tg = telegram_subscribers()
    if not show:
        return ""

    # Deliberately not the channel's subscriber count. Almost all of it was
    # bought through ad campaigns and it churns; quoting it to a sponsor would
    # be quoting a number that falls apart on their first report. What is left
    # here is what the show genuinely does.
    # The cadence is stated, not measured: it is the rhythm the show commits
    # to, and a rolling average would swing with every gap and double week.
    cards = [
        (str(show["episodes"]), "long-form episodes"),
        (show["mean"], "mean views per episode"),
        ("1–2", "new episodes a month"),
    ]
    if tg:
        cards.append((tg, "Telegram subscribers"))

    out = [
        '        <div class="pk-stats">',
    ]
    for value, label in cards:
        out.append('          <div class="pk-stat">')
        out.append(f'            <p class="pk-stat__num">{html.escape(value)}</p>')
        out.append(f'            <p class="pk-stat__label">{html.escape(label)}</p>')
        out.append("          </div>")
    out.append("        </div>")
    out.append(
        '        <p class="pk-note">The channel only started running properly '
        "in January 2025, so these are the numbers from its first months.</p>"
    )
    return "\n".join(out)


# --------------------------------------------------------------------------
# search engines and social cards
# --------------------------------------------------------------------------

OG_ALT = "Naturally Artificial — founder-to-founder talks in the AI era"

SOCIAL = [
    YT_HANDLE,
    "https://t.me/naturally_artificial",
    "https://open.spotify.com/show/033U1gDnjee2ValRcN3vWX",
    "https://www.linkedin.com/in/osipov/",
    "https://www.linkedin.com/company/naturally-artificial/",
    "https://www.instagram.com/naturally__artificial/",
]


def page_meta(path):
    """The title and description the page already carries in its head.

    Reading them back rather than keeping a second copy here means the two
    can never drift apart.
    """
    src = (ROOT / path).read_text(encoding="utf-8")
    title = re.search(r"<title>(.*?)</title>", src, re.S)
    desc = re.search(r'<meta name="description" content="(.*?)">', src, re.S)
    if not title or not desc:
        sys.exit(f"{path}: no <title> or no description to build the social card from")
    return html.unescape(title.group(1).strip()), html.unescape(desc.group(1).strip())


def structured_data(path, title, desc):
    """schema.org JSON-LD, only where there is something real to describe."""
    if path == "index.html":
        return {
            "@context": "https://schema.org",
            "@graph": [
                {
                    "@type": "WebSite",
                    "@id": f"{SITE_URL}/#website",
                    "url": f"{SITE_URL}/",
                    "name": "Naturally Artificial",
                    "inLanguage": "en",
                    "publisher": {"@id": f"{SITE_URL}/#person"},
                },
                {
                    "@type": "Person",
                    "@id": f"{SITE_URL}/#person",
                    "name": "Sergey Osipov",
                    "url": f"{SITE_URL}/",
                    "jobTitle": "Founder and early-stage investor",
                    "description": desc,
                    "image": f"{SITE_URL}{OG_IMAGE}",
                    "sameAs": SOCIAL,
                },
            ],
        }
    if path == "show.html":
        return {
            "@context": "https://schema.org",
            "@type": "PodcastSeries",
            "@id": f"{SITE_URL}/show#podcast",
            "name": "Naturally Artificial",
            "url": f"{SITE_URL}/show",
            "description": desc,
            "inLanguage": "en",
            "image": f"{SITE_URL}{OG_IMAGE}",
            "webFeed": YT_FEED,
            "author": {"@type": "Person", "name": "Sergey Osipov"},
        }
    return None


def seo_block(path):
    title, desc = page_meta(path)
    url = SITE_URL + PAGE_PATHS[path]
    t, d = html.escape(title, quote=True), html.escape(desc, quote=True)

    tags = [
        f'<link rel="canonical" href="{url}">',
        '<meta name="robots" content="index, follow, max-image-preview:large">',
        '<meta property="og:type" content="website">',
        '<meta property="og:site_name" content="Naturally Artificial">',
        '<meta property="og:locale" content="en_US">',
        f'<meta property="og:url" content="{url}">',
        f'<meta property="og:title" content="{t}">',
        f'<meta property="og:description" content="{d}">',
        f'<meta property="og:image" content="{SITE_URL}{OG_IMAGE}">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        f'<meta property="og:image:alt" content="{OG_ALT}">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{t}">',
        f'<meta name="twitter:description" content="{d}">',
        f'<meta name="twitter:image" content="{SITE_URL}{OG_IMAGE}">',
    ]

    data = structured_data(path, title, desc)
    if data:
        tags.append(
            '<script type="application/ld+json">'
            + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            + "</script>"
        )
    return "\n".join("  " + tag for tag in tags)


def git_dates():
    """Last commit date per page, when the checkout can actually tell us.

    Netlify clones shallow, and there every file looks as though it changed in
    the one commit that exists — a sitemap claiming every page changed today,
    on every single deploy, is worse than one with no dates at all. So when the
    clone is shallow we say nothing.
    """
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--is-shallow-repository"],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        if probe.returncode != 0 or probe.stdout.strip() != "false":
            return {}
        dates = {}
        for page in PAGE_PATHS:
            r = subprocess.run(
                ["git", "log", "-1", "--format=%cs", "--", page],
                cwd=ROOT, capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                dates[page] = r.stdout.strip()
        return dates
    except (OSError, subprocess.SubprocessError):
        return {}


def build_search_files(posts=None):
    (ROOT / "robots.txt").write_text(
        "# Everything here is meant to be found.\n"
        "User-agent: *\n"
        "Allow: /\n"
        "\n"
        f"Sitemap: {SITE_URL}/sitemap.xml\n",
        encoding="utf-8",
    )
    print("  wrote robots.txt")

    dates = git_dates()
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for page, path in PAGE_PATHS.items():
        lines.append("  <url>")
        lines.append(f"    <loc>{SITE_URL}{path}</loc>")
        if page in dates:
            lines.append(f"    <lastmod>{dates[page]}</lastmod>")
        priority = "1.0" if page == "index.html" else "0.3" if page in LOW_PRIORITY else "0.8"
        lines.append(f"    <priority>{priority}</priority>")
        lines.append("  </url>")

    # The blog index and every post. Posts carry their own publication date,
    # which is a lastmod we can actually vouch for.
    if posts:
        lines.append("  <url>")
        lines.append(f"    <loc>{SITE_URL}/blog/</loc>")
        lines.append("    <priority>0.8</priority>")
        lines.append("  </url>")
        for p in sorted(posts, key=lambda x: x["id"], reverse=True):
            if not p.get("slug"):
                continue
            lines.append("  <url>")
            lines.append(f"    <loc>{SITE_URL}/blog/{p['slug']}</loc>")
            if p.get("date"):
                lines.append(f"    <lastmod>{p['date'].date().isoformat()}</lastmod>")
            lines.append("    <priority>0.6</priority>")
            lines.append("  </url>")

    lines.append("</urlset>")
    (ROOT / "sitemap.xml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  wrote sitemap.xml ({len(PAGE_PATHS)} urls"
          f"{'' if dates else ', no dates — shallow clone'})")


def build_seo():
    for page in PAGE_PATHS:
        inject_between(page, "SEO", seo_block(page), indent="  ")
    build_search_files()


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


def inject_between(path, marker, content, indent=""):
    f = ROOT / path
    src = f.read_text(encoding="utf-8")
    start, end = f"<!-- {marker}:start -->", f"<!-- {marker}:end -->"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)
    if not pattern.search(src):
        sys.exit(f"marker {marker} not found in {path}")
    f.write_text(
        pattern.sub(lambda _: f"{start}\n{content}\n{indent}{end}", src),
        encoding="utf-8",
    )


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
    # First, because it needs no network: a feed outage must not cost the
    # site its canonical links or its sitemap.
    print("Search metadata…")
    build_seo()

    print("YouTube…")
    videos, all_videos = fetch_youtube()
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
            "\n".join(render_episode(v) for v in videos[:3]),
        )
        inject(
            "show.html",
            "episodes",
            "\n".join(render_episode(v) for v in videos),
        )

    if all_videos:
        print("Press kit numbers…")
        stats = render_stats(all_videos)
        if stats:
            inject("presskit.html", "stats", stats)
        else:
            print("  incomplete — keeping the committed numbers")

    if posts and tg_complete:
        print("Blog…")
        ordered = build_blog(assign_slugs(posts))
        build_search_files(posts)
        inject("index.html", "home-posts", render_home_posts(ordered))


    print("done")


if __name__ == "__main__":
    main()
