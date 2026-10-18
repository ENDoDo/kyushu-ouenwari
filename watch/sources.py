"""監視対象の一覧。ここに足せば check.py が巡回する。

kind:
  text     ページ本文から「販売中」「停止」「完売」などを含む行だけを取り出して比べる
  rakuten  楽天の県別特集ページのクーポン枠の状態（couponOneClick-status-*）を数える
  staycsv  県公式の「参画宿泊施設一覧」の CSV から、施設ごとの完売フラグを比べる

air（航空券の要否。2026-10-17 時点で各社の対象商品の説明から判断）:
  none   宿だけで予約できる（車で行ける）
  must   航空券＋宿泊のセットだけが対象
  mixed  県や商品によって違う（note に書く）
  省略    県公式・観光庁など、予約先そのものではないページ
"""

PREFS = ["fukuoka", "saga", "nagasaki", "kumamoto", "oita", "miyazaki", "kagoshima"]
PREF_JA = {
    "fukuoka": "福岡", "saga": "佐賀", "nagasaki": "長崎", "kumamoto": "熊本",
    "oita": "大分", "miyazaki": "宮崎", "kagoshima": "鹿児島",
}

SOURCES = [
    # --- 予約サイト（OTA） ---
    {"id": "jalan", "name": "じゃらんnet", "air": "none", "kind": "text",
     "url": "https://www.jalan.net/kyushu-shien/"},
    {"id": "ana", "name": "ANA X", "air": "must", "kind": "text",
     "url": "https://www.ana.co.jp/ja/jp/domestic/theme/kyushu-ouen/"},
    *[{"id": f"rakuten-{p}", "name": f"楽天トラベル（{PREF_JA[p]}）", "air": "none", "kind": "rakuten",
       "url": f"https://travel.rakuten.co.jp/special/kyushuouen/{p}/"} for p in PREFS if p != "kagoshima"],
    # 鹿児島だけクーポン枠の作りが違うので、本文の状況行で見る
    {"id": "rakuten-kagoshima", "name": "楽天トラベル（鹿児島）", "air": "mixed", "note": "鹿児島はOTA経由だと交通付きプランのみの社がある", "kind": "text",
     "url": "https://travel.rakuten.co.jp/special/kyushuouen/kagoshima/"},
    {"id": "knt", "name": "近畿日本ツーリスト", "air": "none", "kind": "text",
     "url": "https://www.knt.co.jp/kyushu-ouenwari/"},
    {"id": "rurubu", "name": "るるぶトラベル", "air": "none", "kind": "text",
     "url": "https://www.rurubu.travel/theme/kyushu_fukkou_campaign/"},
    {"id": "jtb", "name": "JTB", "air": "none", "kind": "text",
     "url": "https://www.jtb.co.jp/kokunai/kyushu-ouen/"},
    {"id": "airtrip", "name": "エアトリ", "air": "mixed", "note": "航空券付きツアーが中心。県によりホテルのみも対象", "kind": "text",
     "url": "https://domdp.airtrip.jp/campaign/fukkou-wari/"},
    {"id": "needstour", "name": "ニーズツアー", "air": "must", "note": "交通付きツアー", "kind": "text",
     "url": "https://www.needstour.com/kyushu/fukko-oenwari/"},
    {"id": "jcation", "name": "Jcation", "air": "mixed", "note": "宿泊単体も対象。ただし鹿児島は航空券付きのみ", "kind": "text",
     "url": "https://jcation.com/blog/kyushuouen/"},

    # --- 県公式 ---
    {"id": "pref-fukuoka", "name": "福岡県公式", "kind": "text", "url": "https://fukuoka-fukkou-wari.jp/"},
    {"id": "pref-saga", "name": "佐賀県公式", "kind": "text", "url": "https://saga-ouen.jp/"},
    {"id": "pref-nagasaki", "name": "長崎県公式", "kind": "text", "url": "https://nagasaki-fukkou-wari.jp/"},
    # 熊本県公式は中身を JavaScript で読み込むため、取得しても状況が読めない（対象外）
    {"id": "pref-oita", "name": "大分県公式", "kind": "text", "url": "https://oita-fukkou-wari.jp/"},
    {"id": "pref-miyazaki", "name": "宮崎県公式", "kind": "text", "url": "https://miyazaki-ouen.jp/"},
    {"id": "pref-kagoshima", "name": "鹿児島県公式", "kind": "text", "url": "https://fukko-ouenwari.pref.kagoshima.jp/"},
    {"id": "mlit", "name": "観光庁 集約ページ", "kind": "text",
     "url": "https://www.mlit.go.jp/kankocho/page13_00002.html"},

    # --- 宿の直接予約（施設ごとの完売フラグ） ---
    {"id": "stay-oita", "name": "大分 宿直接", "air": "none", "kind": "staycsv",
     "url": "https://oita-fukkou-wari.jp/stay/", "prefix": "oita"},
    {"id": "stay-kagoshima", "name": "鹿児島 宿直接", "air": "none", "kind": "staycsv",
     "url": "https://fukko-ouenwari.pref.kagoshima.jp/stay/", "prefix": "kagoshima"},
    {"id": "stay-fukuoka", "name": "福岡 宿直接", "air": "none", "kind": "staycsv",
     "url": "https://fukuoka-fukkou-wari.jp/stay/", "prefix": "fukuoka"},
]

# 朝の現状まとめに載せる「日時が決まっている次の販売」。過ぎたものは自動で出なくなる。
# air は SOURCES と同じ意味（2026-10-17 に各社・各県のページで確認）
UPCOMING = [
    {"at": "2026-10-19 13:00", "name": "鹿児島 Jcation", "air": "must"},
    {"at": "2026-10-19 00:00", "name": "佐賀 JTB", "air": "mixed"},
    {"at": "2026-10-23 10:00", "name": "長崎 宿の直接予約（第2期）", "air": "none"},
    {"at": "2026-10-26 00:00", "name": "鹿児島 オリオンツアー", "air": "mixed"},
    {"at": "2026-11-10 00:00", "name": "佐賀市観光協会", "air": "mixed"},
]

# 朝の現状まとめで個別に状態を書く宿（施設名の一部で照合）
FOCUS_STAYS = ["COMOREBI"]
