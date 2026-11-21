#!/usr/bin/env python3
"""
한국 바이럴 콘텐츠 모니터
유튜브 / X / 인스타그램 / 틱톡 / 스레드에서 좋아요 N개 이상 콘텐츠를 모아
HTML 리포트 + CSV + (선택) 텔레그램 알림으로 보여줍니다.

사용법:
    python monitor.py            # 실제 수집
    python monitor.py --demo     # API 키 없이 샘플 데이터로 리포트 미리보기
"""
import argparse
import csv
import html
import json
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
KST = timezone(timedelta(hours=9))
HANGUL = re.compile(r"[가-힣]")
PLATFORMS = ["youtube", "x", "instagram", "tiktok", "threads"]
PLATFORM_LABEL = {"youtube": "유튜브", "x": "X", "instagram": "인스타그램",
                  "tiktok": "틱톡", "threads": "스레드"}


def log(msg):
    print(f"[{datetime.now(KST):%H:%M:%S}] {msg}", flush=True)


# ─────────────────────────── 공통 정규화 ───────────────────────────

def dig(obj, path):
    """'author.userName' 같은 점 경로로 값 꺼내기"""
    for key in path.split("."):
        if isinstance(obj, dict) and key in obj:
            obj = obj[key]
        else:
            return None
    return obj


def first(obj, paths):
    for p in paths:
        v = dig(obj, p)
        if v not in (None, ""):
            return v
    return None


def to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def parse_time(v):
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():
            ts = float(v)
            if ts > 1e12:
                ts /= 1000
            return datetime.fromtimestamp(ts, timezone.utc)
        s = str(v).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:  # 'Wed Oct 10 20:19:24 +0000 2018' (X 형식)
            dt = datetime.strptime(str(v), "%a %b %d %H:%M:%S %z %Y")
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


LIKE_KEYS = ["likeCount", "likesCount", "like_count", "diggCount", "favorite_count",
             "favoriteCount", "likes", "stats.diggCount", "public_metrics.like_count"]
VIEW_KEYS = ["playCount", "viewCount", "videoPlayCount", "videoViewCount", "view_count",
             "views", "stats.playCount", "public_metrics.impression_count"]
URL_KEYS = ["url", "postUrl", "post_url", "webVideoUrl", "link", "twitterUrl", "permalink"]
TEXT_KEYS = ["text", "fullText", "full_text", "caption", "text_content", "content", "desc", "title"]
AUTHOR_KEYS = ["ownerUsername", "username", "author.userName", "author.username",
               "authorMeta.name", "user.username", "author_username", "user.screen_name"]
TIME_KEYS = ["timestamp", "createdAt", "created_at", "createTimeISO", "createTime",
             "posted_at", "published_at", "taken_at", "date"]
ID_KEYS = ["id", "id_str", "shortCode", "post_code", "code", "postId", "post_id"]


def normalize(platform, raw):
    likes = to_int(first(raw, LIKE_KEYS))
    url = first(raw, URL_KEYS)
    if likes is None or not url:
        return None
    return {
        "platform": platform,
        "id": f"{platform}:{first(raw, ID_KEYS) or url}",
        "url": url,
        "title": (str(first(raw, TEXT_KEYS) or "")).strip().replace("\n", " ")[:200],
        "author": str(first(raw, AUTHOR_KEYS) or ""),
        "likes": likes,
        "views": to_int(first(raw, VIEW_KEYS)),
        "published": parse_time(first(raw, TIME_KEYS)),
    }


# ─────────────────────────── Apify 공통 ───────────────────────────

APIFY_MAX_USD = None


