"""九州ふっこう応援割の販売状況・キャンセル枠を横断で見張る。

各ソースを取得して「状態」を取り出し、watch/state.json の前回値と比べる。
変化があれば Slack（Incoming Webhook）に投稿する。標準ライブラリだけで動く。

  python watch/check.py            # 本番（SLACK_WEBHOOK_URL があれば投稿）
  python watch/check.py --dry-run  # 投稿せず、通知文を標準出力に出す
  python watch/check.py --ping     # 変化がなくても1通投稿する（通知の疎通確認）
"""

import csv
import datetime as dt
import html
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(__file__))
import pet  # noqa: E402
from sources import FOCUS_STAYS, SOURCES, UPCOMING  # noqa: E402

STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")
JST = dt.timezone(dt.timedelta(hours=9))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130 Safari/537.36")
HEARTBEAT_HOUR = 8  # JST。この時刻以降の最初の実行で1日1回だけ現状まとめを出す
ERROR_ALERT_AT = 3  # 連続でこの回数取れなかったら知らせる

STATUS_RE = re.compile(
    r"販売中|発売中|受付中|配布中|再開|販売終了|受付終了|受付を終了|停止|完売|売切|売り切れ"
    r"|終了しました|上限に達し|発売開始|販売開始|予約開始")
# 「空きが出た・買える」側の語。通知で 🟢 を付ける
OPEN_RE = re.compile(r"販売中|発売中|受付中|配布中|再開")
PREF_RE = re.compile(r"福岡|佐賀|長崎|熊本|大分|宮崎|鹿児島")


# ---------------------------------------------------------------- 取得

