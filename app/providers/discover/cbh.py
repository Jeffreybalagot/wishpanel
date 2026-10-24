"""Comic Book Herald: curated comic lists and character reading orders.

Pages are fetched through WordPress's public REST API
(``/wp-json/wp/v2/posts|pages?slug=…``) and parsed with BeautifulSoup. CBH lists
collected-edition *titles*; the discover job resolves those to ComicVine volumes
in the background (``services/matching.py``), politely rate-limited.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
from bs4 import BeautifulSoup, Tag

from app.core.http import ExternalServiceError, request_with_retry
from app.core.ratelimit import throttle
from app.core.textmatch import clean_list_title, norm, year_hint
from app.providers._util import json_body
from app.providers.base import ProviderError

SERVICE = "Comic Book Herald"
SITE = "https://www.comicbookherald.com"
POPULAR_SLUG = "35-most-popular-comics-2020s-right-now"


@dataclass(frozen=True)
class ListDef:
    id: str
    title: str
    slug: str | None = None  # exact slug (last path segment)
    search: str | None = None  # WordPress search, newest matching post wins
    title_re: str | None = None  # the search result's title must match this
    section: str | None = None  # H2/H3 heading on the page whose picks to take
    character: str | None = None  # character/team name for reading-order rows
    cv: tuple[str, int] | None = None  # ComicVine fallback ("character"|"team", id)


@dataclass(frozen=True)
class ListItem:
    title: str
    hint: int | None = None


def _popular(section: str, label: str) -> ListDef:
    return ListDef(
        f"cbh-popular-{label.lower().replace(' ', '-')}",
        f"Most Popular {label}",
        slug=POPULAR_SLUG,
        section=section,
    )


def _year_lists() -> list[ListDef]:
    this = date.today().year
    return [
        ListDef(
            "cbh-best-this-year",
            f"Best Comics of {this}",
            search=f"Best Comics of {this}",
            title_re=rf"best comics of {this}",
        ),
        ListDef(
            "cbh-best-last-year",
            f"Best Comics of {this - 1}",
            search=f"Best Comics of {this - 1}",
            title_re=rf"best comics of {this - 1}",
        ),
    ]


LISTS: tuple[ListDef, ...] = (
    _popular("Superhero Comics", "Superhero Comics"),
    _popular("Manga", "Manga"),
    _popular("Indie Comics For Young Adults", "Indie YA Comics"),
    _popular("Comics For Kids", "Kids Comics"),
    _popular("Webtoons", "Webtoons"),
    _popular("Prestige Graphic Novels", "Graphic Novels"),
    ListDef(
        "cbh-monthly",
        "Recent Favorite Graphic Novels",
        search="My Favorite Graphic Novels of",
        title_re=r"favorite graphic novels of",
    ),
    *_year_lists(),
    ListDef(
        "cbh-decade-marvel",
        "Best Marvel Comics of the Decade",
        search="Best Marvel Comics of the 2020s",
        title_re=r"best marvel.*20[0-9]0s",
    ),
    ListDef(
        "cbh-decade-dc",
        "Best DC Comics of the Decade",
        search="Best DC Comics of the 2020s",
        title_re=r"best dc.*20[0-9]0s",
    ),
    ListDef(
        "cbh-decade-image",
        "Best Image Comics of the Decade",
        search="Best Image Comics of the 2020s",
        title_re=r"best image.*20[0-9]0s",
    ),
    ListDef(
        "cbh-decade-gn",
        "Best Graphic Novels of the Decade",
        slug="best-graphic-novels-of-the-2020s-decade",
    ),
    ListDef("cbh-all-time", "Best Comics of All Time", slug="the-best-comics-of-all-time"),
)

# name -> (CBH reading-order path, ComicVine fallback)
CHARACTERS: dict[str, tuple[str | None, tuple[str, int]]] = {
    "Avengers": ("where-do-i-start-with-avengers-trade-collections/", ("team", 3806)),
    "Batman": ("reading-dc-comics/batman-reading-order/", ("character", 1699)),
    "Black Panther": ("black-panther-reading-order/", ("character", 1477)),
    "Captain America": ("captain-america-reading-order-modern-marvel-era/", ("character", 1442)),
    "Daredevil": ("daredevil-reading-order/", ("character", 24694)),
    "Deadpool": ("deadpool-reading-order/", ("character", 7606)),
    "Doctor Doom": ("doctor-doom-reading-order/", ("character", 1468)),
    "Doctor Strange": (None, ("character", 1456)),
    "Fantastic Four": ("fantastic-four-reading-order/", ("team", 3804)),
    "Flash": ("reading-dc-comics/flash-reading-order/", ("character", 45114)),
    "Green Lantern": ("reading-dc-comics/green-lantern-reading-order/", ("character", 45112)),
    "Guardians of the Galaxy": ("guardians-of-the-galaxy-reading-order/", ("team", 25956)),
    "Harley Quinn": ("harley-quinn-reading-order/", ("character", 1696)),
    "Hulk": ("she-hulk-reading-order/", ("character", 2267)),
    "Iron Man": (
        "the-complete-marvel-reading-order-guide/marvel-fresh-start-reading-order/"
        "iron-man-2020-reading-order/",
        ("character", 1455),
    ),
    "Joker": (
        "reading-dc-comics/dc-rebirth-reading-order/year-four/batman-joker-war/",
        ("character", 1702),
    ),
    "Justice League": ("reading-dc-comics/justice-league-reading-order/", ("team", 22672)),
    "Moon Knight": ("moon-knight-reading-order/", ("character", 1493)),
    "Punisher": ("punisher-reading-order/", ("character", 1525)),
    "Silver Surfer": ("silver-surfer-reading-order/", ("character", 2502)),
    "Spider-Man": ("amazing-spider-man-reading-order-modern-marvel-era/", ("character", 1443)),
    "Supergirl": ("reading-dc-comics/supergirl-reading-order/", ("character", 2351)),
    "Superman": (
        "reading-dc-comics/superman-reading-order/death-of-superman/",
        ("character", 1807),
    ),
    "TMNT": ("teenage-mutant-ninja-turtles-comics-reading-order/", ("team", 26934)),
    "Thanos": ("the-complete-thanos-reading-order-guide/", ("character", 7607)),
    "Thor": ("thor-reading-order/", ("character", 2268)),
    "Venom": ("venom-reading-order/", ("character", 1486)),
    "Wolverine": (
        "the-complete-marvel-reading-order-guide/the-hunt-for-wolverine/",
        ("character", 1440),
    ),
    "Wonder Woman": (
        "reading-dc-comics/dc-absolute-universe-reading-order/absolute-wonder-woman/",
        ("character", 2048),
    ),
    "X-Men": ("the-complete-x-men-reading-order-guide-modern-marvel-comics-era/", ("team", 3173)),
}


def character_row_id(name: str) -> str:
    return "char-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def character_def(name: str) -> ListDef:
    path, cv = CHARACTERS.get(name, (None, None))  # type: ignore[assignment]
    slug = path.strip("/").split("/")[-1] if path else None
    return ListDef(character_row_id(name), name, slug=slug, character=name, cv=cv)


# --- Parsing -------------------------------------------------------------------

_NUMBERED = re.compile(r"^\s*#?\d{1,3}\s*[\).:\-–]\s*\S")
_JUNK = re.compile(
    r"^(collects?|read|buy|get|note|also|plus|see|start|next|previous|where|why|how|"
    r"what|honorable|bonus|update|related|table of contents|amazon|comixology)\b",
    re.I,
)


def _ok_title(text: str) -> bool:
    t = text.strip()
    return (
        2 <= len(t) <= 120 and not _JUNK.match(t) and not t.endswith("?") and len(t.split()) <= 16
    )


def _items_from(nodes: list[Tag]) -> list[ListItem]:
    """Picks under a run of sibling nodes: numbered headings, else bold-only paragraphs."""
    numbered: list[ListItem] = []
    bold: list[ListItem] = []
    for idx, node in enumerate(nodes):
        if node.name in ("h2", "h3", "h4"):
            text = node.get_text(" ", strip=True)
            if _NUMBERED.match(text):
                title = clean_list_title(text)
                if _ok_title(title):
                    numbered.append(ListItem(title, year_hint(text)))
        elif node.name == "p":
            strongs = node.find_all(["strong", "b"])
            if not strongs:
                continue
            text = node.get_text(" ", strip=True)
            strong_text = " ".join(s.get_text(" ", strip=True) for s in strongs).strip()
            # Whole paragraph bold (pick), or a bold lead followed by "Collects …" here or next.
            nxt = nodes[idx + 1].get_text(" ", strip=True) if idx + 1 < len(nodes) else ""
            whole = strong_text and len(strong_text) >= len(text) - 3
            collects = "collect" in text.lower() or nxt.lower().startswith("collect")
            if whole or collects:
                first = strongs[0].get_text(" ", strip=True)
                title = clean_list_title(first if collects and not whole else strong_text)
                if _ok_title(title):
                    bold.append(ListItem(title, year_hint(text)))
    picks = numbered if len(numbered) >= 5 else (bold or numbered)
    seen: set[str] = set()
    out = []
    for item in picks:
        key = norm(item.title)
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def parse_list(html: str, section: str | None = None) -> list[ListItem]:
    soup = BeautifulSoup(html, "html.parser")
    nodes = [n for n in soup.find_all(["h2", "h3", "h4", "p"])]
    if section:
        wanted = norm(section)
        start = next(
            (
                i
                for i, n in enumerate(nodes)
                if n.name in ("h2", "h3") and wanted in norm(n.get_text(" ", strip=True))
            ),
            None,
        )
        if start is None:
            return []
        level = nodes[start].name
        end = next(
            (j for j in range(start + 1, len(nodes)) if nodes[j].name in ("h2", level)), len(nodes)
        )
        nodes = nodes[start + 1 : end]
    return _items_from(nodes)


class ComicBookHeraldSource:
    name = "cbh"

    def __init__(self, client: httpx.AsyncClient, site: str = SITE) -> None:
        self.client = client
        self.site = site.rstrip("/")

    async def _wp(self, kind: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        await throttle("cbh")
        try:
            resp = await request_with_retry(
                self.client,
                SERVICE,
                "GET",
                f"{self.site}/wp-json/wp/v2/{kind}",
                params={"_fields": "content,link,title,date", **params},
                retries=2,
                timeout=30.0,
            )
        except ExternalServiceError as exc:
            raise ProviderError(
                f"Could not reach Comic Book Herald: {exc.message}.", retryable=True
            ) from exc
        if resp.status_code != 200:
            raise ProviderError(
                f"Comic Book Herald responded with HTTP {resp.status_code}.", retryable=True
            )
        body = json_body(resp, SERVICE)
        return body if isinstance(body, list) else []

    async def fetch(self, d: ListDef) -> tuple[str | None, str]:
        """(source URL, HTML) for a list definition."""
        posts: list[dict[str, Any]] = []
        if d.slug:
            posts = await self._wp("posts", {"slug": d.slug})
            if not posts:
                posts = await self._wp("pages", {"slug": d.slug})
        elif d.search:
            found = await self._wp("posts", {"search": d.search, "per_page": 10, "orderby": "date"})
            pattern = re.compile(d.title_re or re.escape(d.search), re.I)
            posts = [
                p
                for p in found
                if pattern.search(
                    BeautifulSoup(
                        (p.get("title") or {}).get("rendered", ""), "html.parser"
                    ).get_text()
                )
            ][:1]
        if not posts:
            raise ProviderError(f"Comic Book Herald page for “{d.title}” was not found.")
        post = posts[0]
        return post.get("link"), (post.get("content") or {}).get("rendered", "")

    async def get_list(self, d: ListDef) -> tuple[str | None, list[ListItem]]:
        url, html = await self.fetch(d)
        return url, parse_list(html, d.section)