def run_apify(actor, actor_input, max_wait=900):
    """Apify 수집기를 실행하고 끝날 때까지 기다린 뒤 결과를 가져옴 (최대 15분)"""
    token = os.getenv("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_TOKEN 환경변수가 없습니다")
    api = "https://api.apify.com/v2"
    params = {"token": token, "waitForFinish": 60}
    if APIFY_MAX_USD:  # 수집기 1회 실행 비용 상한 (넘으면 Apify가 자동 중단)
        params["maxTotalChargeUsd"] = APIFY_MAX_USD
    r = requests.post(f"{api}/acts/{actor.replace('/', '~')}/runs",
                      params=params, json=actor_input, timeout=90)
    if r.status_code >= 400:
        raise RuntimeError(f"Apify 실행 실패 ({r.status_code}): {r.text[:200]}")
    run = r.json()["data"]
    waited = 60
    while run["status"] in ("READY", "RUNNING") and waited < max_wait:
        r = requests.get(f"{api}/actor-runs/{run['id']}",
                         params={"token": token, "waitForFinish": 60}, timeout=90)
        r.raise_for_status()
        run = r.json()["data"]
        waited += 60
        log(f"  {actor}: 수집 중... {waited // 60}분 경과")
    if run["status"] in ("READY", "RUNNING"):
        requests.post(f"{api}/actor-runs/{run['id']}/abort", params={"token": token}, timeout=30)
        log(f"  {actor}: {max_wait // 60}분 초과 → 중단하고 그때까지 모인 결과만 사용")
    elif run["status"] != "SUCCEEDED":
        log(f"  {actor}: 상태 {run['status']} → 모인 결과만 사용")
    r = requests.get(f"{api}/datasets/{run['defaultDatasetId']}/items",
                     params={"token": token, "clean": "true", "format": "json"}, timeout=120)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def apify_usage():
    """이번 결제 주기 Apify 사용 금액 (조회만, 비용 없음)"""
    token = os.getenv("APIFY_TOKEN")
    if not token:
        return None
    try:
        r = requests.get("https://api.apify.com/v2/users/me/limits", params={"token": token}, timeout=30)
        r.raise_for_status()
        d = r.json()["data"]
        return {"used": float(d.get("current", {}).get("monthlyUsageUsd") or 0),
                "limit": float(d.get("limits", {}).get("maxMonthlyUsageUsd") or 0),
                "reset": parse_time(d.get("monthlyUsageCycle", {}).get("endAt"))}
    except Exception as e:
        log(f"Apify 사용량 조회 실패: {e}")
        return None


# ─────────────────────────── 플랫폼별 수집 ───────────────────────────

REGION_NAME = {"KR": "한국"}
CROATIAN = re.compile(r"[čćžšđČĆŽŠĐ]")
SCRIPTS = {   # 제외용 문자 범위
    "indic": re.compile(r"[\u0900-\u0DFF]"),        # 힌디·벵골·펀잡·타밀·텔루구·말라얄람 등
    "arabic": re.compile(r"[\u0600-\u06FF\u0750-\u077F]"),  # 아랍·우르두
}


def excluded(region, sn):
    """나라별 제외 규칙: 특정 문자나 단어가 제목·채널명·설명·태그에 있으면 제외"""
    scripts = region.get("exclude_scripts") or []
    words = region.get("exclude_keywords") or []
    if not scripts and not words:
        return False
    text = " ".join([sn.get("title", ""), sn.get("channelTitle", ""),
                     sn.get("description", "")[:1500], " ".join(sn.get("tags", []) or [])])
    if any(SCRIPTS[sc].search(text) for sc in scripts if sc in SCRIPTS):
        return True
    low = text.lower()
    return any(re.search(r"(?<![a-z])" + re.escape(w.lower().lstrip("#")) + r"(?![a-z])", low) for w in words)


def lang_match(region, sn):
    """검색으로 찾은 영상이 그 나라 콘텐츠인지 판별"""
    lang = (sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "").lower()
    text = " ".join([sn.get("title", ""), sn.get("channelTitle", ""), sn.get("description", "")[:500]])
    want = (region.get("language") or "").lower()
    if want and lang.startswith(want):
        return True
    if want == "ko":
        return bool(HANGUL.search(text))
    if want == "hr":
        return lang[:2] in ("bs", "sr", "sh") or bool(CROATIAN.search(text))
    if want == "en":   # 언어 정보가 없으면 제목이 대부분 영문자일 때만
        if lang:
            return False
        t = sn.get("title", "")
        letters = [c for c in t if c.isalpha()]
        return bool(letters) and sum(c.isascii() for c in letters) / len(letters) > 0.9
    return not want


