"""一時的な調査用: 宿一覧 CSV にペット情報があるか見る（PR マージ前に消す）"""
import csv, io, os, re, sys
sys.path.insert(0, os.path.dirname(__file__))
from check import decode, fetch, find_csv_url
for pref, url in [("oita","https://oita-fukkou-wari.jp/stay/"),("kagoshima","https://fukko-ouenwari.pref.kagoshima.jp/stay/"),
                  ("fukuoka","https://fukuoka-fukkou-wari.jp/stay/"),("nagasaki","https://nagasaki-fukkou-wari.jp/stay/"),
                  ("saga","https://saga-ouen.jp/stay/"),("miyazaki","https://miyazaki-ouen.jp/stay/")]:
    print("=====", pref, url)
    try:
        page = decode(*fetch(url))
        print("page len", len(page), "ペット in page:", page.count("ペット"))
        for m in re.finditer(r".{0,120}ペット.{0,120}", page):
            print("  PAGE:", re.sub(r"\s+"," ",m.group(0))); break
        for m in re.findall(r"<(?:input|option|label)[^>]*>[^<]{0,40}", page):
            if "ペット" in m or "pet" in m.lower(): print("  FORM:", m[:200])
        cu = find_csv_url(page, url, pref); print("csv", cu)
        body, cs = fetch(cu)
        text = body.decode("utf-8-sig") if not cs else decode(body, cs)
        if "�" in text or not text.strip(): text = body.decode("cp932","replace")
        rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
        print("HEADER:", rows[0]); print("ROW1:", rows[1][:40])
        pet = [r for r in rows[1:] if any("ペット" in c or "犬" in c for c in r)]
        print("rows with ペット/犬:", len(pet), "/", len(rows)-1)
        for r in pet[:5]:
            print("  ", [ (rows[0][i] if i < len(rows[0]) else i, c[:80]) for i,c in enumerate(r) if "ペット" in c or "犬" in c][:4], r[0][:30])
    except Exception as e:
        print("ERR", type(e).__name__, e)
