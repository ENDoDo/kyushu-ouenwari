"""グランディスタイル沖縄読谷の「2027年2月の平日・2泊・大人2名」の料金を毎朝1回見て、いちばん安い日を知らせる。

hotel.py（アイランド長崎）の取得部品をそのまま使い、日付だけを2月の平日ぶん回す。
取るのは公式HP（489ban）・じゃらん・楽天トラベル。Yahoo!トラベル・一休はクラウドから読めないので見ていない。

- 毎朝8時台に1回、候補日ぜんぶの料金を取り、日付ごとの最安（朝食付）を一覧にして投稿する
- 前回より DROP_YEN 以上下がった日・監視開始からの最安を更新した日があればメンション付きにする
- 候補日がすべて過ぎたら止まる

  python watch/glamday.py   # 取得してまとめの文面を標準出力に出す（state は書かない）
"""

import datetime as dt
import time

import hotel as H

TARGET = {
    "name": "グランディスタイル沖縄読谷",
    "nights": 2,
    "adults": 2,
    "official": "glamdaystyle",
    "guest_key": 14984,   # 489ban の「大人（男性）」の入力欄の番号（宿ごとに違う）
    "jalan": "368381",
    "rakuten": "172439",
}
MONTH = (2027, 2)
# 泊まる夜が祝日か祝前日なら平日扱いしない（2/11 建国記念の日・2/23 天皇誕生日）
HOLIDAYS = {dt.date(2027, 2, 11), dt.date(2027, 2, 23)}
MEAL = "朝食付"     # 主に比べる食事タイプ（ブランチ 7:00〜14:00 付き）
RUN_HOUR = 8        # JST。この時刻以降の最初の巡回で1日1回だけ取る
DROP_YEN = 2000     # 同じ日の「実質」最安が前回からこの額以上下がったらメンション
WEEK = "月火水木金土日"


def checkins(today):
    """チェックイン候補日。泊まる夜がすべて月〜木で、祝日・祝前日を含まない日（今日より後）。"""
    first = dt.date(*MONTH, 1)
    out = []
    d = first
    while d.month == MONTH[1]:
        nights = [d + dt.timedelta(days=i) for i in range(TARGET["nights"])]
        ok = all(n.weekday() <= 3 and n not in HOLIDAYS and n + dt.timedelta(days=1) not in HOLIDAYS
                 for n in nights)
        if ok and d > today:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def stay(ci):
    return dict(TARGET, checkin=ci)


def label(ci):
    return f"{ci.month}/{ci.day}({WEEK[ci.weekday()]})"


def scan(dates, pause=1.0):
    """{日付ISO: {"best": {食事: offer}, "errors": [サイト]}} を返す。"""
    out = {}
    for ci in dates:
        h = stay(ci)
        offers, errors = [], []
        for site, fn in H.FETCHERS.items():
            try:
                got = fn(h)
                if not got:
                    raise ValueError("プランが1件も取れない")
                offers += got
            except Exception:  # noqa: BLE001 — 1サイトの失敗で全体を止めない
                errors.append(site)
            time.sleep(pause)
        best = H.meal_mins(H.plan_mins(offers))
        out[ci.isoformat()] = {"best": best, "errors": errors}
    return out


def line(ci, o, prev_eff):
    h = stay(ci)
    site = f"<{H.search_url(o['site'], h)}|{H.SITE_NAME[o['site']]}>"
    s = f"{label(ci)}〜 実質{H.fmt_yen(o['eff'])}"
    if o["eff"] != o["total"]:
        s += f"（支払い{H.fmt_yen(o['total'] - o['coupon'])}）"
    s += f" {site}"
    if prev_eff is not None and prev_eff != o["eff"]:
        d = o["eff"] - prev_eff
        s += f"　前回比 {'+' if d > 0 else '−'}{abs(d):,}円"
    return s