def fetch_youtube(cfg, since):
    key = os.getenv("YOUTUBE_API_KEY")
    if not key:
        raise RuntimeError("YOUTUBE_API_KEY 환경변수가 없습니다")
    base = "https://www.googleapis.com/youtube/v3"
    regions = cfg.get("regions") or [{"code": "KR", "name": "한국", "language": "ko",
                                       "search_queries": cfg.get("search_queries", [""])}]
    videos = {}        # 영상ID → 영상 정보
    found = {}         # 영상ID → {국가코드: 차트여부}

    def get(path, params):
        d = requests.get(f"{base}/{path}", params={**params, "key": key}, timeout=30).json()
        if "error" in d:
            raise RuntimeError(d["error"].get("message"))
        return d

    for rg in regions:
        code = rg["code"].upper()
        REGION_NAME[code] = rg.get("name", code)
        # 1) 인기 급상승 차트 (페이지당 1유닛)
        page = None
        for _ in range(rg.get("trending_pages", cfg.get("trending_pages", 4))):
            p = {"part": "snippet,statistics", "chart": "mostPopular", "regionCode": code, "maxResults": 50}
            if page:
                p["pageToken"] = page
            d = get("videos", p)
            for v in d.get("items", []):
                videos[v["id"]] = v
                found.setdefault(v["id"], {})[code] = True
            page = d.get("nextPageToken")
            if not page:
                break
        # 2) 최근 조회수순 검색 (검색 1회 100유닛)
        for q in rg.get("search_queries", [""]):
            p = {"part": "id", "type": "video", "regionCode": code, "order": "viewCount",
                 "publishedAfter": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "maxResults": cfg.get("search_max_results", 50)}
            if rg.get("language"):
                p["relevanceLanguage"] = rg["language"]
            if q:
                p["q"] = q
            for i in get("search", p).get("items", []):
                found.setdefault(i["id"]["videoId"], {}).setdefault(code, False)
    # 검색으로 찾은 영상 통계 가져오기 (50개당 1유닛)
    missing = [v for v in found if v not in videos]
    for i in range(0, len(missing), 50):
        for v in get("videos", {"part": "snippet,statistics", "id": ",".join(missing[i:i + 50])}).get("items", []):
            videos[v["id"]] = v

    by_code = {rg["code"].upper(): rg for rg in regions}
    out = []
    for vid, regs in found.items():
        v = videos.get(vid)
        if not v:
            continue
        st, sn = v.get("statistics", {}), v.get("snippet", {})
        ok = [c for c, from_chart in regs.items()
              if ((from_chart and cfg.get("trust_trending", True)) or lang_match(by_code[c], sn))
              and not excluded(by_code[c], sn)]
        if not ok:
            continue
        out.append({
            "platform": "youtube", "id": f"youtube:{vid}",
            "url": f"https://www.youtube.com/watch?v={vid}",
            "title": sn.get("title", ""), "author": sn.get("channelTitle", ""),
            "likes": to_int(st.get("likeCount")), "views": to_int(st.get("viewCount")),
            "published": parse_time(sn.get("publishedAt")), "korean_checked": True,
            "regions": [c for c in by_code if c in ok],
            "min_likes": min((by_code[c].get("min_likes") or 0) for c in ok) or None,
        })
    return [o for o in out if o["likes"] is not None]


def fetch_x(cfg, since, min_likes):
    if cfg.get("provider") == "official":
        token = os.getenv("X_BEARER_TOKEN")
        if not token:
            raise RuntimeError("X_BEARER_TOKEN 환경변수가 없습니다")
        r = requests.get("https://api.x.com/2/tweets/search/recent",
                         headers={"Authorization": f"Bearer {token}"},
                         params={"query": cfg.get("official_query"), "max_results": 100,
                                 "sort_order": "relevancy",
                                 "start_time": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 "tweet.fields": "public_metrics,created_at"}, timeout=30)
        r.raise_for_status()
        out = []
        for t in r.json().get("data", []):
            out.append({"platform": "x", "id": f"x:{t['id']}",
                        "url": f"https://x.com/i/web/status/{t['id']}",
                        "title": t.get("text", "")[:200], "author": "",
                        "likes": t["public_metrics"]["like_count"],
                        "views": t["public_metrics"].get("impression_count"),
                        "published": parse_time(t.get("created_at"))})
        return out
    # Apify: 검색 단계에서 min_faves 로 걸러서 과금 건수 최소화
    query = f"lang:ko min_faves:{min_likes} -filter:replies since:{since:%Y-%m-%d}"
    items = run_apify(cfg["apify_actor"], {"searchTerms": [query], "sort": "Top",
                                           "maxItems": cfg.get("max_items", 100)})
    return [n for n in (normalize("x", i) for i in items) if n]


def fetch_instagram(cfg, since):
    items = run_apify(cfg["apify_actor"], {"hashtags": cfg["hashtags"], "resultsType": "posts",
                                           "resultsLimit": cfg.get("results_per_hashtag", 20)})
    return [n for n in (normalize("instagram", i) for i in items) if n]


