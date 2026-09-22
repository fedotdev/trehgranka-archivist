#!/usr/bin/env python3
"""
link_rewriter.py — rewrite archived HTML/CSS links to local paths.

WAYBACK_PRIMARY_SITE_MODE reconstruction: after raw archived artefacts are
stored under `raw/`, validated pages are copied into `site/` with every
internal link rewritten to its local path (never left pointing at
web.archive.org). Rules:

  * a raw link is resolved against the page base (original URL or <base href>);
  * Wayback-rewritten absolute links are restored to their original URL first;
  * a resolved original that exists in the local link map -> local relative
    path (fragment preserved, query considered);
  * a resolved original that was NOT archived -> left untouched and recorded
    as unresolved (never rewritten to a fake path);
  * an external / out-of-scope original -> left untouched, recorded external.

Usage:
    python3 scripts/link_rewriter.py            (self-check, offline)
"""

from __future__ import annotations

import argparse
import posixpath
import re
import sys
import urllib.parse
from html.parser import HTMLParser

from wayback_url_parser import is_wayback_url, restore_original_url


# attributes that carry URLs in HTML (srcset handled separately)
_URL_ATTRS = {"href", "src", "srcset", "data-src", "data-fallback",
              "data-original", "data-lazy-src", "poster"}
_GLOBAL_URL_ATTRS = frozenset(
    ("action", "background", "cite", "data", "formaction", "icon", "itemtype",
     "longdesc", "manifest", "ping", "profile", "usemap", "xmlns"))

_CSS_URL_RE = re.compile(r"url\(\s*(?P<url>(?:[^'\")]+|'[^']*'|\"[^\"]*\")?)\s*\)")
_META_URL_RE = re.compile(r"url\s*=\s*(['\"]?)([^;'\"]+)\1", re.IGNORECASE)


def _canonical(url: str) -> str:
    """Lower-case scheme/host, keep path/query/fragment verbatim."""
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path,
        parsed.params,
        parsed.query,
        parsed.fragment,
    ))


def resolve_link(raw: str, base_url: str) -> tuple[str | None, str]:
    """Resolve a raw link to its canonical original URL.

    Returns (canonical_url | None, status) where status is:
      'original'   — directly an http(s) URL of the original site
      'wayback'    — restored from an embedded Wayback replay link
      'relative'   — resolved against base_url
      'fragment'   — pure '#...' anchor, no resource
      'opaque'     — non-http scheme (mailto:, javascript:, tel:, data:)
    """
    raw = raw.strip()
    if not raw:
        return None, "opaque"
    if raw.startswith("#"):
        return None, "fragment"
    if is_wayback_url(raw):
        restored = restore_original_url(raw)
        if restored:
            return _canonical(restored), "wayback"
        return None, "opaque"
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("", "http", "https"):
        return None, "opaque"
    if parsed.scheme == "":
        joined = urllib.parse.urljoin(base_url, raw)
        return _canonical(joined), "relative"
    return _canonical(raw), "original"


