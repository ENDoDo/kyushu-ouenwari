"""1軒の宿の料金を予約サイト横断で見張る（いまはアイランド長崎・12/6〜1泊）。

各サイトの「指定日・指定人数」の検索結果からプランごとの合計料金を取り出し、
クーポン・ポイントを差し引いた「実質」の最安を食事タイプ別に比べる。

- 毎朝（check.py の現状まとめと同じタイミング）にサイト別の最安をまとめて出す
- 良い割引が出たら即時に知らせる（下の deals() の条件）

  python watch/hotel.py   # 取得結果とまとめ・即時通知の文面を標準出力に出す（state は書かない）
"""

import datetime as dt
import html
import http.cookiejar
import json
import os
import re
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130 Safari/537.36")

HOTEL = {
    "name": "アイランド長崎（i+Land nagasaki）",
    "checkin": dt.date(2026, 12, 6),
    "nights": 1,
    "adults": 2,
    # 犬（ポメラニアン1匹）連れなので、ペット同伴のプランだけを見る（プラン名＋部屋名で照合）。
    # 施設のグランピング（GRAX）はペット不可で、犬と泊まれるのはドッグラン付きの BARK LODGE だけ
    "plan_filter": r"ワンちゃん|愛犬|ペット|BARK",
    "party": "大人2名・犬1匹",
    # 予約サイトごとの施設ID
    "official": "islandnagasaki",  # reserve.489ban.net/client/<ここ>/0/
    "jalan": "319516",             # www.jalan.net/yad<ここ>/
    "rakuten": "18933",            # hotel.travel.rakuten.co.jp/hinfo/<ここ>/
}
INTERVAL_MIN = 30  # 料金の取得間隔（check.py は10分ごとに回るので、そのうち30分に1回だけ取る）

# 即時通知の条件
DROP_YEN = 1000   # 食事タイプ別の「実質」最安が、これまでの最安からこの額以上下がったら
DROP_PCT = 10     # 同じプランの「実質」が前回からこの%以上下がったら
SALE_RE = re.compile(r"セール|SALE|タイムセール|特価|割引|OFF|早割|応援割|ふっこう|復興|クーポン|限定価格")

SITE_NAME = {"official": "公式HP", "jalan": "じゃらん", "rakuten": "楽天トラベル",
             "yahoo": "Yahoo!トラベル", "ikyu": "一休.com"}

# Yahoo!トラベル・一休はクラウドの IP を丸ごと拒否する（ヘッドレス Chrome でも 403）。
# 持ち主の Mac（家庭回線）が launchd で1日3回取得して watch/local_prices.json にコミットするので、それを読む
LOCAL_PATH = os.path.join(os.path.dirname(__file__), "local_prices.json")
LOCAL_STALE_H = 26  # これより古ければ Mac 側が止まっているとみなして取得失敗に数える
MEALS = ["素泊まり", "朝食付", "夕食付", "2食付"]


# ---------------------------------------------------------------- 共通

def opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def get(op, url, data=None, headers=None, timeout=40):
    h = {"User-Agent": UA, "Accept-Language": "ja"}
    h.update(headers or {})
    for i in range(3):
        try:
            with op.open(urllib.request.Request(url, data=data, headers=h), timeout=timeout) as r:
                body = r.read()
                cs = r.headers.get_content_charset()
            break
        except (OSError, TimeoutError):
            if i == 2:
                raise
            time.sleep(3)
    if not cs:
        m = re.search(rb'charset=["\']?([\w-]+)', body[:4000], re.I)
        cs = m.group(1).decode() if m else "utf-8"
    # じゃらんは Shift_JIS 系を windows-31j と名乗るが、Python はこの名前を知らない
    cs = {"windows-31j": "cp932", "shift_jis": "cp932", "x-sjis": "cp932"}.get(cs.lower(), cs)
    try:
        return body.decode(cs, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def text(fragment):
    s = re.sub(r"<[^>]+>", " ", fragment or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def yen(s):
    return int(s.replace(",", "")) if s else None


def meal(s):
    s = s or ""
    if re.search(r"朝夕|朝・夕|2食|二食", s):
        return "2食付"
    b = bool(re.search(r"朝食あり|朝食付|朝のみ", s))
    d = bool(re.search(r"夕食あり|夕食付|夕のみ", s))
    return {(True, True): "2食付", (True, False): "朝食付", (False, True): "夕食付"}.get((b, d), "素泊まり")


def offer(site, plan, room, total, meal_type, coupon=0, points=0, regular=None, left=None, coupons=None):
    o = {"site": site, "plan": plan, "room": room, "meal": meal_type, "total": total,
         "coupon": coupon or 0, "points": points or 0, "eff": total - (coupon or 0) - (points or 0)}
    if regular and regular > total:
        o["regular"] = regular
    if left:
        o["left"] = left
    if coupons:
        o["coupons"] = coupons
    return o


# ---------------------------------------------------------------- 各サイト

def search_url(site, h=HOTEL):
    ci, co = h["checkin"], h["checkin"] + dt.timedelta(days=h["nights"])
    if site == "official":
        q = urllib.parse.urlencode({"date": ci.isoformat(), "numberOfNights": h["nights"], "roomCount": 1,
                                    "guests[8857][adult]": h["adults"], "searchTagMode": 1,
                                    "priceMin": 0, "priceMax": 99999999})
        return f"https://reserve.489ban.net/client/{h['official']}/0/plan/search?{q}"
    if site == "jalan":
        q = urllib.parse.urlencode({"stayYear": ci.year, "stayMonth": ci.month, "stayDay": ci.day,
                                    "stayCount": h["nights"], "roomCount": 1, "adultNum": h["adults"],
                                    "minPrice": 0, "maxPrice": 999999})
        return f"https://www.jalan.net/yad{h['jalan']}/plan/?{q}"
    if site == "rakuten":
        q = urllib.parse.urlencode({"f_nen1": ci.year, "f_tuki1": ci.month, "f_hi1": ci.day,
                                    "f_nen2": co.year, "f_tuki2": co.month, "f_hi2": co.day,
                                    "f_heya_su": 1, "f_otona_su": h["adults"], "f_s1": 0, "f_s2": 0,
                                    "f_y1": 0, "f_y2": 0, "f_y3": 0, "f_y4": 0, "f_flg": "PLAN"})
        return f"https://hotel.travel.rakuten.co.jp/hotelinfo/plan/{h['rakuten']}?{q}"
    if site == "yahoo":
        return f"https://travel.yahoo.co.jp/00002017/plan/?cid={ci:%Y%m%d}&lc={h['nights']}&ppc={h['adults']}&rc=1"
    if site == "ikyu":
        return f"https://www.ikyu.com/00002017/?cid={ci:%Y%m%d}&lc={h['nights']}&ppc={h['adults']}&rc=1"
    raise KeyError(site)


def fetch_official(h=HOTEL):
    """公式（489ban）。一覧は CSRF トークン付きの POST で別に取る。料金は「1名様料金」なので人数を掛ける。"""
    op = opener()
    url = search_url("official", h)
    page = get(op, url)
    token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    body = get(op, url.replace("/plan/search?", "/planlist/search?"), data=b"",
               headers={"X-CSRF-TOKEN": token, "X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})
    plans = json.loads(body)["planList"]
    out = []
    for sec in re.split(r'<section class="plan-list', plans)[1:]:
        name = text(re.search(r"<h2.*?</h2>", sec, re.S).group(0))
        m = re.search(r"<dt>お食事</dt>\s*<dd>(.*?)</dd>", sec, re.S)
        mt = meal(text(m.group(1)) if m else name)
        left = re.search(r'webc_plan_remain">\s*残り(\d+)', sec)
        prices = [yen(p) for p in re.findall(r"<strong>\s*<span>([\d,]+)円</span>", sec)]
        if not prices:
            continue
        out.append(offer("official", name, "", min(prices) * h["adults"], mt, left=left and int(left.group(1))))
    return out


def fetch_jalan(h=HOTEL):
    page = get(opener(), search_url("jalan", h))
    out = []
    for cas in re.split(r'<li class="p-planCassette', page)[1:]:
        plan = text(re.search(r'p-searchResultItem__catchPhrase">(.*?)</p>', cas, re.S).group(1))
        m = re.search(r'p-mealType__value">(.*?)<', cas)
        mt = meal(m.group(1) if m else plan)
        for row in re.split(r'<tr class="js-searchYadoRoomPlanCd', cas)[1:]:
            room = re.search(r'jsc-planDetailLink" href="[^"]*">(.*?)</a>', row, re.S)
            sale = re.search(r'total--SpecialWeek">\s*<em>([\d,]+)</em>', row)
            norm = re.search(r'p-searchResultItem__total">\s*<em>([\d,]+)</em>', row)
            total = yen((sale or norm).group(1)) if (sale or norm) else None
            if total is None:
                continue  # 満室
            reg = re.search(r'total--line-through">\s*([\d,]+)円', row)
            cp = re.search(r'popup__headingPrice">([\d,]+)<', row)
            pt = re.search(r'p-searchResultItem__point">\s*<em>([\d,]+)</em>', row)
            left = re.search(r'restNumber">(\d+)<', row)
            names = [text(n) for n in re.findall(r'popupCouponBlock__heading">(.*?)</h3>', row, re.S)]
            out.append(offer("jalan", plan, text(room.group(1)) if room else "", total, mt,
                             coupon=yen(cp.group(1)) if cp else 0, points=yen(pt.group(1)) if pt else 0,
                             regular=yen(reg.group(1)) if reg else None, left=left and int(left.group(1)),
                             coupons=names))
    return out


def fetch_rakuten(h=HOTEL, max_pages=6):
    op, out = opener(), []
    for page_no in range(1, max_pages + 1):
        page = get(op, search_url("rakuten", h) + f"&f_page_no={page_no}")
        blocks = re.split(r'<li id="\d+" class="planThumb"', page)[1:]
        for b in blocks:
            plan = text(re.search(r"<h4>(.*?)</h4>", b, re.S).group(1)).replace("NEW ", "", 1)
            for room in re.split(r'data-locate="htlPlnRmTypInfo"', b)[1:]:
                price = re.search(r'ndPrice">合計<strong>([\d,]+)</strong>', room)
                if not price:
                    continue  # 空室なし
                rn = re.search(r'data-locate="roomType-name">(.*?)</h6>', room, re.S)
                mm = re.search(r'roomType-option-meal">(.*?)</span>', room, re.S)
                pt = re.search(r'point-amount"><strong>([\d,]+)</strong>', room)
                left = re.search(r"残り(\d+)部屋", room)
                out.append(offer("rakuten", plan, text(rn.group(1)) if rn else "", yen(price.group(1)),
                                 meal(text(mm.group(1)) if mm else plan),
                                 points=yen(pt.group(1)) if pt else 0, left=left and int(left.group(1))))
        if len(blocks) < 10 or f"f_page_no={page_no + 1}" not in page:
            break
    return out


FETCHERS = {"official": fetch_official, "jalan": fetch_jalan, "rakuten": fetch_rakuten}


def load_local(now, path=None):
    """Mac が書いた local_prices.json を {site: offers のリスト または 例外} で返す。ファイルが無ければ空。"""
    try:
        with open(path or LOCAL_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    age = now - dt.datetime.fromisoformat(data["fetched_at"])
    out = {}
    for site, rows in (data.get("sites") or {}).items():
        err = (data.get("errors") or {}).get(site)
        if site not in SITE_NAME:
            continue
        if err:
            out[site] = RuntimeError(f"Mac 側の取得で失敗: {err}")
        elif age > dt.timedelta(hours=LOCAL_STALE_H):
            out[site] = RuntimeError(f"Mac 側の取得が {age.total_seconds() / 3600:.0f} 時間止まっている")
        else:
            out[site] = [offer(site, r["plan"], r.get("room", ""), int(r["total"]), r.get("meal") or meal(r["plan"]),
                               coupon=r.get("coupon"), points=r.get("points"), regular=r.get("regular"),
                               left=r.get("left")) for r in rows]
    return out


def wanted(offers, h=HOTEL):
    pat = h.get("plan_filter")
    return [o for o in offers if not pat or re.search(pat, f"{o['plan']} {o['room']}")]


# ---------------------------------------------------------------- 比べる

def plan_mins(offers):
    """プランごとに「実質」が一番安い部屋だけ残す。キーは "サイト|プラン名"。"""
    best = {}
    for o in offers:
        k = f"{o['site']}|{o['plan']}"
        if k not in best or o["eff"] < best[k]["eff"]:
            best[k] = o
    return best


def meal_mins(plans):
    best = {}
    for o in plans.values():
        if o["meal"] not in best or o["eff"] < best[o["meal"]]["eff"]:
            best[o["meal"]] = o
    return best


def fmt_yen(n):
    return f"{n:,}円"


def describe(o, link=True):
    """1プランを1行で。合計（通常料金）→ クーポン・ポイント → 実質。"""
    s = fmt_yen(o["total"])
    if o.get("regular"):
        s += f"（通常{fmt_yen(o['regular'])}から{round(100 - o['total'] * 100 / o['regular'])}%OFF）"
    if o["coupon"]:
        s += f" → クーポン{fmt_yen(o['coupon'])}引きで支払い{fmt_yen(o['total'] - o['coupon'])}"
    if o["points"]:
        s += f"（＋ポイント{o['points']:,}還元で実質{fmt_yen(o['eff'])}）"
    name = SITE_NAME[o["site"]]
    if link:
        name = f"<{search_url(o['site'])}|{name}>"
    left = f"・残り{o['left']}" if o.get("left") else ""
    plan = o["plan"][:60] + (f"／{o['room'][:30]}" if o.get("room") else "")
    return f"{name} {s}［{o['meal']}{left}］{plan}"


def deals(prev, plans):
    """即時に知らせる「良い割引」。prev は前回の state（初回は None）。"""
    if not prev:
        return []
    # 前回までに1度も取れていなかったサイトは、今回が基準づくり（取得失敗からの復旧で誤検知しない）
    known = {k.split("|", 1)[0] for k in prev.get("plans", {})}
    plans = {k: o for k, o in plans.items() if o["site"] in known}
    out, told = [], set()
    lows = prev.get("lowest", {})
    for mt, o in meal_mins(plans).items():
        low = lows.get(mt)
        if low is not None and o["eff"] <= low - DROP_YEN:
            told.add(f"{o['site']}|{o['plan']}")
            out.append(f"📉 {mt}の最安を更新（これまで{fmt_yen(low)}）: {describe(o)}")
    old, seen = prev.get("plans", {}), set(prev.get("seen", []))
    for k, o in plans.items():
        p = old.get(k)
        if p is None:
            # 満室で一度消えて戻ってきたプランは「新しい」としない
            if k not in seen and (SALE_RE.search(o["plan"]) or o.get("regular")):
                out.append(f"🆕 割引のプランが出た: {describe(o)}")
            continue
        if k not in told and o["eff"] <= p * (100 - DROP_PCT) / 100:
            out.append(f"⬇️ 前回{fmt_yen(p)}から{round(100 - o['eff'] * 100 / p)}%値下がり: {describe(o)}")
    seen = set(prev.get("coupons", []))
    new_cp = sorted({c for o in plans.values() for c in o.get("coupons", [])} - seen)
    if new_cp:
        out.append("🎟️ 新しいクーポン: " + " / ".join(new_cp[:5]))
    return out


def summary(state, stamp):
    h = HOTEL
    ci = h["checkin"]
    party = h.get("party") or f"大人{h['adults']}名"
    head = (f"*🏝️ 【{h['name']}】{ci.month}/{ci.day}〜{h['nights']}泊・{party}の料金まとめ（{stamp}）*\n"
            "ペット同伴プランのみ（犬の料金は別途・現地払い）。料金は1室あたり税込。"
            "実質＝支払い額からさらに還元ポイントを引いた目安（クーポンは先着で事前の獲得が要る）")
    lines = []
    for mt in MEALS:
        rows = sorted((o for o in state["best"].values() if o["meal"] == mt), key=lambda o: o["eff"])
        if rows:
            lines.append(f"*{mt}*\n" + "\n".join(f"・{describe(o)}" for o in rows))
    # 監視を始めてからの最安は、いまの最安より安かったときだけ載せる（同じなら上の一覧で分かる）
    lows, at = state.get("lowest", {}), state.get("lowest_at", {})
    now_min = meal_mins({k: v for k, v in state["best"].items()})
    past = [f"{mt} 実質{fmt_yen(lows[mt])}（{at.get(mt, '')}）" for mt in MEALS
            if mt in lows and mt in now_min and lows[mt] < now_min[mt]["eff"]]
    if past:
        lines.append("監視を始めてからの最安（いまはこれより高い）: " + " / ".join(past))
    cps = state.get("coupons_now") or []
    if cps:
        lines.append("いま出ているクーポン: " + " / ".join(cps[:6]))
    none = [SITE_NAME[s] for s, n in state.get("counts", {}).items() if n == 0 and not state["errors"].get(s)]
    if none:
        lines.append("対象プランの空室なし: " + ", ".join(none))
    errs = [SITE_NAME[s] for s, n in state.get("errors", {}).items() if n]
    if errs:
        lines.append("取得できなかったサイト: " + ", ".join(errs))
    return head + "\n\n" + "\n\n".join(lines)


def run(saved, now, force=False):
    """saved["hotel"] を更新し、(即時通知の文面リスト, 取得できたか) を返す。"""
    st = saved.setdefault("hotel", {})
    if now.date() >= HOTEL["checkin"]:
        return [], False
    last = st.get("checked_at")
    if not force and last and now - dt.datetime.fromisoformat(last) < dt.timedelta(minutes=INTERVAL_MIN - 1):
        return [], False

    offers, errors = [], st.setdefault("errors", {})
    for site, fn in FETCHERS.items():
        try:
            got = fn()
            if not got:
                raise ValueError("プランが1件も取れない（ページの作りが変わったか満室）")
            mine = wanted(got)
            offers += mine
            st.setdefault("counts", {})[site] = len(mine)
            errors[site] = 0
        except Exception as e:  # noqa: BLE001 — 1サイトの失敗で全体を止めない
            errors[site] = errors.get(site, 0) + 1
            st.setdefault("last_error", {})[site] = f"{type(e).__name__}: {e}"[:200]
    try:
        local = load_local(now)
    except Exception as e:  # noqa: BLE001 — 壊れたファイルで全体を止めない
        local = {s: e for s in ("yahoo", "ikyu")}
    for site, got in local.items():
        if isinstance(got, Exception):
            errors[site] = errors.get(site, 0) + 1
            st.setdefault("last_error", {})[site] = f"{type(got).__name__}: {got}"[:200]
            continue
        mine = wanted(got)  # 空でもよい（犬連れプランの空室なし）
        offers += mine
        st.setdefault("counts", {})[site] = len(mine)
        errors[site] = 0
    plans = plan_mins(offers)
    ok_sites = {s for s, n in errors.items() if n == 0}
    prev = st.get("plans") and st
    alerts = deals(prev, plans)

    # 取れなかったサイトの前回値は残す（一時的な失敗で「新しいプラン」と誤検知しないため）
    kept = {k: v for k, v in st.get("plans", {}).items() if k.split("|", 1)[0] not in ok_sites}
    st["plans"] = {**kept, **{k: o["eff"] for k, o in plans.items()}}
    st["seen"] = sorted(set(st.get("seen", [])) | set(plans))
    best = {k: v for k, v in st.get("best", {}).items() if v["site"] not in ok_sites}
    for site in ok_sites:
        for mt, o in meal_mins({k: v for k, v in plans.items() if v["site"] == site}).items():
            best[f"{site}|{mt}"] = o
    st["best"] = best
    lows = st.setdefault("lowest", {})
    lows_at = st.setdefault("lowest_at", {})
    for mt, o in meal_mins(plans).items():
        if mt not in lows or o["eff"] < lows[mt]:
            lows[mt] = o["eff"]
            lows_at[mt] = f"{now.month}/{now.day} {SITE_NAME[o['site']]}"
    now_cp = sorted({c for o in plans.values() for c in o.get("coupons", [])})
    st["coupons_now"] = now_cp
    st["coupons"] = sorted(set(st.get("coupons", [])) | set(now_cp))
    st["checked_at"] = now.isoformat(timespec="seconds")

    if errors and any(n == 3 for n in errors.values()):
        alerts += [f"⚠️ {SITE_NAME[s]}の料金が3回続けて取れていません（{st['last_error'].get(s)}）"
                   for s, n in errors.items() if n == 3]
    return alerts, bool(offers)


if __name__ == "__main__":
    JST = dt.timezone(dt.timedelta(hours=9))
    now = dt.datetime.now(JST)
    for site, fn in FETCHERS.items():
        got = wanted(fn())
        print(f"== {site}: {len(got)}件")
        for o in sorted(plan_mins(got).values(), key=lambda o: o["eff"])[:5]:
            print("  ", describe(o, link=False))
    saved = {}
    alerts, _ = run(saved, now, force=True)
    print(summary(saved["hotel"], now.strftime("%m/%d %H:%M")))
