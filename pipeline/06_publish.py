"""6. 게시 — ready_to_publish/ 의 게시물(사진 넘기기형·영상)만 다룬다 (다른 상태 폴더는 읽지도 옮기지도 않는다).
사진 넘기기형은 carousel_channels(인스타그램·틱톡)에만, 채널별 크기 이미지로 키트를 만든다.

예약 게시 도구·플랫폼 API 연결 전 단계: 채널별 **게시 키트**를 만든다 → 사람이(또는 나중에 게시 도구가) 그대로 올린다.
  - 채널: config/channels.yaml shortform.channels (tiktok, instagram, youtube)
  - 스폰서 글: sponsored_policy 에서 막힌 채널(틱톡)은 키트를 만들지 않고, 필요한 설정(브랜디드 콘텐츠 도구·18+·유료 프로모션 표시)을 체크리스트로
  - 모든 채널: 플랫폼 AI 생성 라벨 켜기 (AI 기본법 §31), 영상 내 표시 줄은 렌더링에 이미 들어 있음
  - 캡션: 제목·훅·블로그 링크(프로필 링크 안내)·해시태그 — 채널별 길이 제한 안
게시 후 "N 게시 완료 tiktok <URL>" → `done` 으로 기록, 계획한 채널을 모두 올리면 published/ 로 옮긴다.

    python -m pipeline.06_publish kits                          # 키트 생성 + 텔레그램 요약
    python -m pipeline.06_publish done <draft_id> tiktok https://www.tiktok.com/@.../video/...
    python -m pipeline.06_publish status
"""

from __future__ import annotations

import argparse
import sys
from urllib.parse import urlparse

from pipeline import sponsors
from pipeline.common import (
    content_dir,
    draft_dirs,
    get_logger,
    load_draft,
    load_json,
    load_yaml,
    move_draft,
    now_iso,
    run_cli,
    save_json,
    slugify,
)

log = get_logger("06_publish")

STAGE = "ready_to_publish"
ALLOWED = {(STAGE, "published")}
LIMITS = {"tiktok": 2200, "instagram": 2200, "youtube": 5000}
TITLE_LIMIT = {"youtube": 100}
POST_HOSTS = {"tiktok": ("tiktok.com",), "instagram": ("instagram.com",), "youtube": ("youtube.com", "youtu.be")}


def blog_url(draft: dict) -> str:
    site = load_yaml("site.yaml")
    base = f"https://{site['domain']}" if site.get("domain") else ""
    slug = slugify(draft["blog"].get("slug") or draft["blog"].get("title") or draft["id"], 60)
    return f"{base}/{slug}/" if base else f"/{slug}/"


