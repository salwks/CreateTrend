#!/usr/bin/env python3
"""
Weekly Product Digest MCP Server.

Aggregates startup / product / indie-maker news from multiple public sources
(RSS feeds and public JSON APIs) and produces a weekly report grouped by source.

Sources:
  - TechCrunch        (RSS)
  - BetaNews          (RSS)
  - Product Hunt      (RSS)
  - Indie Hackers     (unofficial feed.indiehackers.world RSS)
  - Hacker News       (Show HN, via public Algolia API)  -- broadens indie coverage
  - Reddit            (r/SaaS + r/indiehackers, public JSON)

Design notes:
  - Everything is UTF-8 (avoids the cp949 decode issues seen on Korean Windows).
  - Sources are fetched in parallel; a single source failing never breaks the report.
  - All items are normalized to one schema, filtered to a 7-day window, then grouped.
"""

from __future__ import annotations

import asyncio
import calendar
import html
import json
import os
import re
import time
from dataclasses import dataclass, asdict, field
from enum import Enum
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

import feedparser
import httpx
from pydantic import BaseModel, ConfigDict, Field
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

mcp = FastMCP("weekly_product_digest_mcp")

USER_AGENT = (
    "Mozilla/5.0 (compatible; weekly-product-digest-mcp/1.0; "
    "+https://modelcontextprotocol.io)"
)
# Reddit blocks fake-browser UAs and requires a unique, descriptive agent.
REDDIT_USER_AGENT = "weekly-product-digest-mcp/1.0 (public feed aggregator)"
HTTP_TIMEOUT = 25.0
DEFAULT_PER_SOURCE_LIMIT = 10
SECONDS_PER_DAY = 86_400
WEEK_SECONDS = 7 * SECONDS_PER_DAY

# Indie Hackers is server-rendered HTML; these public section pages are the
# key-free fallback we scrape when the unofficial RSS feed is unavailable.
IH_SCRAPE_PAGES = [
    "https://www.indiehackers.com/",
    "https://www.indiehackers.com/tech",
    "https://www.indiehackers.com/starting-up",
]
IH_LINK_STOPWORDS = {"like", "upvote", "view comments", "comment", "comments", "reply"}

# OPTIONAL Algolia layer for Indie Hackers.
# Not enabled by default: IH's public Algolia app is wired to places.js (address
# autocomplete) and the content index name is not exposed on the page, so this
# only works if the operator supplies a confirmed, currently-valid index name.
# Reverse-engineered client keys/indexes rotate without notice — treat as fragile.
# Configure via environment variables (never hardcode scraped secrets):
#   IH_ALGOLIA_APP_ID, IH_ALGOLIA_API_KEY (search-only), IH_ALGOLIA_INDEX
IH_ALGOLIA_APP_ID = os.environ.get("IH_ALGOLIA_APP_ID", "").strip()
IH_ALGOLIA_API_KEY = os.environ.get("IH_ALGOLIA_API_KEY", "").strip()
IH_ALGOLIA_INDEX = os.environ.get("IH_ALGOLIA_INDEX", "").strip()

