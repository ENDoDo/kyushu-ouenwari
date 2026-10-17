"""監視対象の一覧。ここに足せば check.py が巡回する。

kind:
  text     ページ本文から「販売中」「停止」「完売」などを含む行だけを取り出して比べる
  rakuten  楽天の県別特集ページのクーポン枠の状態（couponOneClick-status-*）を数える
  staycsv  県公式の「参画宿泊施設一覧」の CSV から、施設ごとの完売フラグを比べる
"""

PREFS = ["fukuoka", "saga", "nagasaki", "kumamoto", "oita", "miyazaki", "kagoshima"]
PREF_JA = {
    "fukuoka": "福岡", "saga": "佐賀", "nagasaki": "長崎", "kumamoto": "熊本",
    "oita": "大分", "miyazaki": "宮崎", "kagoshima": "鹿児島",
}

SOURCES = [
    # --- 予約サイト（OTA） ---
    {"id": "jalan", "name": "じゃらんnet", "kind": "text",
     "url": "https://www.jalan.net/kyushu-shien/"},
    {"id": "ana", "name": "ANA X", "kind": "text",
     "url": "https://www.ana.co.jp/ja/jp/domestic/theme/kyushu-ouen/"},
    *[{"id": f"ana-{p}", "name": f"ANA X（{PREF_JA[p]}）", "kind": "text",
       "url": f"https://www.ana.co.jp/ja/jp/domestic/theme/kyushu-ouen/{p}/"} for p in PREFS],
    *[{"id": f"rakuten-{p}", "name": f"楽天トラベル（{PREF_JA[p]}）", "kind": "rakuten",
       "url": f"https://travel.rakuten.co.jp/special/kyushuouen/{p}/"} for p in PREFS],
    {"id": "knt", "name": "近畿日本ツーリスト", "kind": "text",
     "url": "https://www.knt.co.jp/kyushu-ouenwari/"},
    {"id": "rurubu", "name": "るるぶトラベル", "kind": "text",
     "url": "https://www.rurubu.travel/theme/kyushu_fukkou_campaign/"},
    {"id": "jtb", "name": "JTB", "kind": "text",
     "url": "https://www.jtb.co.jp/kokunai/kyushu-ouen/"},
    {"id": "airtrip", "name": "エアトリ", "kind": "text",
     "url": "https://domdp.airtrip.jp/campaign/fukkou-wari/"},
    {"id": "needstour", "name": "ニーズツアー", "kind": "text",
     "url": "https://www.needstour.com/kyushu/fukko-oenwari/"},
    {"id": "jcation", "name": "Jcation", "kind": "text",
     "url": "https://jcation.com/blog/kyushuouen/"},

    # --- 県公式 ---
    {"id": "pref-fukuoka", "name": "福岡県公式", "kind": "text", "url": "https://fukuoka-fukkou-wari.jp/"},
    {"id": "pref-saga", "name": "佐賀県公式", "kind": "text", "url": "https://saga-ouen.jp/"},
    {"id": "pref-nagasaki", "name": "長崎県公式", "kind": "text", "url": "https://nagasaki-fukkou-wari.jp/"},
    {"id": "pref-kumamoto", "name": "熊本県公式", "kind": "text", "url": "https://kumamoto-ouenwari.com/index.html"},
    {"id": "pref-oita", "name": "大分県公式", "kind": "text", "url": "https://oita-fukkou-wari.jp/"},
    {"id": "pref-miyazaki", "name": "宮崎県公式", "kind": "text", "url": "https://miyazaki-ouen.jp/"},
    {"id": "pref-kagoshima", "name": "鹿児島県公式", "kind": "text", "url": "https://fukko-ouenwari.pref.kagoshima.jp/"},
    {"id": "mlit", "name": "観光庁 集約ページ", "kind": "text",
     "url": "https://www.mlit.go.jp/kankocho/page13_00002.html"},

    # --- 宿の直接予約（施設ごとの完売フラグ） ---
    {"id": "stay-oita", "name": "大分 宿直接", "kind": "staycsv",
     "url": "https://oita-fukkou-wari.jp/stay/", "prefix": "oita"},
    {"id": "stay-kagoshima", "name": "鹿児島 宿直接", "kind": "staycsv",
     "url": "https://fukko-ouenwari.pref.kagoshima.jp/stay/", "prefix": "kagoshima"},
    {"id": "stay-fukuoka", "name": "福岡 宿直接", "kind": "staycsv",
     "url": "https://fukuoka-fukkou-wari.jp/stay/", "prefix": "fukuoka"},
]