def caption(draft: dict, channel: str) -> str:
    sf = draft.get("shortform") or {}
    sponsor = draft.get("sponsor")
    tags = " ".join(t if t.startswith("#") else f"#{t}" for t in sf.get("hashtags", []))
    head = [sf.get("title") or draft["blog"].get("title", "")]
    if sf.get("hook"):
        head.append(sf["hook"])
    if sponsor:
        head.insert(0, f"#ad {sponsors.short_disclosure(sponsor)}")
    link = blog_url(draft)
    where = "Full guide with sources: link in bio" if channel in ("tiktok", "instagram") else f"Full guide with sources: {link}"
    body = [*head, "", where, "Information only — not medical advice. AI-generated content.", "", tags]
    text = "\n".join(body).strip()
    limit = LIMITS.get(channel, 2200)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def kit(path) -> dict:
    """ready_to_publish/<id> 의 채널별 게시 키트 (kit.json + kit.md)."""
    if path.parent.name != STAGE:
        raise ValueError(f"06_publish 는 {STAGE}/ 만 처리합니다: {path}")
    draft = load_draft(path)
    sponsor = draft.get("sponsor")
    policy = sponsors.platform_policy()
    shortform = load_yaml("channels.yaml")["shortform"]
    channels = list(shortform["channels"])
    slides = load_json(path / "carousel.json") if (path / "carousel.json").exists() else None
    out = {"draft_id": draft["id"], "created_at": now_iso(), "blog_url": blog_url(draft), "channels": {}, "skipped": {}}
    if slides:  # 사진 넘기기형: 채널별 이미지 크기 (인스타그램 4:5, 틱톡 9:16)
        out["format"] = "carousel"
    else:
        out.update(video="video.mp4", captions_file="captions.srt")
    for ch in channels:
        if sponsor and ch in policy["blocked"]:
            out["skipped"][ch] = policy["blocked"][ch]
            continue
        if slides and ch not in shortform.get("carousel_channels", []):
            out["skipped"][ch] = "사진 넘기기형은 이 채널에 올릴 수 없음 (영상 형식 준비 후)"
            continue
        checklist = ["플랫폼 AI 생성 콘텐츠 라벨 켜기 (AI 기본법 §31)"]
        checklist.append("이미지를 순서대로 올리기" if slides else "자막 파일(captions.srt) 업로드 또는 자동 자막 확인")
        if slides and ch == "tiktok":
            checklist.append("음악은 틱톡 상업용 음악 라이브러리에서만 (트렌드 사운드 금지)")
        if sponsor:
            checklist = [*policy["requires"].get(ch, []), *checklist]
        entry = {"caption": caption(draft, ch), "checklist": checklist}
        if slides:
            entry["images"] = slides["files"]["tt" if ch == "tiktok" else "ig"]
        if ch in TITLE_LIMIT:
            entry["title"] = ((draft.get("shortform") or {}).get("title") or draft["blog"].get("title", ""))[:TITLE_LIMIT[ch]]
        out["channels"][ch] = entry
    save_json(path / "kit.json", out)
    media = "사진 넘기기형 (carousel/)" if slides else "영상: `video.mp4` · 자막: `captions.srt`"
    lines = [f"# 게시 키트 — {draft['id']}", "", f"{media} · 블로그: {out['blog_url']}", ""]
    for ch, e in out["channels"].items():
        lines += [f"## {ch}", *(f"- [ ] {c}" for c in e["checklist"])]
        if e.get("images"):
            lines += ["", "이미지: " + ", ".join(f"`{i}`" for i in e["images"])]
        if e.get("title"):
            lines += ["", f"제목: {e['title']}"]
        lines += ["", "```", e["caption"], "```", ""]
    for ch, why in out["skipped"].items():
        lines.append(f"- ⛔ {ch}: 올리지 않음 — {why}")
    (path / "kit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def publish_log(path) -> dict:
    f = path / "publish_log.json"
    return load_json(f) if f.exists() else {}


def done(draft_id: str, channel: str, url: str) -> str:
    """게시 완료 기록. 키트의 채널을 모두 올리면 published/ 로 옮긴다."""
    path = content_dir(STAGE) / draft_id
    if not (path / "kit.json").exists():
        raise ValueError(f"{STAGE}/{draft_id} 에 게시 키트가 없습니다 (kits 먼저)")
    planned = load_json(path / "kit.json")["channels"]
    if channel not in planned:
        raise ValueError(f"{channel} 은 이 글의 게시 채널이 아닙니다 ({', '.join(planned) or '없음'})")
    host = urlparse(url).netloc.lower().removeprefix("www.").removeprefix("m.")
    if urlparse(url).scheme != "https" or not any(host == h or host.endswith("." + h) for h in POST_HOSTS[channel]):
        raise ValueError(f"{channel} 게시물 주소가 아닙니다: {url}")
    record = publish_log(path)
    record[channel] = {"url": url, "at": now_iso()}
    save_json(path / "publish_log.json", record)
    left = [c for c in planned if c not in record]
    if left:
        return f"✅ {draft_id} {channel} 게시 기록 (남은 채널: {', '.join(left)})"
    move_draft(draft_id, STAGE, "published", ALLOWED)
    return f"✅ {draft_id} 모든 채널 게시 완료 → published"


def kits_message() -> str:
    lines = []
    for path in draft_dirs(STAGE):
        if (path / "kit.json").exists():
            continue
        k = kit(path)
        skipped = f" (제외: {', '.join(k['skipped'])})" if k["skipped"] else ""
        lines.append(f"- {k['draft_id']}: {', '.join(k['channels'])}{skipped}")
    if not lines:
        return ""
    return "\n".join(["📦 게시 키트 준비 — 폴더의 kit.md 대로 올린 뒤 'N 게시 완료 <채널> <URL>'", *lines])


def status_message() -> str:
    rows = []
    for path in draft_dirs(STAGE):
        planned = load_json(path / "kit.json")["channels"] if (path / "kit.json").exists() else {}
        posted = publish_log(path)
        rows.append(f"- {path.name}: " + (", ".join(f"{c}{'✅' if c in posted else '⏳'}" for c in planned) or "키트 없음"))
    return "\n".join(["[게시 대기]", *rows]) if rows else "게시 대기 없음"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="게시 키트·게시 기록 (ready_to_publish 전용)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("kits")
    sub.add_parser("status")
    d = sub.add_parser("done")
    d.add_argument("draft_id")
    d.add_argument("channel", choices=sorted(POST_HOSTS))
    d.add_argument("url")
    args = parser.parse_args(argv)
    if args.cmd == "kits":
        message = kits_message()
        if message:
            print(message)
        return 0
    if args.cmd == "status":
        print(status_message())
        return 0
    try:
        print(done(args.draft_id, args.channel, args.url))
    except ValueError as e:
        print(f"❓ {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    run_cli("06_publish", main)