def run(saved, now, force=False):
    """1日1回だけ取ってまとめの文面を作る。(文面, メンションするか) を返す。取らない日は None。"""
    st = saved.setdefault("glamday", {})
    today = now.date()
    dates = checkins(today)
    if not dates:
        return None
    if not force and (now.hour < RUN_HOUR or st.get("checked_on") == today.isoformat()):
        return None

    res = scan(dates)
    prev = st.get("eff", {})
    lowest, lowest_at = st.get("lowest"), st.get("lowest_at", "")
    rows, loud_lines, effs = [], [], {}
    for ci in dates:
        k = ci.isoformat()
        o = res[k]["best"].get(MEAL)
        if not o:
            rows.append((None, f"・{label(ci)}〜 {MEAL}の空室なし" +
                         (f"（取れなかった: {', '.join(H.SITE_NAME[s] for s in res[k]['errors'])}）"
                          if res[k]["errors"] else "")))
            continue
        effs[k] = o["eff"]
        p = prev.get(k)
        rows.append((o["eff"], "・" + line(ci, o, p)))
        if p is not None and o["eff"] <= p - DROP_YEN:
            loud_lines.append(f"📉 {label(ci)}〜 が前回から{p - o['eff']:,}円下がった: 実質{H.fmt_yen(o['eff'])}")

    stamp = now.strftime("%m/%d %H:%M")
    head = (f"*🌺 【{TARGET['name']}】{MONTH[0]}年{MONTH[1]}月の平日・{TARGET['nights']}泊・大人{TARGET['adults']}名"
            f"・{MEAL}の最安（{stamp}）*\n"
            "1室・全泊の税込合計。実質＝支払いからポイント還元を引いた目安。"
            "公式HP・じゃらん・楽天トラベルを比較（Yahoo!トラベル・一休は未対象）")
    if not effs:
        return head + "\n\n料金が1件も取れませんでした。", True

    best_k = min(effs, key=effs.get)
    best_ci = dt.date.fromisoformat(best_k)
    top = f"★ いちばん安い日: {label(best_ci)}〜 実質{H.fmt_yen(effs[best_k])}"
    if lowest is None or effs[best_k] < lowest:
        if lowest is not None:
            loud_lines.append(f"🏆 監視開始からの最安を更新（これまで{H.fmt_yen(lowest)}・{lowest_at}）")
        lowest, lowest_at = effs[best_k], f"{now.month}/{now.day}時点の{label(best_ci)}〜"
    elif lowest < effs[best_k]:
        top += f"\n（監視開始からの最安は {H.fmt_yen(lowest)}・{lowest_at}）"

    body = [top, "\n".join(r for _, r in rows)]
    other = []
    for ci in dates:
        for mt in ("素泊まり",):
            o = res[ci.isoformat()]["best"].get(mt)
            if o and (mt not in [x[0] for x in other] or o["eff"] < other[[x[0] for x in other].index(mt)][1]):
                other = [x for x in other if x[0] != mt] + [(mt, o["eff"], ci)]
    if other:
        body.append("参考: " + " / ".join(f"{mt}の最安 {label(ci)}〜 実質{H.fmt_yen(e)}" for mt, e, ci in other))
    errs = sorted({s for r in res.values() for s in r["errors"]})
    if errs:
        body.append("一部の日で取得できなかったサイト: " + ", ".join(H.SITE_NAME[s] for s in errs))

    st["eff"] = effs
    st["lowest"], st["lowest_at"] = lowest, lowest_at
    st["checked_on"] = today.isoformat()
    text = head + "\n\n" + ("\n".join(loud_lines) + "\n\n" if loud_lines else "") + "\n\n".join(body)
    return text, bool(loud_lines)


if __name__ == "__main__":
    JST = dt.timezone(dt.timedelta(hours=9))
    now = dt.datetime.now(JST)
    print("候補日:", ", ".join(label(d) for d in checkins(now.date())))
    out = run({}, now, force=True)
    print(out[0] if out else "対象日なし")
