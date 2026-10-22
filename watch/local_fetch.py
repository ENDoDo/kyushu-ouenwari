"""Yahoo!トラベルと一休.com の料金を、手元の Mac（家庭回線）から取る。

両サイトとも GitHub Actions やクラウドの IP からは 403 で読めないので、Mac の launchd で
定期実行し、結果を watch/local_prices.json としてコミットする。取り込みと通知は Actions 側。

両サイトは同じ基盤（Nuxt + GraphQL）で、宿ページが呼んでいる /graphql の PlansAndRooms と
同じ検索条件でプラン一覧（全件）を引く。プラン×部屋ごとに1件の OFFER にする。

  /usr/bin/python3 watch/local_fetch.py          # 取得して local_prices.json を書く
  /usr/bin/python3 watch/local_fetch.py --push   # 書いたうえで local_prices.json だけを commit・push

launchd（~/Library/LaunchAgents/com.endodo.kyushu-localprice.plist）から --push 付きで呼ぶ。
標準ライブラリだけで動かす（launchd からは /usr/bin/python3 = 3.9 で起動する）。
"""

import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

HOTEL = {
    "id": "00002017",  # Yahoo!トラベル・一休で共通の施設ID
    "checkin": "2026-12-06",
    "nights": 1,
    "adults": 2,
    "rooms": 1,
    # hotel.py の plan_filter と同じ（犬と泊まれるのは BARK LODGE だけ）
    "plan_filter": r"ワンちゃん|愛犬|ペット|BARK",
}
SITES = {"yahoo": "travel.yahoo.co.jp", "ikyu": "www.ikyu.com"}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "watch", "local_prices.json")
JST = dt.timezone(dt.timedelta(hours=9))

QUERY = """query LocalPrices($id: AccommodationIdScalar!, $plans: SearchPlansInput!, $amounts: PlanAmountInput!) {
  accommodation(accommodationId: $id) {
    searchPlans2(input: $plans, first: 999, offset: 0) {
      plans {
        totalCount
        edges { node {
          planId name meal { code name }
          amounts(input: $amounts, first: 50) { edges { node {
            amount discountAmount point instantPoint inventory
            coupon { price }
            room { roomId name }
          } } }
        } }
      }
    }
  }
}"""

# 一休系の食事コード → hotel.py の食事タイプ
MEALS = {"000": "素泊まり", "001": "朝食付", "002": "夕食付", "003": "2食付"}


def fetch(site):
    host = SITES[site]
    cond = {"checkInDate": HOTEL["checkin"], "discount": True, "lodgingCount": HOTEL["nights"],
            "peopleCount": HOTEL["adults"], "roomCount": HOTEL["rooms"], "searchType": "1",
            "sortItem": "1", "sortOrder": "1", "currency": "JPY", "sortAmountTarget": "USE"}
    body = json.dumps({"operationName": "LocalPrices", "query": QUERY,
                       "variables": {"id": HOTEL["id"], "plans": dict(cond, preferBookable=True),
                                     "amounts": cond}}).encode()
    req = urllib.request.Request(
        f"https://{host}/graphql?lang=ja-JP", data=body,
        headers={"Content-Type": "application/json", "User-Agent": UA, "Accept-Language": "ja",
                 "Origin": f"https://{host}", "Referer": f"https://{host}/{HOTEL['id']}/"})
    with urllib.request.urlopen(req, timeout=40) as r:
        res = json.load(r)
    if res.get("errors"):
        raise RuntimeError("GraphQL: " + "; ".join(e.get("message", "?") for e in res["errors"]))
    plans = res["data"]["accommodation"]["searchPlans2"]["plans"]

    offers = []
    for e in plans["edges"]:
        p = e["node"]
        for a in (x["node"] for x in p["amounts"]["edges"]):
            if not a["inventory"]:  # 満室
                continue
            if not re.search(HOTEL["plan_filter"], p["name"] + " " + a["room"]["name"]):
                continue
            # discountAmount は「ポイント即時利用」後の支払額（画面の「2名税込◯円」）。
            # 即時利用で使い切る分は points に入れない（hotel.py が total から二重に引くため）
            offers.append({
                "plan": p["name"],
                "room": a["room"]["name"].strip(),
                "meal": MEALS.get(p["meal"]["code"], p["meal"]["name"]),
                "total": a["discountAmount"],
                "coupon": (a.get("coupon") or {}).get("price") or 0,
                "points": max(0, (a["point"] or 0) - (a["instantPoint"] or 0)),
                "regular": a["amount"],
                "left": a["inventory"],
            })
    offers.sort(key=lambda o: (o["total"], o["plan"], o["room"]))
    return offers


def collect():
    out = {"fetched_at": dt.datetime.now(JST).isoformat(timespec="seconds"), "sites": {}, "errors": {}}
    for site in SITES:
        try:
            out["sites"][site] = fetch(site)
            out["errors"][site] = None
        except Exception as e:  # 片方が落ちてももう片方は書く
            out["sites"][site] = []
            out["errors"][site] = f"{type(e).__name__}: {e}"[:300]
        time.sleep(2)
    return out


def git(*args):
    return subprocess.run(["git", "-C", ROOT, *args], check=True, capture_output=True, text=True).stdout


def push():
    rel = os.path.relpath(OUT, ROOT)
    git("add", rel)
    if not git("diff", "--cached", "--name-only"):
        print("変更なし")
        return
    git("commit", "-m", "local: 料金を更新", "--", rel)
    # Actions が watch/state.json を頻繁にコミットするので、取り込んでから push し直す
    for i in range(4):
        try:
            git("pull", "--rebase", "--autostash", "origin", "main")
            git("push", "origin", "HEAD:main")
            print("push 済み")
            return
        except subprocess.CalledProcessError as e:
            print(f"push 失敗（{i + 1}回目）: {e.stderr.strip()}", file=sys.stderr)
            time.sleep(2 ** (i + 1))
    raise SystemExit(1)


def main():
    if "--push" in sys.argv:
        git("pull", "--rebase", "--autostash", "origin", "main")
    data = collect()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")
    for site, offers in data["sites"].items():
        print(f"[{site}] {len(offers)}件" + (f" エラー: {data['errors'][site]}" if data["errors"][site] else ""))
        for o in offers:
            print(f"  {o['meal']} {o['total']:,}円（通常{o['regular']:,}円・残{o['left']}） {o['plan']} / {o['room']}")
    if "--push" in sys.argv:
        push()


if __name__ == "__main__":
    main()
