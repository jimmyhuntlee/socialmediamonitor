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

def run_apify(actor, actor_input, timeout=600):
    token = os.getenv("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_TOKEN 환경변수가 없습니다")
    url = f"https://api.apify.com/v2/acts/{actor.replace('/', '~')}/run-sync-get-dataset-items"
    r = requests.post(url, params={"token": token}, json=actor_input, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


# ─────────────────────────── 플랫폼별 수집 ───────────────────────────

def fetch_youtube(cfg, since):
    key = os.getenv("YOUTUBE_API_KEY")
    if not key:
        raise RuntimeError("YOUTUBE_API_KEY 환경변수가 없습니다")
    base = "https://www.googleapis.com/youtube/v3"
    videos, ids = [], set()

    # 1) 한국 인기 급상승 (페이지당 1유닛)
    page = None
    for _ in range(cfg.get("trending_pages", 4)):
        p = {"part": "snippet,statistics", "chart": "mostPopular", "regionCode": "KR",
             "maxResults": 50, "key": key}
        if page:
            p["pageToken"] = page
        d = requests.get(f"{base}/videos", params=p, timeout=30).json()
        if "error" in d:
            raise RuntimeError(d["error"].get("message"))
        for v in d.get("items", []):
            if v["id"] not in ids:
                ids.add(v["id"]); videos.append(v)
        page = d.get("nextPageToken")
        if not page:
            break

    # 2) 한국 지역 최근 영상 조회수순 검색 (검색 1회 100유닛)
    search_ids = []
    for q in cfg.get("search_queries", []):
        p = {"part": "id", "type": "video", "regionCode": "KR", "relevanceLanguage": "ko",
             "order": "viewCount", "publishedAfter": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "maxResults": cfg.get("search_max_results", 50), "key": key}
        if q:
            p["q"] = q
        d = requests.get(f"{base}/search", params=p, timeout=30).json()
        if "error" in d:
            raise RuntimeError(d["error"].get("message"))
        search_ids += [i["id"]["videoId"] for i in d.get("items", []) if i["id"]["videoId"] not in ids]
    for i in range(0, len(search_ids), 50):
        chunk = list(dict.fromkeys(search_ids[i:i + 50]))
        d = requests.get(f"{base}/videos", params={"part": "snippet,statistics",
                         "id": ",".join(chunk), "key": key}, timeout=30).json()
        for v in d.get("items", []):
            if v["id"] not in ids:
                ids.add(v["id"]); videos.append(v)

    out = []
    for v in videos:
        st, sn = v.get("statistics", {}), v.get("snippet", {})
        out.append({
            "platform": "youtube", "id": f"youtube:{v['id']}",
            "url": f"https://www.youtube.com/watch?v={v['id']}",
            "title": sn.get("title", ""), "author": sn.get("channelTitle", ""),
            "likes": to_int(st.get("likeCount")), "views": to_int(st.get("viewCount")),
            "published": parse_time(sn.get("publishedAt")), "korean_checked": True,
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
                        "published": now - timedelta(hours=random.randint(1, 46))})
    return out


# ─────────────────────────── 저장 / 리포트 ───────────────────────────

def load_history():
    f = ROOT / "data" / "history.json"
    return json.loads(f.read_text("utf-8")) if f.exists() else {}


def save_history(hist):
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "history.json").write_text(json.dumps(hist, ensure_ascii=False, indent=1), "utf-8")


def fmt(n):
    if n is None:
        return "-"
    if n >= 10000:
        return f"{n / 10000:.1f}만".replace(".0만", "만")
    return f"{n:,}"


def write_csv(items, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["플랫폼", "좋아요", "조회수", "제목/본문", "작성자", "게시시각(KST)", "신규", "URL"])
        for it in items:
            pub = it["published"].astimezone(KST).strftime("%Y-%m-%d %H:%M") if it["published"] else ""
            w.writerow([PLATFORM_LABEL[it["platform"]], it["likes"], it["views"] or "",
                        it["title"], it["author"], pub, "Y" if it["is_new"] else "", it["url"]])


