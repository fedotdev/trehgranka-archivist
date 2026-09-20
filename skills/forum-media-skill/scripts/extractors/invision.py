#!/usr/bin/env python3
"""Deterministic Invision Community (IPS) forum extractor — stdlib only.

Parses a saved topic page and returns posts, authors, profile URLs (recorded
only, never followed), dates, quotes, reactions, attachments, in-post images
and external photo hotlinks. Pattern-verified against real IPS pages
(article#elComment_*, ul.ipsPagination, data-role="commentContent").

IPS specifics handled here:
- images are lazy-loaded: <img src="...spacer.png" data-src="REAL"> — the real
  URL lives in data-src (also picked up from srcset when present);
- attachments are <a class="ipsAttachLink ..." href="...">, often wrapping the
  same lazy image;
- quotes are <blockquote data-ipsquote="..." class="ipsQuote">, possibly nested;
- pagination: <link rel="next|last"> in <head>, ul.ipsPagination data-pages=N,
  /page/N/ paths (or ?page=N);
- external photo hotlinks (radikal.ru, imageshack, photofile, ...) are plain
  <a href="...jpg"> anchors without an <img> child and on non-forum hosts.

Usage:
    python scripts/extractors/invision.py --html topic_page_01.html [--page-url URL]
    python scripts/extractors/invision.py --probe
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.parse
from pathlib import Path

TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)((?:\s+[^<>]*?)?)(/?)>", re.S)
ATTR_RE = re.compile(r"""([^\s=<>"'/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?""")
TAG_STRIP_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
IMAGE_EXT_RE = re.compile(r"\.(jpe?g|png|gif)(?:[?#]|$)", re.I)
ARTICLE_RE = re.compile(r"elComment_(\d+)")
ELCOMMENT_CLS_RE = re.compile(
    r"<article\b[^>]*\bid=[\"']elComment_\d+[\"'][^>]*\bclass=[\"'][^\"']*\bcPost\b[^\"']*\bipsComment\b",
    re.I,
)

# Known object-storage/CDN hosts used by IPS boards for static assets; links
# pointing there are service chrome, not user-posted external photos.
CLOUD_STORAGE_SUFFIXES = (
    ".yandexcloud.net",
    ".cloudfront.net",
    ".amazonaws.com",
    ".cloudflarestorage.com",
    ".r2.cloudflarestorage.com",
)

# Tags whose raw contents are never markup (JS/JSON can contain fake tags).
IGNORE_CONTENT_TAGS = {"script", "style", "noscript", "textarea"}


# --------------------------------------------------------------------------- tags


def _attrs(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in ATTR_RE.finditer(text or ""):
        val = m.group(2)
        if val is None:
            val = m.group(3)
        if val is None:
            val = m.group(4)
        out[m.group(1).lower()] = html.unescape(val or "")
    return out


def _tags(html_text: str):
    """Yield (start, end, name, attrs, is_close, self_close) for every tag.

    Contents of script/style/noscript/textarea are suppressed so embedded JSON
    or JS strings that look like markup cannot confuse the extractor.
    """
    ignoring: str | None = None
    depth = 0
    for m in TAG_RE.finditer(html_text):
        is_close = bool(m.group(1))
        name = (m.group(2) or "").lower()
        self_close = bool(m.group(4))
        if self_close:
            if ignoring is None:
                yield m.start(), m.end(), name, _attrs(m.group(3) or ""), False, True
            continue
        if ignoring is not None:
            if not is_close and name == ignoring:
                depth += 1
            elif is_close and name == ignoring:
                depth -= 1
                if depth <= 0:
                    ignoring = None
            continue
        if not is_close:
            attrs = _attrs(m.group(3) or "")
            if name in IGNORE_CONTENT_TAGS:
                ignoring = name
                depth = 1
                continue
            yield m.start(), m.end(), name, attrs, False, False
        else:
            yield m.start(), m.end(), name, {}, True, False


def _find_close(events: list, open_idx: int, name: str) -> int:
    """Index of the matching closing tag, or len(events) when unmatched."""
    depth = 0
    for k in range(open_idx, len(events)):
        _, _, ev_name, _, is_close, _ = events[k]
        if ev_name != name:
            continue
        if is_close:
            depth -= 1
            if depth == 0:
                return k
        else:
            depth += 1
    return len(events)


def _slice_html(html_text: str, events: list, start_idx: int, end_idx: int) -> str:
    """Inner HTML of events[start_idx]..events[end_idx] (tag positions)."""
    lo = events[start_idx][1]
    hi = events[end_idx][0] if end_idx < len(events) else len(html_text)
    return html_text[lo:hi] if hi > lo else ""


def _clean_text(raw: str) -> str:
    text = TAG_STRIP_RE.sub(" ", raw or "")
    text = html.unescape(text)
    text = WS_RE.sub(" ", text)
    return text.strip()


def _abs(page_url: str, href: str) -> str:
    return urllib.parse.urljoin(page_url, href)


def _host_root(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")


# --------------------------------------------------------------------------- detect


def detect(html_text: str) -> dict:
    """Detect Invision Community (IPS) from DOM markers.

    Returns {"name": ..., "confidence": "high"|"medium"|"low", "evidence": [...]}.
    The URL marker is read from the page itself (canonical/pagination hrefs),
    so the signature stays detect(html) per the skill contract.
    """
    evidence: list[str] = []
    if ELCOMMENT_CLS_RE.search(html_text):
        evidence.append("article#elComment_* with classes cPost,ipsComment")
    if "ipsPagination" in html_text or "data-ipsPagination" in html_text:
        evidence.append("ipsPagination / data-ipsPagination")
    if "core.front.core.comment" in html_text:
        evidence.append("data-controller=core.front.core.comment")
    if "/forums/topic/" in html_text:
        evidence.append("URL pattern: /forums/topic/")

    structural = [e for e in evidence if not e.startswith("URL pattern")]
    if len(structural) >= 2:
        return {"name": "invision-community-ips", "confidence": "high", "evidence": evidence}
    if structural or evidence:
        return {"name": "invision-community-ips", "confidence": "medium", "evidence": evidence}
    return {"name": "unknown", "confidence": "low", "evidence": []}


# ------------------------------------------------------------------------- pagination


def extract_pagination(html_text: str, page_url: str = "") -> dict:
    """Current page, next/last page URLs and total page count.

    current: from /page/N/ path or ?page=N/&page=N query (default 1).
    next/last: <link rel="next|last"> plus the in-page pagination anchors.
    total_pages: data-pages / data-ipsPagination-pages on ul.ipsPagination,
    falling back to the highest page number rendered in the pagination block.
    """
    events = list(_tags(html_text))
    current = 1
    m = re.search(r"/page/(\d+)", page_url)
    if m:
        current = int(m.group(1))
    else:
        m = re.search(r"[?&]page=(\d+)", page_url)
        if m:
            current = int(m.group(1))

    next_url: str | None = None
    last_url: str | None = None
    total_pages: int | None = None
    pagination_idx: int | None = None
    for k, (_, _, name, attrs, is_close, _) in enumerate(events):
        if (
            not is_close
            and name == "ul"
            and "ipsPagination" in attrs.get("class", "")
            and pagination_idx is None
        ):
            pagination_idx = k
        if name not in ("link", "a") or not attrs.get("href"):
            continue
        rels = attrs.get("rel", "").split()
        if "next" in rels and next_url is None:
            next_url = _abs(page_url, attrs["href"])
        if "last" in rels and last_url is None:
            last_url = _abs(page_url, attrs["href"])

    if pagination_idx is not None:
        pag_attrs = events[pagination_idx][3]
        for key in ("data-pages", "data-ipspagination-pages"):
            val = pag_attrs.get(key)
            if val and val.isdigit():
                total_pages = int(val)
                break
        if total_pages is None:
            block = _slice_html(html_text, events, pagination_idx, _find_close(events, pagination_idx, "ul"))
            nums = [int(n) for n in re.findall(r"data-page=[\"'](\d+)[\"']", block)]
            if nums:
                total_pages = max(nums)
    return {"current": current, "next": next_url, "last": last_url, "total_pages": total_pages}


# ---------------------------------------------------------------------------- posts


def extract_posts(html_text: str, page_url: str = "") -> list[dict]:
    """Extract every article#elComment_* post from an IPS topic page."""
    events = list(_tags(html_text))
    posts: list[dict] = []
    for k, (_, _, name, attrs, is_close, _) in enumerate(events):
        if is_close or name != "article":
            continue
        cls = attrs.get("class", "")
        if "cPost" not in cls or "ipsComment" not in cls:
            continue
        mm = ARTICLE_RE.search(attrs.get("id", ""))
        if not mm:
            continue
        close_idx = _find_close(events, k, "article")
        posts.append(_extract_post(events, k, close_idx, int(mm.group(1)), html_text, page_url))
    return posts


def _extract_post(events: list, open_idx: int, close_idx: int, post_id: int, html_text: str, page_url: str) -> dict:
    author = ""
    author_fallback = ""
    profile_url: str | None = None
    date: str | None = None
    content_open = -1

    for k in range(open_idx + 1, close_idx):
        _, _, name, attrs, is_close, _ = events[k]
        if is_close:
            continue
        if name in ("h2", "h3") and "cAuthorPane_author" in attrs.get("class", ""):
            if not author:
                author = _clean_text(_slice_html(html_text, events, k, _find_close(events, k, name)))
        elif name == "span" and "ipsUserPhoto" in attrs.get("class", ""):
            span_close = _find_close(events, k, "span")
            for j in range(k + 1, min(span_close, close_idx)):
                if events[j][2] == "img" and not events[j][4]:
                    alt = events[j][3].get("alt", "")
                    if alt and not author_fallback:
                        author_fallback = alt
                    break
        elif name == "a" and attrs.get("href", "").find("/profile/") != -1 and profile_url is None:
            profile_url = _abs(page_url, re.sub(r"/badges/?$", "/", attrs["href"]))
        elif name == "time" and date is None:
            date = attrs.get("datetime") or _clean_text(_slice_html(html_text, events, k, _find_close(events, k, "time")))
        elif name == "div" and attrs.get("data-role") == "commentContent" and content_open < 0:
            content_open = k

    if not author:
        author = author_fallback

    if content_open < 0:
        return {
            "post_id": post_id,
            "author": author,
            "profile_url": profile_url,
            "date": date,
            "text": "",
            "quotes": [],
            "reactions": [],
            "attachments": [],
            "images": [],
        }

    content_close = _find_close(events, content_open, "div")
    text = _clean_text(_slice_html(html_text, events, content_open, content_close))
    quotes = _extract_quotes(events, content_open, content_close, html_text)
    reactions = _extract_reactions(events, content_open, content_close, html_text)
    attachments = _extract_attachments(events, content_open, content_close, html_text, page_url)
    images = _extract_images(events, content_open, content_close, html_text, page_url)
    return {
        "post_id": post_id,
        "author": author,
        "profile_url": profile_url,
        "date": date,
        "text": text,
        "quotes": quotes,
        "reactions": reactions,
        "attachments": attachments,
        "images": images,
    }


def _extract_quotes(events: list, lo: int, hi: int, html_text: str) -> list[str]:
    quotes: list[str] = []
    for k in range(lo + 1, hi):
        _, _, name, attrs, is_close, _ = events[k]
        if is_close or name != "blockquote" or "data-ipsquote" not in attrs:
            continue
        quotes.append(_clean_text(_slice_html(html_text, events, k, _find_close(events, k, "blockquote"))))
    return quotes


def _extract_reactions(events: list, lo: int, hi: int, html_text: str) -> list[int]:
    reactions: list[int] = []
    for k in range(lo + 1, hi):
        _, _, name, attrs, is_close, _ = events[k]
        if is_close or name != "span" or attrs.get("data-role") != "reactionCount":
            continue
        inner = _clean_text(_slice_html(html_text, events, k, _find_close(events, k, "span")))
        digits = re.sub(r"[^0-9]", "", inner)
        if digits:
            reactions.append(int(digits))
    return reactions


def _extract_attachments(events: list, lo: int, hi: int, html_text: str, page_url: str) -> list[dict]:
    attachments: list[dict] = []
    for k in range(lo + 1, hi):
        _, _, name, attrs, is_close, _ = events[k]
        if is_close or name != "a" or "ipsAttachLink" not in attrs.get("class", ""):
            continue
        href = attrs.get("href", "")
        if not href:
            continue
        url = _abs(page_url, href)
        close_idx = _find_close(events, k, "a")
        label = _clean_text(_slice_html(html_text, events, k, close_idx))
        if not label:
            for j in range(k + 1, min(close_idx, hi)):
                if events[j][2] == "img" and not events[j][4]:
                    label = events[j][3].get("alt", "") or ""
                    break
        if not label:
            label = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
        attachments.append({"url": url, "label": label})
    return attachments


def _img_url_ok(candidate: str) -> bool:
    stripped = candidate.strip()
    if stripped.lower().startswith("data:"):
        return False
    return "spacer" not in urllib.parse.urlsplit(stripped).path.lower()


def _srcset_best(srcset: str) -> str | None:
    """Highest-density candidate from a srcset list (last entry wins)."""
    urls = [part.strip().split(" ", 1)[0] for part in srcset.split(",") if part.strip()]
    return urls[-1] if urls else None


def _extract_images(events: list, lo: int, hi: int, html_text: str, page_url: str) -> list[dict]:
    images: list[dict] = []
    seen: set[str] = set()
    for k in range(lo + 1, hi):
        _, _, name, attrs, is_close, _ = events[k]
        if is_close or name != "img":
            continue
        candidate = None
        source = "src"
        data_src = attrs.get("data-src")
        src = attrs.get("src")
        srcset = attrs.get("srcset")
        if data_src and _img_url_ok(data_src):
            candidate, source = data_src, "data-src"
        elif src and _img_url_ok(src):
            candidate, source = src, "src"
        elif srcset:
            best = _srcset_best(srcset)
            if best and _img_url_ok(best):
                candidate, source = best, "srcset"
        if not candidate:
            continue
        url = _abs(page_url, candidate)
        if url in seen:
            continue
        seen.add(url)
        images.append({"url": url, "label": attrs.get("alt"), "source": source})
    return images


# ------------------------------------------------------------- external hotlinks


def extract_external_images(html_text: str, page_url: str = "") -> list[str]:
    """Plain <a href="...jpg|png|gif"> anchors NOT wrapping an <img> and not on
    the forum's own hosts/cloud storage — external photo hotlinks to try and
    recover like ordinary media."""
    events = list(_tags(html_text))
    base_host = _host_root(page_url)
    result: list[str] = []
    seen: set[str] = set()
    for k, (_, _, name, attrs, is_close, _) in enumerate(events):
        if is_close or name != "a":
            continue
        href = attrs.get("href", "")
        if not IMAGE_EXT_RE.search(href):
            continue
        if "ipsAttachLink" in attrs.get("class", ""):
            continue
        close_idx = _find_close(events, k, "a")
        if any(events[j][2] == "img" and not events[j][4] for j in range(k + 1, close_idx)):
            continue
        url = _abs(page_url, href)
        host = urllib.parse.urlparse(url).netloc.lower()
        if not host:
            continue
        if host == base_host or host.endswith("." + base_host):
            continue
        if any(host.endswith(s) for s in CLOUD_STORAGE_SUFFIXES):
            continue
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


# ---------------------------------------------------------------------- self-check


_PROBE_PAGE_URL = "https://forum.example.org/forums/topic/42-demo/"

PROBE_HTML = """<html><head>
<link rel="next" href="/forums/topic/42-demo/page/2/"/>
<link rel="last" href="/forums/topic/42-demo/page/9/"/>
</head><body>
<ul class='ipsPagination' data-pages='9' data-ipsPagination-pages='9'><li class='ipsPagination_page'></li></ul>
<article id="elComment_100" class="cPost ipsBox ipsComment ipsComment_parent">
 <div class="cAuthorPane_mobile">
  <div class="cAuthorPane_photoWrap"><span class='ipsUserPhoto ipsUserPhoto_large '><img src='data:image/svg+xml,XX' alt='demo_user'></span></div>
  <h3 class="ipsType_sectionHead cAuthorPane_author">demo_user</h3>
  <a href="https://forum.example.org/profile/7-demo_user/badges/" rel="nofollow">badges</a>
 </div>
 <aside class="ipsComment_author cAuthorPane"><h3 class="ipsType_reset cAuthorPane_author"><strong>demo_user</strong></h3><time datetime="2019-05-04T12:34:56Z">4 мая, 2019</time></aside>
 <div class="ipsColumn ipsColumn_fluid ipsMargin:none">
  <div data-role="commentContent" class="ipsType_normal ipsType_richText">
   <blockquote data-ipsquote="" class="ipsQuote" data-ipsquote-username="alice"><div>цитируемый текст</div></blockquote>
   <p>основной текст <img alt="фото 1" src="//forum.example.org/applications/core/interface/js/spacer.png" data-src="https://forum.example.org/uploads/monthly_01_2021/photo_1.jpg" class="ipsImage"></p>
   <p><a class="ipsAttachLink ipsAttachLink_image" href="https://forum.example.org/uploads/post-5-1170000000.jpg"><img alt="attachment.jpg" src="//forum.example.org/applications/core/interface/js/spacer.png" data-src="https://forum.example.org/uploads/post-5-1170000000.jpg"></a></p>
   <span data-role="reactionCount">7</span>
   <p><a href="https://radikal.ru/photo1.jpg">hotlink</a></p>
  </div>
 </div>
</article>
</body></html>
"""


def _probe() -> None:
    """Synthetic off-network self-check: 8-10 lines of IPS-like markup."""
    posts = extract_posts(PROBE_HTML, _PROBE_PAGE_URL)
    assert posts, "no posts extracted from probe fixture"
    assert len(posts) == 1, f"expected 1 post, got {len(posts)}"
    post = posts[0]
    assert post["post_id"] == 100, f"post_id mismatch: {post['post_id']}"
    assert post["author"] == "demo_user", f"author mismatch: {post['author']!r}"
    assert post["profile_url"] == "https://forum.example.org/profile/7-demo_user/", post["profile_url"]
    assert post["quotes"] and "цитируемый текст" in post["quotes"][0], f"quotes: {post['quotes']}"
    assert "основной текст" in post["text"], f"text: {post['text'][:80]!r}"
    assert post["reactions"] == [7], f"reactions: {post['reactions']}"
    assert post["attachments"] and post["attachments"][0]["url"] == "https://forum.example.org/uploads/post-5-1170000000.jpg", post["attachments"]
    assert post["attachments"][0]["label"] == "attachment.jpg", post["attachments"]
    assert post["images"], "no images extracted"
    img = post["images"][0]
    assert img["url"] == "https://forum.example.org/uploads/monthly_01_2021/photo_1.jpg", img
    assert img["source"] == "data-src", f"image source should be data-src, got {img['source']}"
    assert "data:image" not in " ".join(i["url"] for i in post["images"]), "data: URI leaked into images"

    external = extract_external_images(PROBE_HTML, _PROBE_PAGE_URL)
    assert "https://radikal.ru/photo1.jpg" in external, external
    assert "https://forum.example.org/uploads/post-5-1170000000.jpg" not in external, (
        "attachment must not be listed as an external hotlink"
    )

    pagination = extract_pagination(PROBE_HTML, _PROBE_PAGE_URL)
    assert pagination["total_pages"] == 9, pagination
    assert pagination["current"] == 1, pagination
    assert (pagination["next"] or "").endswith("/page/2/"), pagination
    assert (pagination["last"] or "").endswith("/page/9/"), pagination

    result = detect(PROBE_HTML)
    assert result["name"] == "invision-community-ips", result
    assert result["confidence"] == "high", result

    pag2 = extract_pagination(PROBE_HTML, "https://forum.example.org/forums/topic/42-demo/page/3/")
    assert pag2["current"] == 3, pag2


# ---------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Invision Community (IPS) page extractor")
    parser.add_argument("--html", help="saved topic page HTML file")
    parser.add_argument("--page-url", default="", help="URL of the page (for absolute links)")
    parser.add_argument("--probe", action="store_true", help="run the off-network self-check")
    args = parser.parse_args(argv)

    if args.probe:
        _probe()
        print("probe OK")
        return 0
    if not args.html:
        parser.error("--html is required unless --probe is given")
    html_text = Path(args.html).read_text(encoding="utf-8", errors="replace")
    for post in extract_posts(html_text, args.page_url):
        sys.stdout.write(json.dumps(post, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())