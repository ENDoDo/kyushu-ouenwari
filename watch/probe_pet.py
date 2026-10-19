"""一時的な調査用: ペット判定の精度を見る（PR マージ前に消す）"""
import collections, os, random, sys
sys.path.insert(0, os.path.dirname(__file__))
import pet
from check import SOURCES, decode, extract_staycsv, fetch, html_to_lines
random.seed(2)
items = []
for src in SOURCES:
    if src["kind"] != "staycsv": continue
    r = extract_staycsv(src)
    hp = r["_hp"]
    print(src["id"], "施設", len(hp), "HPあり", sum(1 for v in hp.values() if pet.normalize_url(v)))
    items += [(src["id"], n, h) for n, h in hp.items()]
sample = random.sample(items, 300)
from concurrent.futures import ThreadPoolExecutor
with ThreadPoolExecutor(16) as ex:
    res = list(ex.map(lambda t: (t, pet.judge(t[1], t[2], fetch, decode, html_to_lines)), sample))
print(collections.Counter(r["pet"] for _, r in res))
print(collections.Counter(r["why"].split(":")[0] for _, r in res if r["pet"] == "不明").most_common(8))
for (sid, n, h), r in res:
    if r["pet"] in ("可", "要確認"): print("OK ", r["pet"], sid, n, h, "|", r["why"])
for (sid, n, h), r in [x for x in res if x[1]["pet"] == "不可"][:15]:
    print("NG ", sid, n, "|", r["why"])