def write_html(items, path, now, errors):
    counts = {p: sum(1 for i in items if i["platform"] == p) for p in PLATFORMS}
    rows = []
    for it in items:
        pub = it["published"].astimezone(KST).strftime("%m/%d %H:%M") if it["published"] else "-"
        rows.append(
            f'<tr data-p="{it["platform"]}" data-new="{int(it["is_new"])}">'
            f'<td><span class="tag {it["platform"]}">{PLATFORM_LABEL[it["platform"]]}</span></td>'
            f'<td class="num" data-v="{it["likes"]}">{fmt(it["likes"])}</td>'
            f'<td class="num" data-v="{it["views"] or 0}">{fmt(it["views"])}</td>'
            f'<td class="t"><a href="{html.escape(it["url"])}" target="_blank" rel="noopener">'
            f'{html.escape(it["title"] or "(내용 없음)")}</a>'
            f'{" <b class=new>NEW</b>" if it["is_new"] else ""}</td>'
            f'<td>{html.escape(it["author"])}</td><td class="num">{pub}</td></tr>')
    err_html = "".join(f"<li>{PLATFORM_LABEL[p]}: {html.escape(e)}</li>" for p, e in errors.items())
    buttons = "".join(f'<button data-f="{p}">{PLATFORM_LABEL[p]} {counts[p]}</button>' for p in PLATFORMS)
    page = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>한국 바이럴 리포트 {now:%Y-%m-%d}</title><style>
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
.err{{background:#fff4e5;border:1px solid #f0d3a8;padding:10px 14px;border-radius:8px;font-size:13px;margin-bottom:14px}}
</style></head><body><main>
<h1>한국 바이럴 콘텐츠 · 좋아요 {CFG_MIN:,}+</h1>
<div class="sub">{now:%Y년 %m월 %d일 %H:%M} 기준 · 총 {len(items)}건 · 신규 {sum(i['is_new'] for i in items)}건</div>
{f'<div class="err">일부 플랫폼 수집 실패<ul>{err_html}</ul></div>' if errors else ''}
<div class="bar"><button class="on" data-f="all">전체 {len(items)}</button>{buttons}
<button data-f="new">신규만</button></div>
<table id="t"><thead><tr><th>플랫폼</th><th data-s="1">좋아요 ↓</th><th data-s="2">조회수</th>
<th>제목 / 본문</th><th>작성자</th><th>게시</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
</main><script>
document.querySelectorAll('.bar button').forEach(b=>b.onclick=()=>{{
 document.querySelectorAll('.bar button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
 const f=b.dataset.f;document.querySelectorAll('#t tbody tr').forEach(r=>{{
  r.style.display=(f==='all'||r.dataset.p===f||(f==='new'&&r.dataset.new==='1'))?'':'none';}});}});
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
    lines = [f"<b>🔥 한국 바이럴 {now:%m/%d}</b> · 총 {len(items)}건 (신규 {sum(i['is_new'] for i in items)})", ""]
    for i in new:
        lines.append(f"[{PLATFORM_LABEL[i['platform']]}] ❤️{fmt(i['likes'])} "
                     f"<a href=\"{html.escape(i['url'])}\">{html.escape(i['title'][:50] or '(내용 없음)')}</a>")
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                  json={"chat_id": chat, "text": "\n".join(lines), "parse_mode": "HTML",
                        "disable_web_page_preview": True}, timeout=30)
    log(f"텔레그램: {len(new)}건 전송")


# ─────────────────────────── 메인 ───────────────────────────

CFG_MIN = 1000


def main():
    global CFG_MIN
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="API 없이 샘플 데이터로 실행")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text("utf-8"))
    CFG_MIN = min_likes = cfg.get("min_likes", 1000)
    now = datetime.now(KST)
    since = (now - timedelta(days=cfg.get("lookback_days", 2))).astimezone(timezone.utc)

    collected, errors = [], {}
    if args.demo:
        collected = demo_items(now)
    else:
        fetchers = {
            "youtube": lambda c: fetch_youtube(c, since),
            "x": lambda c: fetch_x(c, since, min_likes),
            "instagram": lambda c: fetch_instagram(c, since),
            "tiktok": lambda c: fetch_tiktok(c, since),
            "threads": lambda c: fetch_threads(c, since),
        }
        for p, fn in fetchers.items():
            pc = cfg.get(p, {})
            if not pc.get("enabled"):
                continue
            try:
                got = fn(pc)
                log(f"{PLATFORM_LABEL[p]}: {len(got)}건 수집")
                collected += got
            except Exception as e:  # 한 플랫폼이 실패해도 나머지는 계속
                errors[p] = str(e)[:200]
                log(f"{PLATFORM_LABEL[p]}: 실패 - {e}")

    # 필터: 좋아요 기준, 기간, 한국어
    items, seen_ids = [], set()
    for it in collected:
        if it["id"] in seen_ids or (it["likes"] or 0) < min_likes:
            continue
        if it["published"] and it["published"] < since:
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

    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    stamp = f"{now:%Y-%m-%d}" + ("-demo" if args.demo else "")
    write_csv(items, out / f"{stamp}.csv")
    write_html(items, out / f"{stamp}.html", now, errors)
    write_html(items, out / "index.html", now, errors)   # 항상 최신본
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
