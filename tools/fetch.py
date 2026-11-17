"""Fetch one web page as plain text: 8 s timeout, 1 MB cap, ~3,000 characters kept.

``fetch(url) -> {"url", "title", "text"}`` plus ``"error"`` when it failed (text "").
Never raises. Only public http(s) URLs are fetched. The text is untrusted data.
Reddit threads are read through their public ``.json`` view (post + top comments).
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

import requests

from tools.search import user_agent

TIMEOUT_S = 8
MAX_BYTES = 1_000_000
MAX_CHARS = 3000
MAX_REDIRECTS = 3


def _ip_is_public(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified)


def _resolves_public(host: str) -> bool:
    """Every address the host name resolves to must be public (no private, loopback, link-local...)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return False
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and all(_ip_is_public(a) for a in addresses)


def _public_http(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host in ("localhost",) or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        ipaddress.ip_address(host)
        return _ip_is_public(host)
    except ValueError:
        return _resolves_public(host)


def _download(url: str) -> tuple:
    """GET with redirects followed by hand (at most MAX_REDIRECTS): every hop is checked first."""
    for _ in range(MAX_REDIRECTS + 1):
        if not _public_http(url):
            raise ValueError("redirected to a non-public address")
        r = requests.get(url, timeout=TIMEOUT_S, stream=True, allow_redirects=False,
                         headers={"User-Agent": user_agent()})
        if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
            location = r.headers.get("Location", "")
            r.close()
            if not location:
                raise ValueError("redirect without a Location header")
            url = urljoin(url, location)
            continue
        return _read(r)
    raise ValueError(f"more than {MAX_REDIRECTS} redirects")


def _read(r) -> tuple:
    with r:
        r.raise_for_status()
        chunks, size = [], 0
        for chunk in r.iter_content(65536):
            chunks.append(chunk)
            size += len(chunk)
            if size >= MAX_BYTES:
                break
        raw = b"".join(chunks)[:MAX_BYTES]
        return raw.decode(r.encoding or "utf-8", "replace"), r.headers.get("Content-Type", "")


def html_to_text(markup: str) -> tuple:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form", "svg", "iframe"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return title[:200], re.sub(r"\s+", " ", text).strip()


def _reddit_json_url(url: str):
    parsed = urlparse(url)
    if parsed.hostname and parsed.hostname.endswith("reddit.com") and "/comments/" in parsed.path:
        return f"https://www.reddit.com{parsed.path.rstrip('/')}.json?limit=15&raw_json=1"
    return None


def _reddit_text(raw: str) -> tuple:
    import json

    data = json.loads(raw)
    post = data[0]["data"]["children"][0]["data"]
    parts = [post.get("title", ""), post.get("selftext", "")]
    for child in data[1]["data"]["children"][:12]:
        body = (child.get("data") or {}).get("body")
        if body:
            parts.append("Comment: " + body)
    return post.get("title", "")[:200], re.sub(r"\s+", " ", " ".join(parts)).strip()


def fetch(url: str) -> dict:
    if not _public_http(url):
        return {"url": url, "title": "", "text": "", "error": "not a public http(s) URL"}
    try:
        reddit = _reddit_json_url(url)
        if reddit:
            raw, _ = _download(reddit)
            title, text = _reddit_text(raw)
        else:
            raw, ctype = _download(url)
            if "html" in ctype or raw.lstrip()[:1] == "<":
                title, text = html_to_text(raw)
            elif ctype.startswith("text/") or "json" in ctype:
                title, text = "", re.sub(r"\s+", " ", raw).strip()
            else:
                return {"url": url, "title": "", "text": "", "error": f"unsupported content type {ctype}"}
        return {"url": url, "title": title, "text": text[:MAX_CHARS]}
    except Exception as e:
        return {"url": url, "title": "", "text": "", "error": f"{type(e).__name__}: {str(e)[:160]}"}
