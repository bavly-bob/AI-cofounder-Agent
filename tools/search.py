"""Free web search with no API key: DuckDuckGo (ddgs), Hacker News (Algolia),
Reddit public search.json and the Wikipedia API.

``search(query, k=5)`` returns ``[{"title", "url", "snippet", "provider"}]``.
A provider that fails (no network, rate limit, blocked) adds nothing; the reason
is appended to ``errors`` if a list is passed. It never raises.

Requests carry a generic, honest User-Agent and nothing about the founder.
"""
from __future__ import annotations

import html
import os
import re
from concurrent.futures import ThreadPoolExecutor

import requests

DEFAULT_USER_AGENT = "ai-cofounder-agent/0.1 (open-source research bot)"
TIMEOUT_S = 8
DDG_BACKENDS = ("bing", "yahoo", "duckduckgo")
_TAG = re.compile(r"<[^>]+>")


def user_agent() -> str:
    return (os.getenv("HTTP_USER_AGENT") or "").strip() or DEFAULT_USER_AGENT


def _clean(text, limit: int = 400) -> str:
    text = html.unescape(_TAG.sub(" ", str(text or "")))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _get_json(url: str, params: dict) -> dict:
    r = requests.get(url, params=params, timeout=TIMEOUT_S, headers={"User-Agent": user_agent()})
    r.raise_for_status()
    return r.json()


def search_ddg(query: str, k: int) -> list:
    from ddgs import DDGS

    rows, last = [], None
    for backend in DDG_BACKENDS:  # the metasearch backends differ in what they return per region
        try:
            rows = DDGS(timeout=TIMEOUT_S).text(query, max_results=k, backend=backend) or []
        except Exception as e:
            last = e
            continue
        if rows:
            break
    if not rows and last is not None:
        raise last
    return [{"title": _clean(r.get("title"), 200), "url": r.get("href") or r.get("url") or "",
             "snippet": _clean(r.get("body")), "provider": f"ddgs/{backend}"} for r in rows]


def search_hn(query: str, k: int) -> list:
    data = _get_json("https://hn.algolia.com/api/v1/search", {"query": query, "hitsPerPage": k})
    out = []
    for hit in data.get("hits", [])[:k]:
        oid = hit.get("objectID")
        if not oid:
            continue
        title = hit.get("title") or hit.get("story_title") or ""
        text = hit.get("story_text") or hit.get("comment_text") or ""
        out.append({"title": _clean(title, 200), "url": f"https://news.ycombinator.com/item?id={oid}",
                    "snippet": _clean(f"{title}. {text}"), "provider": "hackernews"})
    return out


def search_reddit(query: str, k: int) -> list:
    data = _get_json("https://www.reddit.com/search.json",
                     {"q": query, "limit": k, "sort": "relevance", "raw_json": 1})
    out = []
    for child in (data.get("data") or {}).get("children", [])[:k]:
        post = child.get("data") or {}
        if post.get("over_18") or not post.get("permalink"):
            continue
        out.append({"title": _clean(post.get("title"), 200),
                    "url": "https://www.reddit.com" + post["permalink"],
                    "snippet": _clean(f"{post.get('title', '')}. {post.get('selftext', '')}"),
                    "provider": "reddit"})
    return out


def search_wikipedia(query: str, k: int) -> list:
    data = _get_json("https://en.wikipedia.org/w/api.php",
                     {"action": "query", "list": "search", "srsearch": query, "srlimit": min(k, 3),
                      "format": "json"})
    out = []
    for hit in (data.get("query") or {}).get("search", []):
        title = hit.get("title") or ""
        out.append({"title": title, "url": "https://en.wikipedia.org/wiki/" + title.replace(" ", "_"),
                    "snippet": _clean(hit.get("snippet")), "provider": "wikipedia"})
    return out


PROVIDERS = {"duckduckgo": search_ddg, "hackernews": search_hn, "reddit": search_reddit,
             "wikipedia": search_wikipedia}


def search(query: str, k: int = 5, errors: list | None = None, providers=None) -> list:
    """Query every provider concurrently; interleave and de-duplicate; return up to ``k`` results."""
    names = list(providers or PROVIDERS)
    per = {}

    def one(name):
        try:
            return name, PROVIDERS[name](query, k), None
        except Exception as e:  # no network, blocked, rate limited, bad JSON...
            detail = re.sub(r"\s*for url:\s*\S+", "", str(e))   # no query URLs in notes or logs
            return name, [], f"{name}: {type(e).__name__}: {detail[:160]}"

    with ThreadPoolExecutor(max_workers=len(names) or 1) as pool:
        for name, rows, err in pool.map(one, names):
            per[name] = rows
            if err and errors is not None:
                errors.append(err)
    merged, seen = [], set()
    # Round robin over the discussion/web providers, so one cannot crowd out the others.
    # Wikipedia (background, rarely a pain signal) gets at most one slot, at the end.
    main = [n for n in names if n != "wikipedia"]
    depth = max((len(per.get(n, [])) for n in main), default=0)
    ordered = [per[n][i] for i in range(depth) for n in main if i < len(per.get(n, []))]
    ordered += per.get("wikipedia", [])[:1]
    for row in ordered:
        url = row.get("url") or ""
        if url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            merged.append(row)
    wiki = [r for r in merged if r.get("provider") == "wikipedia"]
    return [r for r in merged if r.get("provider") != "wikipedia"][:max(0, k - len(wiki))] + wiki