def fetch_tiktok(cfg, since):
    items = run_apify(cfg["apify_actor"], {"hashtags": cfg["hashtags"],
                                           "resultsPerPage": cfg.get("results_per_hashtag", 20),
                                           "shouldDownloadVideos": False,
                                           "shouldDownloadCovers": False})
    return [n for n in (normalize("tiktok", i) for i in items) if n]


def fetch_threads(cfg, since):
    items = run_apify(cfg["apify_actor"], {"mode": "search", "keywords": cfg["keywords"],
                                           "search_filter": "top",
                                           "start_date": f"{since:%Y-%m-%d}",
                                           "max_posts": cfg.get("max_posts_per_keyword", 30)})
    out = []
    for i in items:
        n = normalize("threads", i)
        if n and not n["url"].startswith("http"):
            n["url"] = f"https://www.threads.com/@{n['author']}/post/{n['url']}"
        if n:
            out.append(n)
    return out


# ─────────────────────────── 데모 데이터 ───────────────────────────

def demo_items(now):
    random.seed(7)
    samples = {
        "youtube": ["[MV] 신곡 뮤직비디오 공개", "편의점 신상 전부 먹어봤습니다", "요즘 난리난 그 챌린지 해봄"],
        "x": ["오늘 지하철에서 본 거 실화냐 ㅋㅋㅋ", "이 짤 너무 웃겨서 저장함", "속보) 내일 전국 첫눈 예보"],
        "instagram": ["성수동 새로 생긴 카페 후기", "겨울 데일리룩 모음", "10분 만에 만드는 자취 요리"],
        "tiktok": ["이 노래 챌린지 근황", "강아지가 처음 눈 본 반응", "회사에서 몰래 찍은 브이로그"],
        "threads": ["요즘 MZ 회식 문화 진짜 이럼?", "30대에 깨달은 것들", "오늘 퇴근길 하늘 미쳤다"],
    }
    out = []
    for p, titles in samples.items():
        for i, t in enumerate(titles * 2):
            out.append({"platform": p, "id": f"{p}:demo{i}", "url": "https://example.com",
                        "title": t, "author": f"user_{p}_{i}",
                        "likes": random.choice([800, 1200, 3400, 9800, 25000, 140000]),
                        "views": random.choice([None, 50000, 320000, 1800000]),
                        "published": now - timedelta(hours=random.randint(1, 46)),
                        "regions": [["KR"], ["US"], ["HR"], ["KR"], ["US", "HR"], ["KR"]][i]
                        if p == "youtube" else ["KR"]})
    return out


# ─────────────────────────── 저장 / 리포트 ───────────────────────────

def load_history():
    f = ROOT / "data" / "history.json"
    return json.loads(f.read_text("utf-8")) if f.exists() else {}


def save_history(hist):
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "history.json").write_text(json.dumps(hist, ensure_ascii=False, indent=1), "utf-8")


def save_cache(platform, items, now):
    (ROOT / "data").mkdir(exist_ok=True)
    rows = [{**i, "published": i["published"].isoformat() if i["published"] else None} for i in items]
    (ROOT / "data" / f"cache_{platform}.json").write_text(
        json.dumps({"fetched_at": now.isoformat(), "items": rows}, ensure_ascii=False), "utf-8")


def load_cache(platform):
    f = ROOT / "data" / f"cache_{platform}.json"
    if not f.exists():
        return None, []
    d = json.loads(f.read_text("utf-8"))
    fetched = datetime.fromisoformat(d["fetched_at"])
    items = []
    for i in d["items"]:
        i["published"] = parse_time(i["published"])
        i["cached_at"] = fetched
        items.append(i)
    return fetched, items


WEEKDAY = "월화수목금토일"