# --- Key-pick selection --------------------------------------------------
# The digest's headline "Key Picks" section ranks items by editorial priority
# (focus: product launches & funding). Two selectors exist:
#   - LLM: Claude reads all items and picks/ranks them with editorial judgment.
#   - heuristic: deterministic keyword scoring (below), used as a fallback.
# Mode is chosen per request (key_pick_mode) or via KEY_PICK_MODE env default.
#   "auto"      -> LLM if credentials are configured, else heuristic
#   "llm"       -> force LLM (falls back to heuristic only on error)
#   "heuristic" -> force keyword heuristic (no API calls)
KEY_PICK_MODE_DEFAULT = os.environ.get("KEY_PICK_MODE", "heuristic").strip().lower()
# Model for LLM selection. Defaults to Claude Opus 4.8; override via env.
KEY_PICK_MODEL = os.environ.get("KEY_PICK_MODEL", "claude-opus-4-8").strip()
FUNDING_WEIGHT = 3
LAUNCH_WEIGHT = 2
# Money amounts only count as funding at million/billion scale, so MRR/pricing
# mentions like "$29/month" or "$5K MRR" don't produce false funding hits.
_MONEY_RE = re.compile(
    r"(?:\$\s?\d[\d,.]*\s?(?:m|bn|b|million|billion|trillion)\b)"
    r"|(?:\b\d[\d,.]*\s?(?:million|billion|trillion)\b)",
    re.IGNORECASE,
)
# Strong, unambiguous funding cues (each is funding-news on its own).
_FUNDING_STRONG_RES = [
    re.compile(p, re.IGNORECASE) for p in (
        r"\bseed round\b", r"\bseries\s+[a-e]\b", r"\bvaluation\b",
        r"\bvalued at\b", r"\bipo\b", r"\bfunding round\b",
        r"\bacqui(?:re|res|red|sition)\b", r"\bventure (?:capital|fund|firm)\b",
        r"\bbacked by\b", r"\bvc firm\b", r"\braises? \$?\d",
    )
]
# "raise/raised/raising" is ambiguous ("raised prices" vs "raised $18M"), so it
# only counts as funding when paired with a scale amount or a funding-context word.
_RAISE_RE = re.compile(r"\braise(?:s|d)?\b|\braising\b", re.IGNORECASE)
_FUNDING_CTX_RE = re.compile(
    r"\b(?:funding|round|capital|investors?|venture|seed|series|equity)\b",
    re.IGNORECASE,
)
_LAUNCH_RES = [
    re.compile(p, re.IGNORECASE) for p in (
        r"\blaunch(?:es|ed|ing)?\b", r"\bintroduc(?:e|es|ing)\b",
        r"\bunveil(?:s|ed|ing)?\b", r"\brelease(?:s|d)?\b",
        r"\bannounc(?:e|es|ed|ing)\b", r"\bnow available\b",
        r"\brolls?\s+out\b", r"\brolling out\b", r"\bdebut(?:s|ed)?\b",
        r"\bships?\b", r"\bshipped\b", r"\bout now\b", r"\bbeta\b",
        r"\bgeneral availability\b", r"\bshow hn\b",
    )
]
# Sources whose items are launches by nature (a submission == a new product).
_LAUNCH_NATIVE_SOURCES = {"producthunt", "hackernews"}


# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------

class SourceKind(str, Enum):
    RSS = "rss"
    HN = "hackernews"
    REDDIT = "reddit"
    INDIEHACKERS = "indiehackers"  # RSS feed → SSR HTML scrape → optional Algolia


@dataclass(frozen=True)
class SourceDef:
    key: str          # stable id used in tool params (e.g. "techcrunch")
    label: str        # human-readable name
    kind: SourceKind
    url: str          # primary endpoint
    fallback: Optional[str] = None  # secondary endpoint (used if primary fails)
    extra: Dict[str, Any] = field(default_factory=dict)


SOURCES: Dict[str, SourceDef] = {
    "techcrunch": SourceDef(
        key="techcrunch",
        label="TechCrunch",
        kind=SourceKind.RSS,
        url="https://techcrunch.com/feed/",
    ),
    "betanews": SourceDef(
        key="betanews",
        label="BetaNews",
        kind=SourceKind.RSS,
        url="https://betanews.com/feed/",
    ),
    "producthunt": SourceDef(
        key="producthunt",
        label="Product Hunt",
        kind=SourceKind.RSS,
        url="https://www.producthunt.com/feed",
    ),
    "indiehackers": SourceDef(
        key="indiehackers",
        label="Indie Hackers",
        kind=SourceKind.INDIEHACKERS,
        # Layer 1: unofficial but currently-active feed service (@pyoner).
        url="https://feed.indiehackers.world/posts.rss?exclude=link-post",
        # Layer 1b: weekly "top" feed if the main feed is unavailable.
        # Layer 2 (no key): SSR HTML scrape of indiehackers.com (see IH_SCRAPE_PAGES).
        # Layer 3 (optional): Algolia, only if IH_ALGOLIA_* env vars are set.
        fallback="https://feed.indiehackers.world/top/week.rss",
    ),
    "hackernews": SourceDef(
        key="hackernews",
        label="Hacker News (Show HN)",
        kind=SourceKind.HN,
        # Public Algolia API, no key required. Window is applied via numericFilters.
        url="https://hn.algolia.com/api/v1/search_by_date",
        extra={"tags": "show_hn", "min_points": 2},
    ),
    "reddit": SourceDef(
        key="reddit",
        label="Reddit (r/SaaS, r/indiehackers)",
        kind=SourceKind.REDDIT,
        url="https://www.reddit.com/r/SaaS/top.json",
        extra={"subreddits": ["SaaS", "indiehackers"]},
    ),
}

DEFAULT_SOURCES = list(SOURCES.keys())


# ---------------------------------------------------------------------------
# Normalized item model
# ---------------------------------------------------------------------------

