"""宿のペット同伴可否を、県の参画施設一覧（施設名）と各宿の公式サイトから推定する。

県の一覧 CSV にペットの列は無い（2026-10-19 に大分・鹿児島・福岡で確認）。そのため
  1. 施設名に「ペット可」「愛犬と」などがあればそれで決める
  2. なければ一覧の「ホームページ」列の URL を取得し、本文の言い回しで判定する
     （トップページに無ければ、ペット・犬を名乗るリンクを2つまで辿る）
判定は1施設につき PET_RECHECK_DAYS 日に1回だけ取り直し、state.json の "pet" に覚えておく。

判定値:
  可      ペット同伴可と読める言い回しだけがある
  要確認  可と不可の両方がある（「一部の部屋のみ可」など）
  不可    不可と読める言い回しだけがある
  不明    どちらも無い・サイトが無い・取得できない
"""

import datetime as dt
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

PET_RECHECK_DAYS = 30
PET_BATCH = 80  # 1回の巡回で新しく調べる施設数の上限（10分間隔に収める）

POS_RE = re.compile(
    r"ペット(?:同伴|宿泊|連れ|と一緒|と泊|とお泊|と過ご|ルーム|対応|専用|可|ＯＫ|OK|ok|も泊|も一緒|もご一緒)"
    r"|ペット(?:同伴|宿泊)?[：:]?\s*\n\s*(?:可|OK|○|◯)(?=\s|$)"  # 表の「ペット｜可」が行で分かれた形
    r"|愛犬|ワンちゃん|わんちゃん|わんこ|ドッグラン|ドッグフリー|小型犬|犬と(?:泊|一緒|過ご|旅)|犬連れ|犬同伴|with\s*dogs?|pet[\s-]?friendly",
    re.I)
NEG_RE = re.compile(
    r"ペット[^。\n]{0,20}(?:不可|お断り|ご遠慮|できません|禁止|NG)"
    r"|(?:ペット|動物)(?:の|を)?(?:同伴|持ち込み|お持ち込み|入館|宿泊)[^。\n]{0,12}(?:不可|お断り|ご遠慮|できません|禁止)"
    r"|ペット[^。\n]{0,6}\s*\n\s*(?:不可|NG|×|✕)"  # 表の「ペット同伴｜不可」が行で分かれた形
    r"|no\s+pets", re.I)
# 「ペットと泊まれますか？」のような FAQ の問いは、答えの側で決める
QUESTION_RE = re.compile(r"^[^。\n]{0,20}[？?]")
REFUSAL_RE = re.compile(r"申し訳|恐れ入り|お断り|できません|ご遠慮|いただけません|不可")
# 補助犬だけの言及は「可」と読まない
SERVICE_DOG_RE = re.compile(r"(?:盲導犬|介助犬|聴導犬|身体障害者補助犬|補助犬)")
# 予約サイトや SNS の URL は、宿の情報ではなくサイト共通のメニューを拾うので見ない
SKIP_HOST_RE = re.compile(
    r"(?:jalan|rakuten|ikyu|booking|expedia|agoda|trip\.com|yahoo|instagram|facebook|twitter|x\.com"
    r"|tiktok|line\.me|airbnb|google|youtube|rurubu|jtb|knt|yadoken|relux|stayjapan|hotels\.com)", re.I)
LINK_RE = re.compile(r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.I | re.S)


def classify_text(text):
    """本文から (判定, 根拠の抜粋) を返す。"""
    pos = [m for m in POS_RE.finditer(text)
           if not SERVICE_DOG_RE.search(text[max(0, m.start() - 15):m.end() + 15])]
    neg = list(NEG_RE.finditer(text))
    asked = [m for m in pos if QUESTION_RE.search(text[m.end():m.end() + 25])]
    pos = [m for m in pos if m not in asked]
    refused = [m for m in asked if REFUSAL_RE.search(text[m.end():m.end() + 80])]
    # 「ペット不可」は POS の「ペット…可」にも当たるので、NEG に重なる POS は数えない
    pos = [m for m in pos if not any(n.start() <= m.start() < n.end() for n in neg)]
    snip = lambda m: re.sub(r"\s+", " ", text[max(0, m.start() - 20):m.end() + 20]).strip()
    if pos and neg:
        return "要確認", snip(pos[0])
    if pos:
        return "可", snip(pos[0])
    if neg or refused:
        return "不可", snip((neg or refused)[0])
    return "不明", ""