def build_schedule_notes(cfg, status, now, demo=False):
    """리포트 상단 '수집 주기' 안내 문구"""
    run_hour = 8
    wd = cfg.get("weekly_day", 0)
    daily = [p for p in PLATFORMS if cfg.get(p, {}).get("enabled") and cfg[p].get("frequency", "daily") != "weekly"]
    weekly = [p for p in PLATFORMS if cfg.get(p, {}).get("enabled") and cfg[p].get("frequency") == "weekly"]
    if demo:
        status = {p: {"weekly": p in weekly, "last": now, "failed": False} for p in daily + weekly}
    lines = []
    if daily:
        failed = [p for p in daily if status.get(p, {}).get("failed")]
        state = "오늘 수집" if not failed else "오늘 수집 실패: " + ", ".join(PLATFORM_LABEL[p] for p in failed)
        def label(p):
            rgs = cfg.get(p, {}).get("regions")
            return PLATFORM_LABEL[p] + (f"({'·'.join(r.get('name', r['code']) for r in rgs)})" if rgs else "")
        lines.append(f"{' · '.join(label(p) for p in daily)}: 매일 {run_hour:02d}시 수집 ({state})")
    if weekly:
        days_ahead = (wd - now.weekday()) % 7 or 7
        nxt = now + timedelta(days=days_ahead)
        groups = {}   # 마지막 수집일이 같은 플랫폼끼리 묶기
        for p in weekly:
            st = status.get(p, {})
            last = st.get("last")
            key = ("실패 · " if st.get("failed") else "") + (f"마지막 {last:%m/%d}" if last else "첫 수집 예정")
            groups.setdefault(key, []).append(p)
        for key, ps in groups.items():
            lines.append(f"{' · '.join(PLATFORM_LABEL[p] for p in ps)}: 매주 {WEEKDAY[wd]}요일 수집 "
                         f"({key} · 다음 {nxt:%m/%d})")
    lines.append(f"기준: 최근 {cfg.get('lookback_days', 7)}일 안에 올라온 콘텐츠 중 "
                 f"좋아요 {cfg.get('min_likes', 1000):,}개 이상")
    rgs = cfg.get("youtube", {}).get("regions") or []
    lower = [f"{r.get('name', r['code'])} {r['min_likes']:,}개" for r in rgs if r.get("min_likes")]
    if lower:
        lines.append("국가별 좋아요 기준(유튜브): " + ", ".join(lower))
    if cfg.get("korean_only"):
        lines.append("한국: 한국어 콘텐츠만 · 해외: 각 나라 인기 급상승 차트 + 그 나라 언어 콘텐츠")
    return lines


def fmt(n):
    if n is None:
        return "-"
    if n >= 10000:
        return f"{n / 10000:.1f}만".replace(".0만", "만")
    return f"{n:,}"


def write_csv(items, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["플랫폼", "국가", "좋아요", "조회수", "제목/본문", "작성자", "게시시각(KST)", "신규", "URL"])
        for it in items:
            pub = it["published"].astimezone(KST).strftime("%Y-%m-%d %H:%M") if it["published"] else ""
            w.writerow([PLATFORM_LABEL[it["platform"]], " · ".join(REGION_NAME.get(c, c) for c in it["regions"]),
                        it["likes"], it["views"] or "",
                        it["title"], it["author"], pub, "Y" if it["is_new"] else "", it["url"]])


def write_html(items, path, now, errors, notes=None, active=None, usage=None):
    active = active or PLATFORMS
    counts = {p: sum(1 for i in items if i["platform"] == p) for p in PLATFORMS}
    rows = []
    for it in items:
        pub = it["published"].astimezone(KST).strftime("%m/%d %H:%M") if it["published"] else "-"
        rows.append(
            f'<tr data-p="{it["platform"]}" data-r="{" ".join(it["regions"])}" data-new="{int(it["is_new"])}">'
            f'<td><span class="tag {it["platform"]}">{PLATFORM_LABEL[it["platform"]]}</span></td>'
            f'<td class="rg">{" · ".join(REGION_NAME.get(c, c) for c in it["regions"])}</td>'
            f'<td class="num" data-v="{it["likes"]}">{fmt(it["likes"])}</td>'
            f'<td class="num" data-v="{it["views"] or 0}">{fmt(it["views"])}</td>'
            f'<td class="t"><a href="{html.escape(it["url"])}" target="_blank" rel="noopener">'
            f'{html.escape(it["title"] or "(내용 없음)")}</a>'
            f'{" <b class=new>NEW</b>" if it["is_new"] else ""}</td>'
            f'<td>{html.escape(it["author"])}</td><td class="num">{pub}</td></tr>')
    err_html = "".join(f"<li>{PLATFORM_LABEL[p]}: {html.escape(e)}</li>" for p, e in errors.items())
    buttons = "".join(f'<button data-f="{p}">{PLATFORM_LABEL[p]} {counts[p]}</button>' for p in active)
    codes = list(dict.fromkeys(c for it in items for c in it["regions"]))
    codes = [c for c in REGION_NAME if c in codes] + [c for c in codes if c not in REGION_NAME]
    rbuttons = "".join(f'<button data-r="{c}">{REGION_NAME.get(c, c)} '
                       f'{sum(c in i["regions"] for i in items)}</button>' for c in codes)
    usage_html = ""
    if usage is not None:
        if usage:
            pct = (usage["used"] / usage["limit"] * 100) if usage["limit"] else 0
            reset = f" · {usage['reset'].astimezone(KST):%m/%d} 초기화" if usage.get("reset") else ""
            limit = f" / ${usage['limit']:.2f} ({pct:.0f}%)" if usage["limit"] else ""
            usage_txt = f"이번 달 사용 <b>${usage['used']:.2f}</b>{limit}{reset} · {now:%m/%d %H:%M} 기준"
            bar = f'<div class="ubar"><i style="width:{min(pct, 100):.0f}%"></i></div>' if usage["limit"] else ""
        else:
            usage_txt, bar = "사용량을 불러오지 못했습니다", ""
        usage_html = (f'<div class="usage"><div><b>Apify 비용</b> · {usage_txt}{bar}</div>'
                      f'<a class="btn" href="https://console.apify.com/billing" target="_blank" '
                      f'rel="noopener">Apify 사용량 보기 ↗</a></div>')
    page = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>바이럴 리포트 {now:%Y-%m-%d}</title><style>