@dataclass
class Item:
    source: str            # source key (e.g. "techcrunch")
    source_label: str      # human-readable source name
    title: str
    url: str
    published_epoch: int   # UTC epoch seconds; 0 if unknown
    published_iso: str     # ISO-8601 UTC string; "" if unknown
    score: Optional[int] = None   # votes / points where the source exposes them
    author: Optional[str] = None
    summary: Optional[str] = None

    def sort_key(self) -> tuple:
        # Higher score first, then more recent first.
        return (-(self.score or 0), -self.published_epoch)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean_text(raw: Optional[str], max_len: int = 1200) -> Optional[str]:
    """Strip HTML tags/entities and collapse whitespace from a summary blurb."""
    if not raw:
        return None
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    if not text:
        return None
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def _iso(epoch: int) -> str:
    if not epoch:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _struct_to_epoch(struct_time) -> int:
    """feedparser gives UTC struct_time in *_parsed fields; convert to epoch."""
    if not struct_time:
        return 0
    try:
        return calendar.timegm(struct_time)
    except (TypeError, ValueError, OverflowError):
        return 0


def _window_bounds(week_offset: int) -> tuple[int, int]:
    """Return (start_epoch, end_epoch) for the requested week.

    week_offset=0 -> the most recent 7 days.
    week_offset=1 -> the 7 days before that, etc.
    """
    now = int(time.time())
    end = now - week_offset * WEEK_SECONDS
    start = end - WEEK_SECONDS
    return start, end


# ---------------------------------------------------------------------------
# Fetchers (one per source kind). Each returns a list[Item], never raises.
# ---------------------------------------------------------------------------

