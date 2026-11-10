"""인스타그램 프로필 링크 도착 페이지 /ig/ (pipeline/ig_page.py) 와 릴스 → 홈페이지 지표 (reels.traffic)."""

from datetime import date

from pipeline import ig_page, reels, site


def post(slug, sponsor=None, content_type="procedure"):
    draft = {"id": slug, "content_type": content_type, "blog": {"title": f"T {slug}", "meta_description": "d"}}
    if sponsor:
        draft["sponsor"] = sponsor
    return {"slug": slug, "date": "2026-11-01", "draft": draft}


CFG = {
    "topics": {"rejuran": {"title": "Rejuran", "posts": ["rej", "missing"]},
               "procedures": {"title": "Skin boosters", "posts": ["juv", "paid"]}},
    "landing": {"enabled": True, "trip_title": "Trip", "trip_posts": ["trip", "adcard"], "intro": "Personal experiences."},
}


def no_ad(p):
    return None


def test_sections_follow_config_and_drop_missing_and_ads():
    posts = [post("rej"), post("juv"), post("paid", sponsor={"id": "glow"}), post("trip", content_type="procedure_travel"),
             post("adcard", content_type="procedure_travel")]
    rows = ig_page.sections(posts, CFG, ad_for=lambda p: {"id": "x"} if p["slug"] == "adcard" else None)
    assert [(t, [p["slug"] for p in ps]) for t, ps in rows] == [("Rejuran", ["rej"]), ("Skin boosters", ["juv"]), ("Trip", ["trip"])]


def test_render_is_noindex_and_links_only_listed_posts():
    html = ig_page.render(site.config(), [post("rej"), post("juv"), post("other")], CFG, ad_for=no_ad)
    assert 'content="noindex"' in html and 'href="/rej/"' in html and 'href="/juv/"' in html
    assert 'href="/other/"' not in html and "Personal experiences." in html


def test_render_skips_page_when_nothing_to_show():
    assert ig_page.render(site.config(), [post("other")], CFG, ad_for=no_ad) is None


def test_landing_config_points_at_real_settings():
    cfg = ig_page.settings()
    assert cfg["landing"]["link_label"] and all("posts" in t for t in cfg["topics"].values())


def stats_with(label_clicks, ig=0):
    def fn(start, end):
        n = label_clicks.get(start)
        by_link = [{"code": "a9", "label": "Instagram profile", "clicks": n}] if n is not None else []
        return {"by_link": by_link, "by_source": [{"source": "instagram", "clicks": ig}, {"source": "blog", "clicks": 9}]}
    return fn


def test_traffic_counts_profile_link_and_ig_page():
    day = date(2026, 11, 9)
    text = reels.traffic(7, views=4000, today=day, stats_fn=stats_with({date(2026, 11, 3): 6, date(2026, 10, 27): 2}, ig=7),
                         requests_fn=lambda a, b: {"/ig/": 11, "/": 50})
    assert "홈페이지 도착 (프로필 링크 누름): 6 · 지난 7일 2" in text
    assert "클릭 전체: 7" in text and "1,000회당 도착 1.50" in text and "/ig/ 페이지 요청: 11" in text


def test_traffic_explains_missing_link_and_failed_requests():
    def broken(a, b):
        raise RuntimeError("no token")
    text = reels.traffic(7, today=date(2026, 11, 9), stats_fn=stats_with({}), requests_fn=broken)
    assert "tracker add --target" in text and "확인 못 함 (no token)" in text