def normalize_url(u):
    u = (u or "").strip().split()[0] if (u or "").strip() else ""
    if not u or "." not in u:
        return ""
    if not re.match(r"https?://", u, re.I):
        u = "https://" + u.lstrip("/")
    return u


def check_site(url, fetch, decode, html_to_lines):
    page = decode(*fetch(url, timeout=15))
    text = "\n".join(html_to_lines(page))
    verdict, why = classify_text(text)
    if verdict in ("可", "要確認"):
        return verdict, why
    # トップに無ければ、ペット・犬を名乗るリンクを2つまで見る（同じサイトの中だけ）
    host = urllib.parse.urlsplit(url).netloc
    seen = set()
    for href, label in LINK_RE.findall(page):
        if len(seen) >= 2:
            break
        if not re.search(r"pet|dog|ペット|犬|わん|ワン", href + label, re.I):
            continue
        link = urllib.parse.urljoin(url, href)
        if urllib.parse.urlsplit(link).netloc != host or link in seen:
            continue
        seen.add(link)
        try:
            sub = "\n".join(html_to_lines(decode(*fetch(link, timeout=15))))
        except Exception:  # noqa: BLE001
            continue
        v, w = classify_text(sub)
        if v != "不明":
            return v, w
    return verdict, why


def judge(name, hp, fetch, decode, html_to_lines):
    v, why = classify_text(name)
    if v != "不明":
        return {"pet": v, "why": f"施設名: {why}"}
    url = normalize_url(hp)
    if not url or SKIP_HOST_RE.search(urllib.parse.urlsplit(url).netloc):
        return {"pet": "不明", "why": "公式サイトなし"}
    try:
        v, why = check_site(url, fetch, decode, html_to_lines)
    except Exception as e:  # noqa: BLE001
        return {"pet": "不明", "why": f"取得できない: {type(e).__name__}"[:80]}
    return {"pet": v, "why": why[:80]}


def update(cache, targets, today, fetch, decode, html_to_lines, limit=PET_BATCH):
    """targets: {キー: (施設名, ホームページ)}。期限切れ・未調査のものを limit 件まで調べて cache を更新する。"""
    def stale(key):
        e = cache.get(key)
        if not e:
            return True
        return (dt.date.fromisoformat(today) - dt.date.fromisoformat(e["at"])).days >= PET_RECHECK_DAYS

    # 施設名で決まるものは取得が要らないので上限に数えない
    todo = []
    for key, (name, hp) in targets.items():
        if not stale(key):
            continue
        v, why = classify_text(name)
        if v != "不明":
            cache[key] = {"pet": v, "why": f"施設名: {why}"[:80], "at": today}
        else:
            todo.append((key, name, hp))
    todo.sort(key=lambda t: (t[0] in cache, t[0]))  # 未調査を先に
    todo = todo[:limit]

    def run(t):
        key, name, hp = t
        return key, judge(name, hp, fetch, decode, html_to_lines)

    with ThreadPoolExecutor(max_workers=16) as ex:
        for key, res in ex.map(run, todo):
            cache[key] = dict(res, at=today)
    return len(todo)


PET_OK = ("可", "要確認")


def mark(entry):
    """通知の行末に付ける印。"""
    if not entry:
        return ""
    if entry["pet"] == "可":
        return " 🐶ペット可"
    if entry["pet"] == "要確認":
        return " 🐶ペット可の記載あり（条件を要確認）"
    return ""
