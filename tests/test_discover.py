"""Discover (M4): CBH parsing, matching, AniList/MangaBaka rows, catalog search."""

from __future__ import annotations

import json

import httpx
import respx
from fastapi.testclient import TestClient

from app.core.textmatch import clean_list_title, comic_score, edition_penalty, similarity
from app.domain import MediaType, SearchResult
from app.providers.discover.cbh import parse_list
from app.services.matching import pick, queries_for
from tests.conftest import admin_headers

AL = "https://graphql.anilist.co"
MB = "https://api.mangabaka.org/v1"
CBH = "https://www.comicbookherald.com"
CV = "https://comicvine.gamespot.com/api"

BEST_OF = """
<p>Intro paragraph with <strong>bold</strong> words that isn't a pick.</p>
<h3>#3) Saga Vol. 1 (2012)</h3><p>Great.</p>
<h3>#2) Monstress</h3><p>Also great.</p>
<h3>#1) Saga Vol. 1</h3><p>Dupe of #3 after cleaning.</p>
<h3>#4) Something Is Killing the Children</h3>
<h3>#5) Department of Truth</h3>
<h3>#6) Immortal Hulk</h3>
"""

POPULAR = """
<h3>The Most Popular Superhero Comics!</h3>
<p><strong>Absolute Batman</strong></p>
<p><strong>Ultimate Spider-Man</strong></p>
<p>Not bold at all.</p>
<h3>The Most Popular Manga!</h3>
<p><strong>Chainsaw Man</strong></p>
"""

READING_ORDER = """
<h2>The Beginning</h2>
<p><strong>Batman: Year One</strong></p><p>Collects Batman #404-407</p>
<p><strong>The Long Halloween</strong> Collects Batman: The Long Halloween #1-13</p>
<p><b>Read this next?</b></p>
"""


def test_text_rules() -> None:
    assert clean_list_title("#12) Saga Vol. 1 (2012)") == "Saga Vol. 1"
    assert similarity("The Amazing Spider-Man Vol. 2", "Amazing Spider-Man") == 1.0
    assert edition_penalty("Solo Leveling (Fan Colored)") is None
    assert edition_penalty("Solo Leveling [Official]") == 0.02
    _, eng = comic_score(
        title="Saga",
        name="Saga",
        publisher="Image",
        issues=66,
        year="2012",
        hint=None,
        has_cover=True,
    )
    _, foreign = comic_score(
        title="Saga",
        name="Saga",
        publisher="Panini Comics",
        issues=66,
        year="2012",
        hint=None,
        has_cover=True,
    )
    assert eng - foreign >= 50
    assert queries_for("Batman: Year One") == ["Batman: Year One", "Batman"]


def test_parse_lists() -> None:
    titles = [i.title for i in parse_list(BEST_OF)]
    assert titles[:2] == ["Saga Vol. 1", "Monstress"] and len(titles) == 5
    assert parse_list(BEST_OF)[0].hint == 2012
    assert [i.title for i in parse_list(POPULAR, "Superhero Comics")] == [
        "Absolute Batman",
        "Ultimate Spider-Man",
    ]
    assert [i.title for i in parse_list(POPULAR, "Manga")] == ["Chainsaw Man"]
    assert [i.title for i in parse_list(READING_ORDER)] == [
        "Batman: Year One",
        "The Long Halloween",
    ]


def test_pick_prefers_english_and_rejects_weak() -> None:
    def mk(i, name, pub, n=10):
        return SearchResult(
            str(i),
            name,
            MediaType.COMIC,
            publisher=pub,
            issue_count=n,
            provider="comicvine",
            cover_url="x",
        )

    best = pick(
        "Saga",
        None,
        [mk(1, "Saga", "Panini Comics", 60), mk(2, "Saga", "Image", 54), mk(3, "Sago", "Image")],
        [],
    )
    assert best and best[0].external_id == "2"
    assert pick("Saga", None, [mk(4, "Totally Different", "Image")], []) is None