async def _get(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    resp = await client.get(url, timeout=HTTP_TIMEOUT, **kwargs)
    resp.raise_for_status()
    return resp


async def _fetch_rss(client: httpx.AsyncClient, src: SourceDef) -> List[Item]:
    """Fetch and parse an RSS/Atom feed, with an optional fallback URL."""
    content: Optional[bytes] = None
    last_err: Optional[Exception] = None
    for candidate in (src.url, src.fallback):
        if not candidate:
            continue
        try:
            resp = await _get(client, candidate)
            content = resp.content
            break
        except Exception as e:  # noqa: BLE001 - source isolation is intentional
            last_err = e
            continue
    if content is None:
        raise last_err or RuntimeError(f"No feed content for {src.key}")

    parsed = feedparser.parse(content)
    items: List[Item] = []
    for entry in parsed.entries:
        epoch = _struct_to_epoch(
            getattr(entry, "published_parsed", None)
            or getattr(entry, "updated_parsed", None)
        )
        summary = _clean_text(
            getattr(entry, "summary", None) or getattr(entry, "description", None)
        )
        author = getattr(entry, "author", None) or None
        items.append(
            Item(
                source=src.key,
                source_label=src.label,
                title=_clean_text(getattr(entry, "title", ""), max_len=200) or "(untitled)",
                url=getattr(entry, "link", "") or "",
                published_epoch=epoch,
                published_iso=_iso(epoch),
                author=author,
                summary=summary,
            )
        )
    return items


async def _fetch_hackernews(
    client: httpx.AsyncClient, src: SourceDef, start: int, end: int
) -> List[Item]:
    """Show HN stories from the public HN Algolia API within the time window."""
    min_points = int(src.extra.get("min_points", 0))
    params = {
        "tags": src.extra.get("tags", "show_hn"),
        "numericFilters": f"created_at_i>{start},created_at_i<{end}",
        "hitsPerPage": 100,
    }
    resp = await _get(client, src.url, params=params)
    data = resp.json()
    items: List[Item] = []
    for hit in data.get("hits", []):
        points = hit.get("points") or 0
        if points < min_points:
            continue
        epoch = int(hit.get("created_at_i") or 0)
        object_id = hit.get("objectID", "")
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={object_id}"
        items.append(
            Item(
                source=src.key,
                source_label=src.label,
                title=_clean_text(hit.get("title", ""), max_len=200) or "(untitled)",
                url=url,
                published_epoch=epoch,
                published_iso=_iso(epoch),
                score=int(points),
                author=hit.get("author"),
                summary=_clean_text(hit.get("story_text")),
            )
        )
    return items


async def _fetch_reddit(
    client: httpx.AsyncClient, src: SourceDef
) -> List[Item]:
    """Top weekly posts from the configured subreddits.

    Reddit blocks unauthenticated JSON endpoints (403) but still serves per-subreddit
    Atom feeds, so we use those. Trade-off: RSS carries no upvote score, so Reddit
    items don't appear in the score-ranked highlights (only in their own section).
    """
    subs = src.extra.get("subreddits", [])
    items: List[Item] = []
    ok_count = 0
    last_err: Optional[Exception] = None
    for sub in subs:
        url = f"https://www.reddit.com/r/{sub}/top/.rss"
        try:
            resp = await _get(
                client,
                url,
                params={"t": "week"},
                headers={"User-Agent": REDDIT_USER_AGENT},
            )
            ok_count += 1
        except Exception as e:  # noqa: BLE001 - skip a failing subreddit, keep the rest
            last_err = e
            continue
        parsed = feedparser.parse(resp.content)
        for entry in parsed.entries:
            epoch = _struct_to_epoch(
                getattr(entry, "published_parsed", None)
                or getattr(entry, "updated_parsed", None)
            )
            author = getattr(entry, "author", None) or None
            if author and author.startswith("/u/"):
                author = author[3:]
            items.append(
                Item(
                    source=src.key,
                    source_label=f"r/{sub}",
                    title=_clean_text(getattr(entry, "title", ""), max_len=200) or "(untitled)",
                    url=getattr(entry, "link", "") or "",
                    published_epoch=epoch,
                    published_iso=_iso(epoch),
                    author=author,
                    summary=_clean_text(
                        getattr(entry, "summary", None) or getattr(entry, "description", None)
                    ),
                )
            )
    # If every subreddit failed, surface it honestly rather than masking as empty.
    if subs and ok_count == 0:
        raise last_err or RuntimeError("Reddit: all subreddits failed to fetch")
    return items


class _IHPostParser(HTMLParser):
    """Extract (href, title) for `/post/...` anchors from Indie Hackers SSR HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._href: Optional[str] = None
        self._buf: List[str] = []
        self.posts: List[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "a":
            href = dict(attrs).get("href", "") or ""
            self._href = href if href.startswith("/post/") else None
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            title = " ".join("".join(self._buf).split())
            if title and title.lower() not in IH_LINK_STOPWORDS and len(title) >= 8:
                self.posts.append((self._href, title))
            self._href = None
            self._buf = []


async def _scrape_indiehackers_html(client: httpx.AsyncClient) -> List[Item]:
    """Key-free fallback: parse posts straight out of Indie Hackers' SSR HTML.

    The homepage/section pages are server-rendered with post links embedded, so no
    API key or index name is needed. Publish dates aren't exposed inline, so items
    carry epoch=0 (the window filter admits undated items, and these pages already
    surface only recent posts).
    """
    # href -> longest title seen (real title beats labels like "View Comments").
    best: Dict[str, str] = {}
    for page in IH_SCRAPE_PAGES:
        try:
            resp = await _get(client, page)
        except Exception:  # noqa: BLE001 - try the remaining pages
            continue
        parser = _IHPostParser()
        parser.feed(resp.text)
        for href, title in parser.posts:
            if len(title) > len(best.get(href, "")):
                best[href] = title

    items: List[Item] = []
    for href, title in best.items():
        items.append(
            Item(
                source="indiehackers",
                source_label="Indie Hackers",
                title=_clean_text(title, max_len=200) or "(untitled)",
                url="https://www.indiehackers.com" + href,
                published_epoch=0,
                published_iso="",
            )
        )
    return items


async def _fetch_algolia_indiehackers(client: httpx.AsyncClient) -> List[Item]:
    """Optional Algolia layer — only runs when IH_ALGOLIA_* env vars are configured.

    Uses Algolia's standard client search endpoint. Because IH does not expose a
    content index name publicly, the operator must supply a confirmed index via
    IH_ALGOLIA_INDEX (and the search-only app id/key). Returns [] if unconfigured.
    """
    if not (IH_ALGOLIA_APP_ID and IH_ALGOLIA_API_KEY and IH_ALGOLIA_INDEX):
        return []
    url = f"https://{IH_ALGOLIA_APP_ID}-dsn.algolia.net/1/indexes/{IH_ALGOLIA_INDEX}/query"
    headers = {
        "X-Algolia-Application-Id": IH_ALGOLIA_APP_ID,
        "X-Algolia-API-Key": IH_ALGOLIA_API_KEY,
        "Content-Type": "application/json",
    }
    resp = await client.post(
        url, headers=headers, json={"params": "hitsPerPage=30"}, timeout=HTTP_TIMEOUT
    )
    resp.raise_for_status()
    hits = resp.json().get("hits", [])
    items: List[Item] = []
    for h in hits:
        epoch = int(h.get("createdAt") or h.get("created_at_i") or 0)
        oid = h.get("objectID", "")
        items.append(
            Item(
                source="indiehackers",
                source_label="Indie Hackers",
                title=_clean_text(h.get("title") or h.get("name") or "", max_len=200) or "(untitled)",
                url=h.get("url") or (f"https://www.indiehackers.com/post/{oid}" if oid else ""),
                published_epoch=epoch,
                published_iso=_iso(epoch),
                score=h.get("score") if isinstance(h.get("score"), int) else None,
                author=(h.get("userName") or h.get("author")),
                summary=_clean_text(h.get("body") or h.get("text")),
            )
        )
    return items


async def _fetch_indiehackers(client: httpx.AsyncClient, src: SourceDef) -> List[Item]:
    """Indie Hackers with layered redundancy: RSS feed → SSR HTML → optional Algolia.

    Each layer is tried only if the previous one yields nothing, so a single point
    of failure (the unofficial feed service going down) never blackholes the source.
    """
    # Layer 1: unofficial RSS feed (primary + weekly-top fallback).
    try:
        items = await _fetch_rss(client, src)
        if items:
            return items
    except Exception:  # noqa: BLE001 - fall through to the next layer
        pass

    # Layer 2: key-free SSR HTML scrape of indiehackers.com.
    try:
        items = await _scrape_indiehackers_html(client)
        if items:
            return items
    except Exception:  # noqa: BLE001 - fall through to the next layer
        pass

    # Layer 3: optional Algolia (returns [] unless configured via env).
    return await _fetch_algolia_indiehackers(client)


async def _fetch_source(
    client: httpx.AsyncClient, src: SourceDef, start: int, end: int
) -> List[Item]:
    """Dispatch to the right fetcher for a source. Raises on failure."""
    if src.kind == SourceKind.RSS:
        return await _fetch_rss(client, src)
    if src.kind == SourceKind.HN:
        return await _fetch_hackernews(client, src, start, end)
    if src.kind == SourceKind.REDDIT:
        return await _fetch_reddit(client, src)
    if src.kind == SourceKind.INDIEHACKERS:
        return await _fetch_indiehackers(client, src)
    raise ValueError(f"Unknown source kind: {src.kind}")


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

@dataclass
class SourceResult:
    key: str
    label: str
    ok: bool
    items: List[Item] = field(default_factory=list)
    error: Optional[str] = None


async def _collect(
    source_keys: List[str], week_offset: int, per_source_limit: int
) -> tuple[List[SourceResult], int, int]:
    """Fetch every requested source in parallel and window-filter the results."""
    start, end = _window_bounds(week_offset)
    selected = [SOURCES[k] for k in source_keys]

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/xml, */*"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        raw = await asyncio.gather(
            *(_fetch_source(client, src, start, end) for src in selected),
            return_exceptions=True,
        )

    results: List[SourceResult] = []
    for src, outcome in zip(selected, raw):
        if isinstance(outcome, Exception):
            results.append(
                SourceResult(
                    key=src.key,
                    label=src.label,
                    ok=False,
                    error=f"{type(outcome).__name__}: {outcome}",
                )
            )
            continue
        # Window filter (RSS/Reddit fetchers don't pre-filter by time).
        windowed = [
            it for it in outcome
            if it.published_epoch == 0 or start <= it.published_epoch <= end
        ]
        windowed.sort(key=lambda it: it.sort_key())
        results.append(
            SourceResult(
                key=src.key,
                label=src.label,
                ok=True,
                items=windowed[:per_source_limit],
            )
        )
    return results, start, end


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _importance(item: Item) -> tuple[int, List[str]]:
    """Score an item for the 'Key Picks' section (focus: funding + product launch).

    Returns (score, tags) where tags is a subset of ["funding", "launch"].
    Deterministic keyword heuristic — funding weighted above launch per the
    configured editorial priority.
    """
    text = f"{item.title} {item.summary or ''}"
    tags: List[str] = []
    score = 0

    has_money = bool(_MONEY_RE.search(text))
    fund_hits = sum(1 for r in _FUNDING_STRONG_RES if r.search(text))
    if has_money:
        fund_hits += 2
    # Ambiguous "raise" only counts with a scale amount or explicit funding context.
    if _RAISE_RE.search(text) and (has_money or _FUNDING_CTX_RE.search(text)):
        fund_hits += 2
    if fund_hits:
        tags.append("funding")
        score += FUNDING_WEIGHT * min(fund_hits, 3)

    launch_hits = sum(1 for r in _LAUNCH_RES if r.search(text))
    if item.source in _LAUNCH_NATIVE_SOURCES:
        launch_hits += 1
    if launch_hits:
        tags.append("launch")
        score += LAUNCH_WEIGHT * min(launch_hits, 3)

    return score, tags


@dataclass
class KeyPick:
    item: Item
    importance: int
    tags: List[str]
    reason: Optional[str] = None   # one-line rationale (LLM mode only)
    via: str = "heuristic"          # "heuristic" | "llm"


def _heuristic_key_picks(results: List["SourceResult"], count: int) -> List[KeyPick]:
    """Rank all fetched items by keyword-based importance; return the top `count`."""
    picks: List[KeyPick] = []
    for r in results:
        if not r.ok:
            continue
        for it in r.items:
            score, tags = _importance(it)
            if score > 0:
                picks.append(KeyPick(item=it, importance=score, tags=tags, via="heuristic"))
    picks.sort(key=lambda p: (-p.importance, p.item.sort_key()))
    return picks[:count]


def _all_ok_items(results: List["SourceResult"]) -> List[Item]:
    """Flatten items from all successful sources into one indexable list."""
    return [it for r in results if r.ok for it in r.items]


def _has_anthropic_credentials() -> bool:
    """Best-effort check for API credentials (env var or a logged-in profile)."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    # An `ant auth login` profile also works with a bare client.
    cfg = os.environ.get("ANTHROPIC_CONFIG_DIR")
    candidates = [cfg] if cfg else []
    candidates.append(os.path.join(os.path.expanduser("~"), ".config", "anthropic"))
    candidates.append(os.path.join(os.environ.get("APPDATA", ""), "Anthropic"))
    return any(p and os.path.isdir(os.path.join(p, "credentials")) for p in candidates)


# JSON schema for the LLM's structured selection output.
_KEY_PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["funding", "launch"]},
                    },
                    "reason": {"type": "string"},
                },
                "required": ["index", "tags", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["picks"],
    "additionalProperties": False,
}