class _Scanner(HTMLParser):
    """Collects (attr, value) pairs carrying URLs plus bare srcset entries."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.last_tag = ""
        self.items: list[tuple[str, str]] = []  # (attr, raw)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.last_tag = tag.lower()
        for attr, value in attrs:
            if value is None:
                continue
            attr, value = attr.lower(), value.strip()
            if attr in _URL_ATTRS:
                if attr == "srcset":
                    self.items.append(("srcset", value))
                else:
                    self.items.append((attr, value))
            elif attr == "content" and self.last_tag == "meta":
                self.items.append(("meta-content", value))
            elif attr in _GLOBAL_URL_ATTRS:
                self.items.append((attr, value))


def _srcset_values(value: str) -> list[str]:
    out: list[str] = []
    for part in value.split(","):
        tokens = part.strip().split()
        if tokens:
            out.append(tokens[0])
    return out


def _split_srcset(value: str) -> list[tuple[str, str]]:
    """Split a srcset value into (url, descriptor) pairs for rewrite."""
    pairs: list[tuple[str, str]] = []
    for part in value.split(","):
        tokens = part.strip().split()
        if not tokens:
            continue
        url, *descs = tokens
        pairs.append((url, " ".join(descs)))
    return pairs


class LinkRewriter:
    """Rewrites HTML/CSS of validated archived pages to local paths."""

    def __init__(self, resources: list[dict]) -> None:
        """resources: records with keys original_url, wayback_replay_url,
        local_path, validated (bool)."""
        self.by_canonical: dict[str, str] = {}
        self.by_wayback: dict[str, str] = {}
        for rec in resources:
            if not rec.get("validated") or not rec.get("local_path"):
                continue
            if rec.get("original_url"):
                self.by_canonical[_canonical(rec["original_url"])] = rec["local_path"]
            if rec.get("wayback_replay_url"):
                self.by_wayback[rec["wayback_replay_url"].strip()] = rec["local_path"]

    def lookup(self, canonical_url: str) -> str | None:
        return self.by_canonical.get(canonical_url)

    # ------------------------------------------------------------------
    def rewrite_html(self, html: str, base_url: str, anchor_path: str = "") \
            -> tuple[str, list[dict]]:
        """Rewrite links inside HTML.

        anchor_path: site-relative path of the page being written (e.g.
        'pages/station.html' or '' for the root index) so rewritten links use
        correct relative paths. base_url: the page's original URL (or its
        <base href> when present).
        """
        scanner = _Scanner(base_url)
        try:
            scanner.feed(html)
            scanner.close()
        except Exception:  # malformed HTML must not kill the copy
            scanner.items = []
        replacements: dict[str, str] = {}
        ops: list[dict] = []
        for attr, raw in scanner.items:
            if attr == "meta-content":
                replacement, status = self._rewrite_meta(raw, base_url)
                target: str | None = None
            elif attr == "srcset":
                replacement, status = self._rewrite_srcset(raw, base_url, anchor_path)
                target = None
            else:
                replacement, status, target = self._rewrite_one(raw, base_url, anchor_path)
            ops.append({"attr": attr, "attr_value": raw, "op_status": status,
                        "target": target, "local_path": replacement if replacement != raw else None})
            if replacement != raw:
                replacements[raw] = replacement
        rewritten = _apply_attribute_replacements(html, replacements)
        return rewritten, ops

    def rewrite_css(self, css: str, base_url: str, anchor_path: str = "") \
            -> tuple[str, list[dict]]:
        """Rewrite url(...) references inside a CSS body."""
        ops: list[dict] = []
        rewritten = css

        def _swap(match: re.Match) -> str:
            raw = match.group("url").strip().strip("'\"")
            replacement, status, target = self._rewrite_one(raw, base_url, anchor_path)
            if replacement == raw:
                return match.group(0)
            ops.append({"attr": "css-url", "attr_value": raw, "op_status": status,
                        "target": target, "local_path": replacement})
            return f"url({replacement})"

        return _CSS_URL_RE.sub(_swap, rewritten), ops

    # ------------------------------------------------------------------
    def _rewrite_one(self, raw: str, base_url: str, anchor_path: str) \
            -> tuple[str, str, str | None]:
        canonical, status = resolve_link(raw, base_url)
        if canonical is None:
            return raw, status, None
        fragment = ""
        canon_nofrag = canonical
        if "#" in canonical:
            index = canonical.index("#")
            canon_nofrag, fragment = canonical[:index], canonical[index:]
        local = self.by_canonical.get(canon_nofrag)
        if local is None and is_wayback_url(raw):
            local = self.by_wayback.get(raw.strip())
        if local is None:
            return raw, "unresolved", canonical
        relative = _to_anchor(local, anchor_path) + fragment
        return relative, "rewritten", canonical

    def _rewrite_srcset(self, raw: str, base_url: str, anchor_path: str) \
            -> tuple[str, str]:
        pairs = _split_srcset(raw)
        rewritten_parts: list[str] = []
        statuses: list[str] = []
        for url, desc in pairs:
            new_url, status, _ = self._rewrite_one(url, base_url, anchor_path)
            rewritten_parts.append(f"{new_url}{' ' + desc if desc else ''}".strip())
            statuses.append(status)
        joined = ", ".join(rewritten_parts)
        return (joined, "rewritten" if any(s == "rewritten" for s in statuses) else
                ("unresolved" if "unresolved" in statuses else "external"))

    def _rewrite_meta(self, content: str, base_url: str) -> tuple[str, str]:
        for match in _META_URL_RE.finditer(content):
            url = match.group(2).strip().strip("'\"").strip()
            new_url, status, _ = self._rewrite_one(url, base_url, "")
            if new_url != url:
                return content.replace(url, new_url, 1), status
        if urllib.parse.urlparse(content).scheme in ("http", "https"):
            new_url, status, _ = self._rewrite_one(content, base_url, "")
            if new_url != content:
                return new_url, status
        return content, "external"


def _to_anchor(local_path: str, anchor_path: str) -> str:
    """Relative path from the anchor page to `local_path` inside site/."""
    if not anchor_path:
        return local_path
    anchor_dir = posixpath.dirname(anchor_path.replace("\\", "/"))
    if anchor_dir in ("", "."):
        return local_path
    return posixpath.relpath(local_path.replace("\\", "/"), anchor_dir)


_ATTR_PATTERN = re.compile(
    r"(?P<attr>%s)\s*=\s*\"(?P<val>[^\"]*)\"" % "|".join(
        sorted(_URL_ATTRS | _GLOBAL_URL_ATTRS | {"content"})))


def _apply_attribute_replacements(text: str, replacements: dict[str, str]) -> str:
    """Swap exact attribute values in place; never touches substrings inside
    unrelated URLs (value-scoped, occurrence-by-occurrence)."""
    if not replacements:
        return text

    def _swap(match: re.Match) -> str:
        new = replacements.get(match.group("val"))
        if new is None:
            return match.group(0)
        return f'{match.group("attr")}="{_quote_safe(new)}"'

    return _ATTR_PATTERN.sub(_swap, text)


def _quote_safe(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;")


def probe() -> int:
    """Offline self-check of rewriting rules."""
    resources = [
        {"original_url": "http://example.org/img/map.gif",
         "wayback_replay_url":
         "https://web.archive.org/web/20040801124510id_/http://example.org/img/map.gif",
         "local_path": "media/img/map.gif", "validated": True},
        {"original_url": "http://example.org/pages/station.html",
         "local_path": "pages/station.html", "validated": True},
        {"original_url": "http://example.org/css/style.css",
         "local_path": "css/style.css", "validated": True},
        {"original_url": "http://example.org/img/absent.gif",
         "local_path": "media/img/absent.gif", "validated": False},  # unresolved
    ]
    rewriter = LinkRewriter(resources)
    html = ('<html><head><base href="http://example.org/">\n'
            '<meta http-equiv="refresh" content="0; url=http://example.org/pages/station.html">'
            '</head><body>\n'
            '<img src="img/map.gif">\n'
            '<img src="/img/absent.gif">\n'
            '<img src="https://web.archive.org/web/20040804234004/http://example.org/img/map.gif">\n'
            '<img srcset="/img/map.gif 1x, /css/style.css 2x">\n'
            '<a href="http://example.org/pages/station.html#sec">go</a>\n'
            '<a href="http://external.org/ref">out</a>\n'
            '<a href="mailto:x@y.z">m</a>\n'
            '</body></html>')
    out, ops = rewriter.rewrite_html(html, "http://example.org/", anchor_path="pages/station.html")
    assert "/web.archive.org/" not in out, out
    assert 'src="../media/img/map.gif"' in out, out        # relative link rewritten
    assert "/img/absent.gif" in out, out                   # unresolved kept as-is
    assert 'srcset="../media/img/map.gif 1x, ../css/style.css 2x"' in out, out
    assert 'href="station.html#sec"' in out, out           # fragment preserved on local path
    assert "external.org/ref" in out, out                  # external untouched
    assert "mailto:x@y.z" in out, out                      # opaque untouched
    statuses = {op["op_status"] for op in ops}
    assert "unresolved" in statuses and "rewritten" in statuses, statuses
    # anchor at site root -> plain relative paths, no '../' prefix
    out_root, _ = rewriter.rewrite_html(html, "http://example.org/", anchor_path="index.html")
    assert 'src="media/img/map.gif"' in out_root, out_root
    css = ("a{background:url(/img/map.gif)}"
           "b{background:url('http://example.org/css/style.css')}")
    css_out, css_ops = rewriter.rewrite_css(css, "http://example.org/")
    assert "media/img/map.gif" in css_out and "css/style.css" in css_out, css_out
    assert css_ops and css_ops[0]["op_status"] == "rewritten", css_ops
    print("link_rewriter probe ok: html/srcset/meta/css/fragment/unresolved hold")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Archived link rewriter (trehgranka-archivist)")
    parser.add_argument("--probe", action="store_true", help="offline self-check")
    args = parser.parse_args()
    if args.probe:
        raise SystemExit(probe())
    print(__doc__)
    raise SystemExit(0)