def fetch(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ja"})
    last = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.headers.get_content_charset()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
    raise last


def decode(body, charset=None):
    if not charset:
        m = re.search(rb'charset=["\']?([\w-]+)', body[:4000], re.I)
        charset = m.group(1).decode() if m else None
    for enc in filter(None, [charset, "utf-8", "cp932"]):
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")


# ---------------------------------------------------------------- 抽出

BLOCK_END = re.compile(r"</(p|div|li|tr|h[1-6]|dt|dd|table|section|ul|ol|header|footer)\s*>|<br\s*/?>", re.I)


def html_to_lines(text):
    text = re.sub(r"<(script|style|noscript)\b.*?</\1\s*>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<!-\s?-.*?-\s?->", " ", text, flags=re.S)
    text = BLOCK_END.sub("\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    lines = []
    for raw in text.split("\n"):
        line = re.sub(r"\s+", " ", raw).strip()
        if line:
            lines.append(line)
    return lines


def extract_text(text):
    """状況を表す行だけを順序つきで返す。県名を含まない行には直前の県名行を添える。"""
    out, pref_ctx = [], ""
    for line in html_to_lines(text):
        if PREF_RE.search(line) and len(line) <= 40 and not STATUS_RE.search(line):
            pref_ctx = line
            continue
        if not STATUS_RE.search(line):
            continue
        line = line[:200]
        if pref_ctx and not PREF_RE.search(line):
            line = f"[{pref_ctx}] {line}"
        if line not in out:
            out.append(line)
    return out


def extract_rakuten(text):
    """楽天の県別ページ。クーポン枠ごとの状態クラスを数える（例: {"expired": 5}）。"""
    counts = {}
    for st in re.findall(r"couponOneClick-status-([a-zA-Z]+)", text):
        counts[st] = counts.get(st, 0) + 1
    if not counts:
        raise ValueError("クーポン枠が見つからない（ページの作りが変わった可能性）")
    return counts


def find_csv_url(page, base, prefix):
    page = re.sub(r"<!--.*?-->", " ", page, flags=re.S)
    urls = re.findall(r"csvUrl\s*:\s*['\"]([^'\"]+\.csv)['\"]", page)
    path = urls[-1] if urls else f"/csv/{prefix}_stay_search.csv"
    return urllib.parse.urljoin(base, path)


def pick_col(header, *keys):
    for i, h in enumerate(header):
        h = h.split("\n")[0].strip()
        if any(k in h for k in keys):
            return i
    return None


def extract_staycsv(src):
    page = decode(*fetch(src["url"]))
    csv_url = find_csv_url(page, src["url"], src["prefix"])
    body, cs = fetch(csv_url)
    text = body.decode("utf-8-sig") if not cs else decode(body, cs)
    if "�" in text or not text.strip():
        text = body.decode("cp932", "replace")
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    header, data = rows[0], rows[1:]
    name_i = pick_col(header, "施設名", "宿泊施設", "名称")
    note_i = pick_col(header, "お知らせ", "備考", "販売状況")
    hp_i = pick_col(header, "ホームページ", "HP")
    if name_i is None:
        raise ValueError(f"施設名の列が見つからない: {header}")
    facilities, hp = {}, {}
    for r in data:
        if len(r) <= name_i or not r[name_i].strip():
            continue
        name = re.sub(r"\s+", " ", r[name_i]).strip()
        cells = [r[note_i]] if note_i is not None and note_i < len(r) else r
        sold = any("完売" in c or "受付終了" in c for c in cells)
        key = name
        n = 2
        while key in facilities:
            key, n = f"{name} ({n})", n + 1
        facilities[key] = "完売" if sold else "掲載中"
        hp[key] = r[hp_i].strip() if hp_i is not None and hp_i < len(r) else ""
    # "_hp" はペット判定にだけ使い、state.json には残さない
    return {"csv": csv_url.rsplit("/", 1)[-1], "facilities": facilities, "_hp": hp}


def check(src):
    if src["kind"] == "staycsv":
        return extract_staycsv(src)
    text = decode(*fetch(src["url"]))
    if src["kind"] == "rakuten":
        return extract_rakuten(text)
    lines = extract_text(text)
    if not lines:
        raise ValueError("状況を表す行が1つも取れない（ページの作りが変わった可能性）")
    return lines


# ---------------------------------------------------------------- 差分

def pet_key(src, name):
    return f"{src['id']}|{name}"


def diff(src, old, new, pets=None):
    """(🟢 買える側の変化, その他の変化, 🐶 ペット可の宿の買える側の変化) を文字列のリストで返す。"""
    good, other, dog = [], [], []
    pets = pets or {}
    if src["kind"] == "text":
        added = [l for l in new if l not in old]
        removed = [l for l in old if l not in new]
        for l in added:
            (good if OPEN_RE.search(l) else other).append(f"＋ {l}")
        for l in removed:
            other.append(f"－ {l}")
    elif src["kind"] == "rakuten":
        if old != new:
            fmt = lambda d: " / ".join(f"{k}×{v}" for k, v in sorted(d.items()))
            line = f"クーポン枠の状態: {fmt(old)} → {fmt(new)}"
            closed = {"expired", "finished", "end", "soldout"}
            (good if set(new) - closed else other).append(line)
    elif src["kind"] == "staycsv":
        of, nf = old["facilities"], new["facilities"]
        for name, st in nf.items():
            p = pets.get(pet_key(src, name))
            line = None
            if name not in of:
                line = f"新規掲載: {name}（{st}）{pet.mark(p)}"
                (good if st != "完売" else other).append(line)
            elif of[name] == "完売" and st != "完売":
                line = f"完売が外れた: {name}{pet.mark(p)}"
                good.append(line)
            elif of[name] != "完売" and st == "完売":
                other.append(f"完売になった: {name}{pet.mark(p)}")
            if line and st != "完売" and p and p["pet"] in pet.PET_OK:
                dog.append(line)
        for name in of:
            if name not in nf:
                other.append(f"一覧から消えた: {name}")
    return good, other, dog


def summarize(src, state):
    if src["kind"] == "staycsv":
        f = state["facilities"]
        sold = sum(1 for v in f.values() if v == "完売")
        return f"{len(f)}施設（完売 {sold}）"
    if src["kind"] == "rakuten":
        return " / ".join(f"{k}×{v}" for k, v in sorted(state.items()))
    return f"状況行 {len(state)}"


AIR_LABEL = {"none": "🚗宿のみ可", "must": "✈️航空券必須", "mixed": "🚗/✈️商品による"}
CLOSED_RE = re.compile(r"終了|停止|完売|売切|売り切れ|上限に達")


def open_lines(state):
    """text の状況行のうち「いま買える」と読めるものだけ（注意書きと終了の告知は除く）。"""
    return [l for l in state if OPEN_RE.search(l) and "再開" not in l
            and not CLOSED_RE.search(l) and not l.lstrip("[").startswith("※")]


PET_NOTE = "（施設名と公式サイトの記載から自動判定。犬の大きさ・頭数・料金は各宿で確認してください）"


def pet_list(src, sources_state, pets, title, cap=15, note=True):
    """ペット可（要確認を含む）で、いま完売表示がない宿の一覧。"""
    f = ((sources_state.get(src["id"]) or {}).get("state") or {}).get("facilities", {})
    hits = [(n, pets[pet_key(src, n)]) for n, v in f.items()
            if v != "完売" and (pets.get(pet_key(src, n)) or {}).get("pet") in pet.PET_OK]
    hits.sort(key=lambda h: (h[1]["pet"] != "可", h[0]))
    lines = [f"・{n}{pet.mark(p)}" for n, p in hits[:cap]]
    if len(hits) > cap:
        lines.append(f"・…ほか{len(hits) - cap}施設")
    head = f"🐶 *{title}: <{src['url']}|{src['name']}>* 完売表示なしのペット可 {len(hits)}施設"
    return head + (PET_NOTE if note else "") + ("\n" + "\n".join(lines) if lines else "")


def daily_summary(sources_state, stamp, now, pets=None):
    """毎朝の現状まとめ。各ソースの最新状態から、買えそうなものを先に並べる。"""
    stays, cars, flights, failed = [], [], [], []
    for src in SOURCES:
        entry = sources_state.get(src["id"]) or {}
        st = entry.get("state")
        link = f"<{src['url']}|{src['name']}>"
        if entry.get("errors"):
            failed.append(src["name"])
        if st is None:
            continue
        if src["kind"] == "staycsv":
            f = st["facilities"]
            sold = sum(1 for v in f.values() if v == "完売")
            line = f"・{link}: {len(f)}施設中 完売{sold}（残り{len(f) - sold}施設は申し込める可能性）"
            for key in FOCUS_STAYS:
                hits = [(n, v) for n, v in f.items() if key.lower() in n.lower()]
                if hits:
                    line += "\n　　" + " / ".join(f"{n}: {'完売' if v == '完売' else '完売表示なし'}" for n, v in hits)
            stays.append(line)
            continue
        if src["kind"] == "rakuten":
            live = {k: v for k, v in st.items() if k != "expired"}
            text = ("受付中の枠あり（" + ", ".join(f"{k}×{v}" for k, v in sorted(live.items())) + "）") if live else "全枠終了"
        elif src.get("air"):
            ol = open_lines(st)
            text = " / ".join(l[:80] for l in ol[:3]) if ol else "販売中の表示なし"
        else:
            continue  # 県公式・観光庁は変化があったときだけ載せる
        (flights if src.get("air") == "must" else cars).append(f"・{link}［{AIR_LABEL[src['air']]}］: {text}")

    today = now.strftime("%Y-%m-%d")
    upcoming = [f"・{u['at'][5:].replace('-', '/').replace(' 00:00', '')} {u['name']}［{AIR_LABEL[u['air']]}］"
                for u in sorted(UPCOMING, key=lambda u: u["at"]) if u["at"][:10] >= today]
    out = [f"*【九州ふっこう割ウォッチ】現状まとめ（{stamp}）*"]
    out.append("*🚗 宿の直接予約（県の参画施設一覧）*\n" + "\n".join(stays))
    if pets:
        dog = [pet_list(s, sources_state, pets, "ペット同伴可", cap=8, note=False) for s in SOURCES if s["kind"] == "staycsv"]
        out.append("\n".join(dog) + "\n" + PET_NOTE)
    out.append("*予約サイト・旅行会社*\n" + "\n".join(cars))
    if upcoming:
        out.append("*日時が決まっている次の販売*\n" + "\n".join(upcoming))
    out.append("*【参考】航空券必須*\n" + "\n".join(flights))
    out.append(f"{len(SOURCES)}ソースを10分ごとに巡回中" + (f"。取得失敗: {', '.join(failed)}" if failed else "。すべて取得できています") + "。")
    return "\n\n".join(out)


# ---------------------------------------------------------------- 通知

def post_slack(text, dry_run):
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if dry_run or not url:
        print("----- 通知（未送信） -----\n" + text + "\n--------------------------")
        return
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()


def main():
    dry_run = "--dry-run" in sys.argv
    now = dt.datetime.now(JST)
    stamp = now.strftime("%m/%d %H:%M")
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            saved = json.load(f)
    except FileNotFoundError:
        saved = {}
    sources_state = saved.setdefault("sources", {})

    def run(src):
        try:
            return src, check(src), None
        except Exception as e:  # noqa: BLE001 — 1ソースの失敗で全体を止めない
            return src, None, f"{type(e).__name__}: {e}"[:200]

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(run, SOURCES))

    # 宿ごとのペット同伴可否（少しずつ調べて覚えておく。差分の印と毎朝のまとめに使う）
    pets = saved.setdefault("pet", {})
    targets = {}
    for src, new, err in results:
        if src["kind"] == "staycsv" and not err:
            hp = new.pop("_hp")
            targets.update({pet_key(src, n): (n, hp.get(n, "")) for n in new["facilities"]})
    checked_sources = {s["id"] for s, n, e in results if s["kind"] == "staycsv" and not e}
    for key in list(pets):
        if key.split("|", 1)[0] in checked_sources and key not in targets:
            del pets[key]
    try:
        pet_checked = pet.update(pets, targets, now.strftime("%Y-%m-%d"), fetch, decode, html_to_lines)
    except Exception as e:  # noqa: BLE001 — ペット判定の失敗で空き監視を止めない
        print(f"::warning::ペット判定に失敗: {type(e).__name__}: {e}")
        pet_checked = 0

    goods, dogs, air_goods, others, alerts, baselines, report = [], [], [], [], [], [], []
    for src, new, err in results:
        prev = sources_state.get(src["id"])
        link = f"<{src['url']}|{src['name']}>"
        if src.get("air"):
            link += f"［{AIR_LABEL[src['air']]}" + (f"・{src['note']}" if src.get("note") else "") + "］"
        if err:
            n = (prev or {}).get("errors", 0) + 1
            entry = dict(prev or {}, errors=n, last_error=err)
            sources_state[src["id"]] = entry
            if n == ERROR_ALERT_AT:
                alerts.append(f"⚠️ {link}: {n}回続けて取得できていません（{err}）")
            report.append(f"| {src['name']} | ❌ {err} |")
            continue
        recovered = prev and prev.get("errors", 0) >= ERROR_ALERT_AT
        sources_state[src["id"]] = {"state": new, "errors": 0}
        if recovered:
            alerts.append(f"✅ {link}: 取得が復旧しました")
        report.append(f"| {src['name']} | {summarize(src, new)} |")
        if not prev or "state" not in prev:
            baselines.append(src["name"])
            continue
        good, other, dog = diff(src, prev["state"], new, pets)
        if dog:
            dogs.append(f"*{link}*\n" + "\n".join(f"　🐶 {d}" for d in dog[:20]))
        if src.get("air") == "must":
            # 車で行く前提なので、航空券必須の予約先は変化を参考として載せるだけ（メンションしない）
            if good or other:
                air_goods.append(f"*{link}*\n" + "\n".join(([f"　🟢 {g}" for g in good] + [f"　{o}" for o in other])[:20]))
            continue
        if good:
            goods.append(f"*{link}*\n" + "\n".join(f"　🟢 {g}" for g in good[:20]))
        if other:
            more = f"\n　…ほか{len(other) - 15}件" if len(other) > 15 else ""
            others.append(f"*{link}*\n" + "\n".join(f"　{o}" for o in other[:15]) + more)

    parts = []
    if dogs:
        parts.append(f"🐶 *【九州ふっこう割ウォッチ】ペット同伴可の宿に空きの可能性（{stamp}）*\n" + "\n".join(dogs))
    if goods:
        parts.append(f"🟢 *【九州ふっこう割ウォッチ】空き・再開の可能性（{stamp}）*\n" + "\n".join(goods))
    if air_goods:
        parts.append(f"*【参考】航空券とセットでしか買えない予約先の変化（{stamp}）*\n" + "\n".join(air_goods))
    if others:
        parts.append(f"*【九州ふっこう割ウォッチ】状況の変化（{stamp}）*\n" + "\n".join(others))
    if alerts:
        parts.append("\n".join(alerts))
    if baselines and len(baselines) == len(SOURCES):
        parts.append(f"【九州ふっこう割ウォッチ】監視を始めました（{stamp}）。{len(SOURCES)}ソースを巡回します。"
                     "以後は変化があったときだけ投稿し、毎朝8時台に1回だけ現状まとめを出します。")
    elif baselines:
        parts.append(f"【九州ふっこう割ウォッチ】新しく監視に加えました: {', '.join(baselines)}")

    done, dogs_done = saved.setdefault("pet_done", []), []
    for src in SOURCES:
        if src["id"] not in checked_sources or src["id"] in done:
            continue
        keys = [k for k in targets if k.startswith(src["id"] + "|")]
        if keys and all(k in pets for k in keys):
            done.append(src["id"])
            dogs_done.append(pet_list(src, sources_state, pets, f"ペット同伴可の宿を調べ終えました（{stamp}）"))

    parts[:0] = dogs_done
    today = now.strftime("%Y-%m-%d")
    saved.pop("last_heartbeat", None)  # 旧名（生存確認だけだった頃）
    if now.hour >= HEARTBEAT_HOUR and saved.get("last_summary") != today:
        saved["last_summary"] = today
        parts.append(daily_summary(sources_state, stamp, now, pets))

    if "--ping" in sys.argv and not parts:
        parts.append(f"【九州ふっこう割ウォッチ】テスト投稿（{stamp}）: Slack への通知が届いています。"
                     f"{len(SOURCES)}ソースを巡回中です。")
    if parts:
        # 検知（空き・変化・取得異常）とテスト投稿だけメンションを付けて、Slack の通知・バッジを鳴らす。
        # 開始・現状まとめは鳴らさない
        mention = os.environ.get("SLACK_MENTION", "").strip()
        if mention and (dogs or dogs_done or goods or others or alerts or "--ping" in sys.argv):
            parts[0] = f"{mention} {parts[0]}"
        post_slack("\n\n".join(parts), dry_run)
    else:
        print(f"変化なし（{stamp}）")

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(saved, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"## 巡回結果 {stamp}\n\n| ソース | 状態 |\n|---|---|\n" + "\n".join(report) + "\n")
            f.write(f"\nペット判定: 今回 {pet_checked} 施設を調べた（判定済み {sum(1 for k in targets if k in pets)}/{len(targets)}）\n")
            if parts:
                f.write("\n### 通知\n\n```\n" + "\n\n".join(parts) + "\n```\n")


if __name__ == "__main__":
    main()
