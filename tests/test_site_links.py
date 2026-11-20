"""블로그 사이트 안 연결 (pipeline/site_links.py) — 시술 글 ↔ 여행 글, 시술별 목록."""

import re

import pytest

from pipeline import common, site, site_links


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKIN_CONTENT_DIR", str(tmp_path / "content"))
    monkeypatch.setenv("SKIN_SPONSORS_FILE", str(tmp_path / "sponsors.yaml"))  # 스폰서 없음
    monkeypatch.delenv("TRACKER_URL", raising=False)
    return tmp_path


SPONSOR = {"id": "glow", "name_en": "Glow Skin Clinic", "official_url": "https://glow.example", "contract": {"type": "flat"}}


def post(slug, axis, title, keywords, date, sponsor=None):
    return {"slug": slug, "date": date, "draft": {
        "id": f"d-{slug}", "content_type": axis, "sponsor": sponsor, "topic": {"keywords": keywords}, "facts": [],
        "blog": {"title": title, "meta_description": f"About {title}.", "markdown": f"# {title}\n\nBody text."}}}


@pytest.fixture
def posts():
    return [
        post("rejuran-guide", "procedure", "Rejuran downtime and aftercare", ["rejuran"], "2026-10-16"),
        post("bukchon-walk", "procedure_travel", "Bukchon walk after a skin booster", ["skin booster", "bukchon"], "2026-10-14"),
        post("palace-laser", "procedure_travel", "Gyeongbokgung after laser toning", ["laser toning", "gyeongbokgung"], "2026-10-12"),
        post("juvelook-guide", "procedure", "Juvelook explained", ["juvelook"], "2026-10-19"),
        post("rejuran-trip-ad", "procedure_travel", "A Rejuran trip with Glow", ["rejuran"], "2026-10-20", sponsor=SPONSOR),
        post("how-many-days", "how_to", "How many days for a treatment trip", ["itinerary"], "2026-11-02"),
        post("thermage-guide", "procedure", "Thermage vs HIFU", ["thermage"], "2026-10-26"),
        post("gangnam-area", "procedure_travel", "Gangnam area guide", ["gangnam"], "2026-10-28"),
        post("laser-guide", "procedure", "Laser toning vs pico toning", ["laser toning"], "2026-11-09"),
    ]


def slugs(qs):
    return [q["slug"] for q in qs]


def by(posts, slug):
    return next(p for p in posts if p["slug"] == slug)


def test_procedure_post_links_to_same_treatment_travel_post(env, posts):
    rel = site_links.related(by(posts, "rejuran-guide"), posts)
    assert slugs(rel["trip"]) == ["bukchon-walk"]                      # 같은 계열(스킨부스터) 여행 글, 레이저 여행 글은 아님
    assert slugs(site_links.related(by(posts, "juvelook-guide"), posts)["trip"]) == ["bukchon-walk"]  # 쥬베룩 = 스킨부스터 계열
    assert slugs(site_links.related(by(posts, "laser-guide"), posts)["trip"]) == ["palace-laser"]
    # 이 시술의 여행 글이 없으면 어느 시술에나 쓰는 일정 글 (분류 '여행')
    assert slugs(site_links.related(by(posts, "thermage-guide"), posts)["trip"]) == ["how-many-days"]
    # 관련 시술 글: 같은 계열 먼저
    assert slugs(rel["more"])[0] == "juvelook-guide"


def test_travel_post_links_back_to_treatment_guides(env, posts):
    rel = site_links.related(by(posts, "bukchon-walk"), posts)
    assert set(slugs(rel["treatments"])) == {"rejuran-guide", "juvelook-guide"} and not rel["guides"]
    # 시술 설명 글이 없는 여행 글 → 최신 시술 설명 글 (일정 글은 시술 설명이 아니라 제외), 관련성을 주장하지 않는 제목으로
    gangnam = site_links.related(by(posts, "gangnam-area"), posts)
    assert not gangnam["treatments"] and slugs(gangnam["guides"]) == ["laser-guide", "thermage-guide"]


def test_related_never_includes_sponsored_posts(env, posts):
    for p in posts:
        for qs in site_links.related(p, posts).values():
            assert "rejuran-trip-ad" not in slugs(qs) and p["slug"] not in slugs(qs)


def test_treatment_pages_need_two_posts_and_include_family(env, posts):
    pages = site_links.treatment_pages(posts)
    assert slugs(pages["skin_booster"]) == ["rejuran-trip-ad", "juvelook-guide", "rejuran-guide", "bukchon-walk"]  # 게시일 순
    assert "juvelook" not in pages and "thermage_rf" not in pages and "travel" not in pages
    assert list(pages)[0] == "skin_booster"                              # 글 많은 시술 먼저
    html_out = site_links.render_treatment({**common.load_yaml("site.yaml"), "domain": ""}, "skin_booster", pages["skin_booster"])
    assert "Ad</span>" in html_out                                     # 목록의 스폰서 글은 첫 화면처럼 광고 배지
    assert html_out.index("Treatment guides") < html_out.index("Treatment × travel")


def test_post_page_shows_keep_reading_and_treatment_chips(env, posts):
    cfg = {**common.load_yaml("site.yaml"), "domain": "", "_treatments": [("skin_booster", "Skin boosters")]}
    pages = site_links.treatment_pages(posts)
    out = site.render_post(cfg, by(posts, "rejuran-guide"), posts=posts, pages=pages)
    related = re.search(r'<section class="related">(.*?)</section>', out, re.S).group(1)
    assert "Plan it around your trip" in related and 'href="/bukchon-walk/"' in related
    assert 'href="/treatments/skin-booster/"' in related and 'href="/treatments/rejuran/"' in related
    assert "glow.example" not in related and "rejuran-trip-ad" not in related
    assert out.index('class="related"') < out.index('class="sources"')    # 출처 앞, 병원 목록·광고 카드 뒤
    assert '<a href="/treatments/">Treatments</a>' in out                 # 머리말 메뉴
    # posts 를 안 주면 예전과 같다
    assert 'class="related"' not in site.render_post(cfg, by(posts, "rejuran-guide"))


def test_home_chips_sitemap_and_llms_link_treatment_pages(env, posts):
    extra = [post("botox-a", "procedure", "Botox basics", ["botox"], "2026-09-01"),
             post("botox-b", "procedure", "Jaw botox", ["botox"], "2026-09-02")]
    cfg = {**common.load_yaml("site.yaml"), "domain": "skinbound.example"}
    allp = posts + extra
    home = site.render_index(cfg, allp)
    chips = re.findall(r'class="chip" href="([^"]+)"', home)
    assert "/treatments/skin-booster/" in chips and "/treatments/botox/" in chips and "/#guides" not in chips
    sitemap = site.render_sitemap(cfg, allp)
    assert "https://skinbound.example/treatments/</loc>" in sitemap and "/treatments/laser-toning/" in sitemap
    assert "/treatments/skin-booster/" in site.render_llms_txt(cfg, allp)
    # 목록 페이지가 2개 이하면 칩 영역을 내지 않는다
    assert 'id="treatments"' not in site.render_index(cfg, posts[:3])