body{{font-family:-apple-system,"Apple SD Gothic Neo","Noto Sans KR",sans-serif;margin:0;background:#f6f6f4;color:#1d1d1b}}
main{{max-width:1100px;margin:0 auto;padding:28px 20px}}
h1{{font-size:22px;margin:0 0 4px}} .sub{{color:#6b6b66;font-size:14px;margin-bottom:18px}}
.bar{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}}
button{{border:1px solid #d6d6d0;background:#fff;border-radius:999px;padding:6px 12px;font-size:13px;cursor:pointer}}
button.on{{background:#1d1d1b;color:#fff;border-color:#1d1d1b}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;font-size:14px}}
th,td{{padding:9px 10px;border-bottom:1px solid #eeeeea;text-align:left;vertical-align:top}}
th{{background:#fafaf8;font-weight:600;cursor:pointer;white-space:nowrap}}
.num{{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}}
.t a{{color:inherit;text-decoration:none}} .t a:hover{{text-decoration:underline}}
.tag{{font-size:12px;padding:2px 8px;border-radius:4px;color:#fff;white-space:nowrap}}
.youtube{{background:#d93025}} .x{{background:#111}} .instagram{{background:#c13584}}
.tiktok{{background:#25a3a3}} .threads{{background:#555}}
.new{{font-size:10px;color:#d93025;margin-left:4px}}
.info{{background:#fff;border:1px solid #e4e4de;padding:12px 16px;border-radius:8px;font-size:13px;margin-bottom:12px;line-height:1.7}}
.info ul,.err ul{{margin:4px 0 0;padding-left:18px}}
.usage{{display:flex;justify-content:space-between;align-items:center;gap:12px;background:#fff;border:1px solid #e4e4de;padding:12px 16px;border-radius:8px;font-size:13px;margin-bottom:12px}}
.ubar{{height:6px;background:#eeeeea;border-radius:3px;margin-top:6px;max-width:320px}} .ubar i{{display:block;height:100%;background:#2f8f5b;border-radius:3px}}
.btn{{white-space:nowrap;border:1px solid #1d1d1b;border-radius:999px;padding:6px 12px;color:#1d1d1b;text-decoration:none;font-size:13px}}
.btn:hover{{background:#1d1d1b;color:#fff}}
.lb{{font-size:12px;color:#6b6b66;align-self:center;width:44px}} .rg{{white-space:nowrap;font-size:13px;color:#4a4a45}}
.err{{background:#fff4e5;border:1px solid #f0d3a8;padding:10px 14px;border-radius:8px;font-size:13px;margin-bottom:14px}}
</style></head><body><main>
<h1>바이럴 콘텐츠 모니터 · 최근 {CFG_DAYS}일 · 좋아요 {CFG_MIN:,}+</h1>
<div class="sub">{now:%Y년 %m월 %d일 %H:%M} 기준 · 총 {len(items)}건 · 신규 {sum(i['is_new'] for i in items)}건</div>
{usage_html}
{('<div class="info"><b>수집 주기</b><ul>' + "".join(f"<li>{html.escape(n)}</li>" for n in notes) + '</ul></div>') if notes else ''}
{f'<div class="err">일부 플랫폼 수집 실패<ul>{err_html}</ul></div>' if errors else ''}
<div class="bar" id="pf"><span class="lb">플랫폼</span><button class="on" data-f="all">전체 {len(items)}</button>{buttons}
<button data-f="new">신규만</button></div>
{f'<div class="bar" id="rf"><span class="lb">국가</span><button class="on" data-r="all">전체</button>{rbuttons}</div>' if len(codes) > 1 else ''}
<table id="t"><thead><tr><th>플랫폼</th><th>국가</th><th data-s="2">좋아요 ↓</th><th data-s="3">조회수</th>
<th>제목 / 본문</th><th>작성자</th><th>게시</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
</main><script>
let pf='all',rf='all';
function apply(){{document.querySelectorAll('#t tbody tr').forEach(r=>{{
 const okP=pf==='all'||r.dataset.p===pf||(pf==='new'&&r.dataset.new==='1');
 const okR=rf==='all'||r.dataset.r.split(' ').includes(rf);
 r.style.display=(okP&&okR)?'':'none';}});}}
document.querySelectorAll('#pf button').forEach(b=>b.onclick=()=>{{
 document.querySelectorAll('#pf button').forEach(x=>x.classList.remove('on'));b.classList.add('on');pf=b.dataset.f;apply();}});
document.querySelectorAll('#rf button').forEach(b=>b.onclick=()=>{{
 document.querySelectorAll('#rf button').forEach(x=>x.classList.remove('on'));b.classList.add('on');rf=b.dataset.r;apply();}});
document.querySelectorAll('th[data-s]').forEach(th=>th.onclick=()=>{{
 const i=+th.dataset.s,tb=document.querySelector('#t tbody');
 [...tb.rows].sort((a,b)=>b.cells[i].dataset.v-a.cells[i].dataset.v).forEach(r=>tb.appendChild(r));}});
</script></body></html>"""
    Path(path).write_text(page, "utf-8")


def send_telegram(items, top_n, now):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        log("텔레그램: 토큰 없음 → 건너뜀")
        return
    new = [i for i in items if i["is_new"]][:top_n]
    lines = [f"<b>🔥 바이럴 리포트 {now:%m/%d}</b> · 총 {len(items)}건 (신규 {sum(i['is_new'] for i in items)})", ""]
    for i in new:
        rg = "" if i["regions"] == ["KR"] else "·" + "/".join(REGION_NAME.get(c, c) for c in i["regions"])
        lines.append(f"[{PLATFORM_LABEL[i['platform']]}{rg}] ❤️{fmt(i['likes'])} "
                     f"<a href=\"{html.escape(i['url'])}\">{html.escape(i['title'][:50] or '(내용 없음)')}</a>")
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                  json={"chat_id": chat, "text": "\n".join(lines), "parse_mode": "HTML",
                        "disable_web_page_preview": True}, timeout=30)
    log(f"텔레그램: {len(new)}건 전송")


# ─────────────────────────── 메인 ───────────────────────────

CFG_MIN = 1000
CFG_DAYS = 7


def main():
    global CFG_MIN, CFG_DAYS, APIFY_MAX_USD
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="API 없이 샘플 데이터로 실행")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--all", action="store_true", help="주 1회 플랫폼도 오늘 강제로 수집")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text("utf-8"))
    CFG_MIN = min_likes = cfg.get("min_likes", 1000)
    CFG_DAYS = cfg.get("lookback_days", 7)
    APIFY_MAX_USD = cfg.get("apify_max_usd_per_run")
    now = datetime.now(KST)
    since = (now - timedelta(days=cfg.get("lookback_days", 2))).astimezone(timezone.utc)

    collected, errors, status = [], {}, {}
    for rg in cfg.get("youtube", {}).get("regions") or []:
        REGION_NAME[rg["code"].upper()] = rg.get("name", rg["code"])
    if args.demo:
        collected = [i for i in demo_items(now) if cfg.get(i["platform"], {}).get("enabled")]
    else:
        fetchers = {
            "youtube": lambda c: fetch_youtube(c, since),
            "x": lambda c: fetch_x(c, since, min_likes),
            "instagram": lambda c: fetch_instagram(c, since),
            "tiktok": lambda c: fetch_tiktok(c, since),
            "threads": lambda c: fetch_threads(c, since),
        }
        weekly_day = cfg.get("weekly_day", 0)   # 0=월요일 (한국시간)
        for p, fn in fetchers.items():
            pc = cfg.get(p, {})
            if not pc.get("enabled"):
                continue
            weekly = pc.get("frequency", "daily") == "weekly"
            cached_at, cached = load_cache(p) if weekly else (None, [])
            due = (not weekly) or args.all or now.weekday() == weekly_day
            if not due and cached_at is None:   # 아직 수집 기록 없음 → 비용 아끼려 다음 수집일까지 대기
                status[p] = {"weekly": True, "last": None, "failed": False}
                log(f"{PLATFORM_LABEL[p]}: 수집 요일 아님 → 건너뜀")
                continue
            if not due:
                collected += cached
                status[p] = {"weekly": True, "last": cached_at, "failed": False}
                log(f"{PLATFORM_LABEL[p]}: 이번 주 수집분 사용 ({cached_at:%m/%d})")
                continue
            try:
                log(f"{PLATFORM_LABEL[p]}: 수집 시작")
                got = fn(pc)
                log(f"{PLATFORM_LABEL[p]}: {len(got)}건 수집")
                collected += got
                if weekly:
                    keep = [g for g in got if (g["likes"] or 0) >= min_likes]
                    save_cache(p, keep, now)
                status[p] = {"weekly": weekly, "last": now, "failed": False}
            except Exception as e:  # 한 플랫폼이 실패해도 나머지는 계속
                errors[p] = str(e)[:200]
                log(f"{PLATFORM_LABEL[p]}: 실패 - {e}")
                status[p] = {"weekly": weekly, "last": cached_at, "failed": True}
                if cached:
                    collected += cached

    for it in collected:
        it.setdefault("regions", ["KR"])
    # 필터: 좋아요 기준, 기간, 한국어
    items, seen_ids = [], set()
    for it in collected:
        if it["id"] in seen_ids or (it["likes"] or 0) < (it.get("min_likes") or min_likes):
            continue
        ref = (it["cached_at"] - timedelta(days=cfg.get("lookback_days", 7))) if it.get("cached_at") else since
        if it["published"] and it["published"] < ref:
            continue
        if cfg.get("korean_only") and not it.get("korean_checked") and not args.demo \
                and not HANGUL.search(it["title"] + it["author"]):
            continue
        seen_ids.add(it["id"]); items.append(it)
    items.sort(key=lambda i: i["likes"], reverse=True)

    # 이전 실행과 비교해 신규 표시
    hist = load_history()
    for it in items:
        it["is_new"] = it["id"] not in hist
        h = hist.setdefault(it["id"], {"first_seen": now.isoformat(), "platform": it["platform"],
                                       "url": it["url"], "title": it["title"][:80]})
        h["likes"], h["last_seen"] = it["likes"], now.isoformat()
    cutoff = (now - timedelta(days=30)).isoformat()   # 30일 지난 기록 정리
    hist = {k: v for k, v in hist.items() if v.get("last_seen", "") >= cutoff}
    if not args.demo:
        save_history(hist)

    notes = build_schedule_notes(cfg, status, now, args.demo)
    active = [p for p in PLATFORMS if cfg.get(p, {}).get("enabled")]
    uses_apify = any(cfg.get(p, {}).get("enabled") for p in ("instagram", "tiktok", "threads")) or \
        (cfg.get("x", {}).get("enabled") and cfg["x"].get("provider") == "apify")
    if args.demo:
        usage = {"used": 1.37, "limit": 5.0, "reset": now + timedelta(days=12)} if uses_apify else None
    else:
        usage = (apify_usage() or False) if uses_apify else None

    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    stamp = f"{now:%Y-%m-%d}" + ("-demo" if args.demo else "")
    write_csv(items, out / f"{stamp}.csv")
    write_html(items, out / f"{stamp}.html", now, errors, notes, active, usage)
    write_html(items, out / "index.html", now, errors, notes, active, usage)   # 항상 최신본
    log(f"리포트 저장: reports/{stamp}.html ({len(items)}건)")

    if cfg.get("telegram", {}).get("enabled") and not args.demo:
        try:
            send_telegram(items, cfg["telegram"].get("top_n", 15), now)
        except Exception as e:
            log(f"텔레그램 실패: {e}")

    if errors and not items:
        sys.exit(1)


if __name__ == "__main__":
    main()