_KEY_PICK_SYSTEM = (
    "You are the editor of a weekly startup/product news digest. From a list of "
    "candidate items, select and rank the most important ones for busy founders and "
    "product people. Editorial priority, in order: (1) funding/investment news "
    "(rounds, valuations, acquisitions, IPOs), (2) notable product launches and "
    "releases. Prefer concrete, high-impact items over routine chatter or personal "
    "milestones. Rank most-important first. Tag each pick with which of "
    "'funding'/'launch' apply (one or both). Write each 'reason' as a single short "
    "Korean sentence explaining why it matters."
)


async def _llm_key_picks(results: List["SourceResult"], count: int) -> List[KeyPick]:
    """Select/rank key picks with Claude. Raises on API/SDK error (caller handles)."""
    from anthropic import AsyncAnthropic  # lazy import: only needed in LLM mode

    items = _all_ok_items(results)
    if not items:
        return []

    lines = []
    for gi, it in enumerate(items):
        summary = (it.summary or "").replace("\n", " ")
        if len(summary) > 240:
            summary = summary[:239] + "…"
        lines.append(f"[{gi}] ({it.source_label}) {it.title} :: {summary}")
    candidate_block = "\n".join(lines)

    prompt = (
        f"Here are {len(items)} candidate items (index in brackets). Select the "
        f"{count} most important and return them ranked most-important first.\n\n"
        f"{candidate_block}"
    )

    client = AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model=KEY_PICK_MODEL,
            max_tokens=2000,
            system=_KEY_PICK_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": _KEY_PICK_SCHEMA},
            },
        )
    finally:
        await client.close()

    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
    data = json.loads(text)

    picks: List[KeyPick] = []
    seen: set = set()
    for rank, entry in enumerate(data.get("picks", [])):
        idx = entry.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(items) or idx in seen:
            continue
        seen.add(idx)
        tags = [t for t in entry.get("tags", []) if t in ("funding", "launch")]
        picks.append(
            KeyPick(
                item=items[idx],
                importance=count - rank,  # preserve LLM ordering
                tags=tags,
                reason=(entry.get("reason") or "").strip() or None,
                via="llm",
            )
        )
        if len(picks) >= count:
            break
    return picks