def _anilist(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    v = body["variables"]
    media = [
        {
            "id": 10 + i,
            "title": {"english": f"Title {v.get('country')} {i}", "romaji": None, "native": None},
            "synonyms": [],
            "coverImage": {"large": "c"},
            "startDate": {"year": 2020},
            "chapters": None,
            "status": "RELEASING",
            "countryOfOrigin": v.get("country"),
            "isAdult": False,
            "format": "MANGA",
            "siteUrl": "s",
            "description": None,
        }
        for i in range(3)
    ]
    assert v.get("adult") is False
    return httpx.Response(200, json={"data": {"Page": {"media": media}}})


def test_manga_discover_rows(client: TestClient, setup_done) -> None:
    with respx.mock(assert_all_called=False) as r:
        r.post(AL).mock(side_effect=_anilist)
        mb = r.get(f"{MB}/series/search").respond(
            200,
            json={
                "data": [
                    {
                        "id": 5,
                        "title": "Completed One",
                        "type": "manga",
                        "cover": {"x250": {"x1": "u"}},
                        "source": {"anilist": {"id": 77}},
                        "total_chapters": "120",
                        "year": 2010,
                    },
                    {"id": 6, "title": "Merged", "merged_with": 5},
                ],
                "pagination": {},
            },
        )
        ctx = client.app.state.ctx
        from app.services import discover

        msg = client.portal.call(discover.refresh_manga, ctx)
        assert "10 row(s) refreshed" in msg
        params = str(mb.calls.last.request.url)
        assert "content_rating=safe" in params and "pornographic" not in params
    rows = client.get("/api/discover?media_type=manhwa").json()["rows"]
    assert [x["title"] for x in rows][:2] == ["Trending Manhwa", "Popular Ongoing Manhwa"]
    assert rows[0]["items"][0]["title"].startswith("Title KR")
    mb_row = next(x for x in rows if x["id"] == "manhwa:mb-completed")
    assert (
        mb_row["items"][0]["anilist_id"] == 77
        and mb_row["items"][0]["key"] == "mb:5"
        and len(mb_row["items"]) == 1
    )


def test_comic_discover_and_matching(client: TestClient, setup_done) -> None:
    h = admin_headers(setup_done)
    client.put(
        "/api/admin/settings",
        json={
            "values": {
                "comicvine.api_key": "k",
                "discover.comic.rows": ["cbh-all-time"],
                "discover.characters": ["Batman"],
                "discover.match_interval_seconds": 2,
            }
        },
        headers=h,
    )
    with respx.mock(assert_all_called=False) as r:

        def wp(request: httpx.Request) -> httpx.Response:
            slug = request.url.params.get("slug")
            html = BEST_OF if slug == "the-best-comics-of-all-time" else READING_ORDER
            return httpx.Response(
                200,
                json=[
                    {
                        "link": f"{CBH}/{slug}",
                        "content": {"rendered": html},
                        "title": {"rendered": "x"},
                    }
                ],
            )

        r.get(f"{CBH}/wp-json/wp/v2/posts").mock(side_effect=wp)
        r.get(f"{CV}/search/").mock(
            side_effect=lambda req: httpx.Response(
                200,
                json={
                    "status_code": 1,
                    "results": [
                        {
                            "id": abs(hash(req.url.params["query"])) % 100000,
                            "name": req.url.params["query"],
                            "publisher": {"name": "Image"},
                            "count_of_issues": 12,
                            "start_year": "2015",
                            "image": {"medium_url": "c"},
                        }
                    ],
                },
            )
        )
        r.get(f"{CV}/volumes/").respond(200, json={"status_code": 1, "results": []})
        r.get(url__startswith=f"{CV}/character/").respond(
            200, json={"status_code": 1, "results": {"volume_credits": []}}
        )
        ctx = client.app.state.ctx
        from app.services import discover

        msg = client.portal.call(discover.refresh_comics, ctx)
        assert "7 new title(s)" in msg, msg  # 5 best-of + 2 Batman
        rows = client.get("/api/discover?media_type=comic").json()["rows"]
        assert rows[0]["pending"] == 5 and rows[0]["items"] == []
        for _ in range(7):
            client.portal.call(ctx.matcher.step)
    rows = client.get("/api/discover?media_type=comic").json()["rows"]
    assert [i["title"] for i in rows[0]["items"]][:2] == ["Saga Vol. 1", "Monstress"]
    assert rows[1]["title"] == "Batman" and len(rows[1]["items"]) == 2
    st = client.get("/api/admin/discover/status").json()
    assert st["match"]["ok"] == 7 and st["match"]["pending"] == 0


def test_manga_search_falls_back_to_mangabaka(client: TestClient, setup_done) -> None:
    with respx.mock(assert_all_called=False) as r:
        r.post(AL).respond(500)
        r.get(f"{MB}/series/search").respond(
            200, json={"data": [{"id": 9, "title": "Found", "type": "manga"}]}
        )
        res = client.get("/api/search?media_type=manga&q=found").json()["results"]
        assert res[0]["provider"] == "mangabaka" and res[0]["request"] is None
