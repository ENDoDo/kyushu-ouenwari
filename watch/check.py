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
from sources import SOURCES  # noqa: E402

STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")
JST = dt.timezone(dt.timedelta(hours=9))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130 Safari/537.36")
HEARTBEAT_HOUR = 8  # JST。この時刻以降の最初の実行で1日1回だけ生存確認を出す
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
    if name_i is None:
        raise ValueError(f"施設名の列が見つからない: {header}")
    facilities = {}
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
    return {"csv": csv_url.rsplit("/", 1)[-1], "facilities": facilities}


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

def diff(src, old, new):
    """(🟢 買える側の変化, その他の変化) を文字列のリストで返す。"""
    good, other = [], []
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
            if name not in of:
                (good if st != "完売" else other).append(f"新規掲載: {name}（{st}）")
            elif of[name] == "完売" and st != "完売":
                good.append(f"完売が外れた: {name}")
            elif of[name] != "完売" and st == "完売":
                other.append(f"完売になった: {name}")
        for name in of:
            if name not in nf:
                other.append(f"一覧から消えた: {name}")
    return good, other


def summarize(src, state):
    if src["kind"] == "staycsv":
        f = state["facilities"]
        sold = sum(1 for v in f.values() if v == "完売")
        return f"{len(f)}施設（完売 {sold}）"
    if src["kind"] == "rakuten":
        return " / ".join(f"{k}×{v}" for k, v in sorted(state.items()))
    return f"状況行 {len(state)}"


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

    goods, others, alerts, baselines, report = [], [], [], [], []
    for src, new, err in results:
        prev = sources_state.get(src["id"])
        link = f"<{src['url']}|{src['name']}>"
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
        good, other = diff(src, prev["state"], new)
        if good:
            goods.append(f"*{link}*\n" + "\n".join(f"　🟢 {g}" for g in good[:20]))
        if other:
            more = f"\n　…ほか{len(other) - 15}件" if len(other) > 15 else ""
            others.append(f"*{link}*\n" + "\n".join(f"　{o}" for o in other[:15]) + more)

    parts = []
    if goods:
        parts.append(f"🟢 *【九州ふっこう割ウォッチ】空き・再開の可能性（{stamp}）*\n" + "\n".join(goods))
    if others:
        parts.append(f"*【九州ふっこう割ウォッチ】状況の変化（{stamp}）*\n" + "\n".join(others))
    if alerts:
        parts.append("\n".join(alerts))
    if baselines and len(baselines) == len(SOURCES):
        parts.append(f"【九州ふっこう割ウォッチ】監視を始めました（{stamp}）。{len(SOURCES)}ソースを巡回します。"
                     "以後は変化があったときだけ投稿し、毎朝8時台に1回だけ生存確認を出します。")
    elif baselines:
        parts.append(f"【九州ふっこう割ウォッチ】新しく監視に加えました: {', '.join(baselines)}")

    today = now.strftime("%Y-%m-%d")
    if now.hour >= HEARTBEAT_HOUR and saved.get("last_heartbeat") != today:
        saved["last_heartbeat"] = today
        if not parts:
            ng = sum(1 for _, n, e in results if e)
            parts.append(f"【九州ふっこう割ウォッチ】生存確認（{stamp}）: {len(SOURCES)}ソース巡回中"
                         + (f"、うち{ng}件は取得失敗" if ng else "、すべて取得できています") + "。直近の巡回で変化はありません。")

    if "--ping" in sys.argv and not parts:
        parts.append(f"【九州ふっこう割ウォッチ】テスト投稿（{stamp}）: Slack への通知が届いています。"
                     f"{len(SOURCES)}ソースを巡回中です。")
    if parts:
        # 検知（空き・変化・取得異常）とテスト投稿だけメンションを付けて、Slack の通知・バッジを鳴らす。
        # 開始・生存確認は鳴らさない
        mention = os.environ.get("SLACK_MENTION", "").strip()
        if mention and (goods or others or alerts or "--ping" in sys.argv):
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
            if parts:
                f.write("\n### 通知\n\n```\n" + "\n\n".join(parts) + "\n```\n")


if __name__ == "__main__":
    main()