async def _resolve_key_picks(
    results: List["SourceResult"], count: int, mode: str
) -> tuple[List[KeyPick], str]:
    """Return (key_picks, method) honoring the requested mode with graceful fallback."""
    if count <= 0:
        return [], "none"
    mode = (mode or "auto").lower()

    want_llm = mode == "llm" or (mode == "auto" and _has_anthropic_credentials())
    if want_llm:
        try:
            picks = await _llm_key_picks(results, count)
            if picks:
                return picks, "llm"
        except Exception:  # noqa: BLE001 - never let selection crash the report
            pass  # fall through to heuristic

    return _heuristic_key_picks(results, count), "heuristic"


class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


_TAG_LABELS = {"funding": "투자", "launch": "제품 출시"}


def _render_markdown(
    results: List[SourceResult],
    start: int,
    end: int,
    highlights_count: int,
    key_picks: List[KeyPick],
    key_picks_method: str,
) -> str:
    date_span = f"{_iso(start)[:10]} → {_iso(end)[:10]}"
    lines: List[str] = [f"# Weekly Product Digest ({date_span})", ""]

    total_items = sum(len(r.items) for r in results if r.ok)
    ok_sources = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    lines.append(
        f"**{total_items} items** from {len(ok_sources)} source(s)"
        + (f" · {len(failed)} source(s) unavailable" if failed else "")
    )
    lines.append("")

    # Headline: editorial Key Picks (focus = product launches & funding).
    if key_picks:
        method_label = "LLM 선별" if key_picks_method == "llm" else "키워드 선별"
        lines.append(f"## 🎯 Key Picks — 제품 출시 · 투자 우선 ({method_label})")
        lines.append("")
        for kp in key_picks:
            it = kp.item
            tag_str = " · ".join(_TAG_LABELS.get(t, t) for t in kp.tags)
            meta = [it.source_label]
            if tag_str:
                meta.append(tag_str)
            if it.published_iso:
                meta.append(it.published_iso[:10])
            lines.append(f"- **[{it.title}]({it.url})** — {' · '.join(meta)}")
            if kp.reason:
                lines.append(f"  - _{kp.reason}_")
        lines.append("")

    # Cross-source highlights: prefer items that expose a score.
    scored = [it for r in ok_sources for it in r.items if it.score is not None]
    scored.sort(key=lambda it: it.sort_key())
    if scored:
        lines.append("## ⭐ This Week's Highlights")
        lines.append("")
        for it in scored[:highlights_count]:
            lines.append(
                f"- **[{it.title}]({it.url})** "
                f"— {it.source_label} ({it.score} pts)"
            )
        lines.append("")

    # Per-source sections (structure preserved, never merged).
    for r in results:
        lines.append(f"## \U0001f4cc {r.label}")
        if not r.ok:
            lines.append(f"_Unavailable: {r.error}_")
            lines.append("")
            continue
        if not r.items:
            lines.append("_No items in this window._")
            lines.append("")
            continue
        for it in r.items:
            meta: List[str] = []
            if it.published_iso:
                meta.append(it.published_iso[:10])
            if it.score is not None:
                meta.append(f"{it.score} pts")
            if it.author:
                meta.append(f"by {it.author}")
            meta_str = f" — _{' · '.join(meta)}_" if meta else ""
            lines.append(f"### [{it.title}]({it.url}){meta_str}")
            if it.summary:
                lines.append(it.summary)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_json(
    results: List[SourceResult],
    start: int,
    end: int,
    key_picks: List[KeyPick],
    key_picks_method: str,
) -> str:
    payload = {
        "window": {
            "start_iso": _iso(start),
            "end_iso": _iso(end),
            "start_epoch": start,
            "end_epoch": end,
        },
        "key_picks_method": key_picks_method,
        "key_picks": [
            {
                "importance": kp.importance,
                "tags": kp.tags,
                "reason": kp.reason,
                "via": kp.via,
                "item": asdict(kp.item),
            }
            for kp in key_picks
        ],
        "sources": [
            {
                "key": r.key,
                "label": r.label,
                "ok": r.ok,
                "error": r.error,
                "count": len(r.items),
                "items": [asdict(it) for it in r.items],
            }
            for r in results
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------

class WeeklyReportInput(BaseModel):
    """Input for the weekly aggregated report."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    sources: Optional[List[str]] = Field(
        default=None,
        description=(
            "Which sources to include, by key. Valid keys: "
            "techcrunch, betanews, producthunt, indiehackers, hackernews, reddit. "
            "Omit to include all sources."
        ),
        max_length=12,
    )
    week_offset: int = Field(
        default=0,
        description="0 = most recent 7 days, 1 = the week before that, etc.",
        ge=0,
        le=52,
    )
    per_source_limit: int = Field(
        default=DEFAULT_PER_SOURCE_LIMIT,
        description="Max items to keep per source after ranking.",
        ge=1,
        le=50,
    )
    highlights_count: int = Field(
        default=5,
        description="How many score-based (most-upvoted) items to feature. 0 to hide.",
        ge=0,
        le=25,
    )
    key_picks_count: int = Field(
        default=8,
        description=(
            "How many editorial 'Key Picks' to feature at the very top, ranked by "
            "product-launch and funding signals across all sources. 0 to hide."
        ),
        ge=0,
        le=25,
    )
    key_pick_mode: str = Field(
        default=KEY_PICK_MODE_DEFAULT,
        description=(
            "How Key Picks are selected: 'auto' (LLM if an API key is configured, "
            "else keyword heuristic), 'llm' (force Claude-based selection), or "
            "'heuristic' (keyword scoring only, no API calls)."
        ),
        pattern="^(auto|llm|heuristic)$",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' for a human-readable report, 'json' for structured data.",
    )


class FetchSourceInput(BaseModel):
    """Input for fetching a single source."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    source: str = Field(
        ...,
        description=(
            "Source key to fetch. One of: techcrunch, betanews, producthunt, "
            "indiehackers, hackernews, reddit."
        ),
        min_length=2,
        max_length=40,
    )
    week_offset: int = Field(default=0, ge=0, le=52)
    per_source_limit: int = Field(default=DEFAULT_PER_SOURCE_LIMIT, ge=1, le=50)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="digest_list_sources",
    annotations={
        "title": "List Digest Sources",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def digest_list_sources() -> str:
    """List the news sources this server can aggregate.

    Returns:
        str: JSON with one entry per source:
        {
          "sources": [
            {"key": str, "label": str, "kind": str, "url": str}
          ]
        }
    """
    payload = {
        "sources": [
            {"key": s.key, "label": s.label, "kind": s.kind.value, "url": s.url}
            for s in SOURCES.values()
        ]
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


@mcp.tool(
    name="digest_weekly_report",
    annotations={
        "title": "Weekly Product Digest Report",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def digest_weekly_report(params: WeeklyReportInput) -> str:
    """Aggregate the past week's startup/product news across all sources into one report.

    Fetches every selected source in parallel, filters items to the requested 7-day
    window, ranks them (by score where available, else recency), and renders a report
    with per-source sections plus a cross-source highlights block. A source that fails
    to fetch is reported inline and never aborts the whole report.

    Args:
        params (WeeklyReportInput): Validated input containing:
            - sources (Optional[List[str]]): Source keys to include (default: all).
            - week_offset (int): 0 = last 7 days, 1 = the prior week, etc.
            - per_source_limit (int): Max items per source (default 10).
            - highlights_count (int): Featured cross-source items (default 8).
            - response_format (ResponseFormat): 'markdown' or 'json'.

    Returns:
        str: A markdown report, or (response_format='json') a JSON object:
        {
          "window": {"start_iso": str, "end_iso": str, "start_epoch": int, "end_epoch": int},
          "sources": [
            {
              "key": str, "label": str, "ok": bool, "error": str|null, "count": int,
              "items": [
                {
                  "source": str, "source_label": str, "title": str, "url": str,
                  "published_epoch": int, "published_iso": str,
                  "score": int|null, "author": str|null, "summary": str|null
                }
              ]
            }
          ]
        }

    Examples:
        - "Give me this week's product news" -> defaults.
        - "Last week's TechCrunch + Product Hunt only" ->
          sources=["techcrunch","producthunt"], week_offset=1.
        - "Raw data for a slide" -> response_format="json".
    """
    keys = params.sources if params.sources is not None else DEFAULT_SOURCES
    invalid = [k for k in keys if k not in SOURCES]
    if invalid:
        return (
            f"Error: Unknown source key(s): {', '.join(invalid)}. "
            f"Valid keys: {', '.join(SOURCES.keys())}."
        )

    results, start, end = await _collect(keys, params.week_offset, params.per_source_limit)
    key_picks, method = await _resolve_key_picks(
        results, params.key_picks_count, params.key_pick_mode
    )

    if params.response_format == ResponseFormat.JSON:
        return _render_json(results, start, end, key_picks, method)
    return _render_markdown(
        results, start, end, params.highlights_count, key_picks, method
    )


@mcp.tool(
    name="digest_fetch_source",
    annotations={
        "title": "Fetch a Single Digest Source",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def digest_fetch_source(params: FetchSourceInput) -> str:
    """Fetch just one source's items for the requested week (useful for debugging a source).

    Args:
        params (FetchSourceInput): Validated input containing:
            - source (str): One source key (techcrunch, betanews, producthunt,
              indiehackers, hackernews, reddit).
            - week_offset (int): 0 = last 7 days, 1 = the prior week, etc.
            - per_source_limit (int): Max items to return.
            - response_format (ResponseFormat): 'markdown' or 'json'.

    Returns:
        str: Same shape as digest_weekly_report but scoped to a single source,
        or "Error: ..." if the source key is invalid.
    """
    if params.source not in SOURCES:
        return (
            f"Error: Unknown source key '{params.source}'. "
            f"Valid keys: {', '.join(SOURCES.keys())}."
        )

    results, start, end = await _collect(
        [params.source], params.week_offset, params.per_source_limit
    )
    if params.response_format == ResponseFormat.JSON:
        return _render_json(results, start, end, key_picks=[], key_picks_method="none")
    return _render_markdown(
        results, start, end, highlights_count=0, key_picks=[], key_picks_method="none"
    )


def main() -> None:
    """Console-script entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
