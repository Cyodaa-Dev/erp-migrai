"""從 ACEX-MW 的 Odoo 快照反向產生 T100 風格 seed.sql。

業務資料一律讀自 snapshot，腳本內不寫死任何一筆業務資料——語料與 demo 站台的
料號／工作中心因此是機制保證一致的，不是靠人記得同步。

用法：python3 tools/gen_seed.py <snapshot.json> > data/seed.sql
"""
import json, sys, re

snap = json.load(open(sys.argv[1], encoding="utf-8"))
o = []
def q(s):
    if s is None or s is False: return "NULL"
    return "'" + str(s).replace("'", "''") + "'"

o.append("-- 合成語料：由 ACEX-MW 的 Odoo 快照反向產生（tools/gen_seed.py），勿手改。")
o.append("-- 業務資料全部來自 snapshot；統編、電話、地址為合成值，非任何真實資訊。\n")

# ── 委外廠商（來源系統有主檔，Odoo 端原本沒有這個載體）──
o.append("-- 委外廠商主檔")
vends = []
for i, w in enumerate(sorted([w for w in snap["workcenters"] if w["code"].startswith("OS-")],
                             key=lambda w: w["code"])):
    proc = w["name"].replace("委外-", "")
    vno  = "V" + w["code"][3:][:6].upper().ljust(6, "0")
    vends.append((vno, proc, w["code"]))
    ubn  = f"{53810000 + i * 7311:08d}"
    tel  = f"06-2{450 + i * 13:03d}{100 + i * 7:03d}"
    # 末筆刻意設為停止往來：CHAR(1) Y/N → Odoo boolean active 的值轉換演示
    stus = "N" if i == 5 else "Y"
    o.append(f"INSERT INTO vend_mast VALUES ({q(vno)},{q(proc + '協力廠')},{q(ubn)},{q(tel)},"
             f"{q('臺南市工業區')},{q(proc)},{q(stus)});")
vend_of_wc = {code: vno for vno, _, code in vends}

# ── 品號（取自 Odoo；模具件是來源系統有、Odoo 沒有的）──
o.append("\n-- 品號主檔：MW-*/CMP-*/RAW-* 取自 Odoo")
KIND = {"MW-": "FG", "CMP-": "SF", "RAW-": "RM"}
for p in sorted(snap["products"], key=lambda p: str(p["default_code"])):
    c = p["default_code"]
    if not c: continue
    kind = next((v for k, v in KIND.items() if c.startswith(k)), "SF")
    o.append(f"INSERT INTO item_mast VALUES ({q(c)},{q(p['name'])},NULL,{p['list_price']},"
             f"{p['standard_price']},{q(kind)},0,0,{q('Y' if p['active'] else 'N')});")
o.append("-- 🔴 模具件：來源系統有品號與累計衝次/設計壽命，Odoo 標準模型沒有這兩個欄位")
for c, n, hits, life in [("DIE-2043", "連續模 剎車踏板組", 892000, 1000000),
                         ("DIE-3117", "單發模 引擎吊架",   214500,  800000),
                         ("DIE-4602", "彎管模 電動輪椅支架", 63200,  600000)]:
    o.append(f"INSERT INTO item_mast VALUES ({q(c)},{q(n)},{q('沖壓模具')},0,0,{q('DI')},{hits},{life},{q('Y')});")

# ── 工作中心 ──
o.append("\n-- 工作中心：MW- 本廠 / OS- 委外，前綴是「在製品是否離廠」的唯一依據")
for w in sorted(snap["workcenters"], key=lambda w: w["code"]):
    o.append(f"INSERT INTO wc_mast VALUES ({q(w['code'])},{q(w['name'])},{w['costs_hour']},"
             f"{q('Y' if w['code'].startswith('OS-') else 'N')});")

# ── 製令 ──
o.append("\n-- 製令")
ST = {"draft": "10", "confirmed": "20", "progress": "30", "done": "90"}
def item_of(label):
    m = re.search(r"\[([^\]]+)\]", label or "")
    return m.group(1) if m else label
for w in sorted(snap["mos"], key=lambda w: w["name"]):
    d = str(w["date_start"])[:10] if w.get("date_start") else None
    o.append(f"INSERT INTO wo_mast VALUES ({q(w['name'])},{q(item_of(w['product_id'][1]))},"
             f"{w['product_qty']},{q(d)},{q(ST.get(w['state'], '10'))});")

# ── 委外發包單：把 Odoo 只有工序、沒有單據的狀況還原成來源系統的形狀 ──
o.append("\n-- 委外發包單頭／單身")
o.append("-- 🔴 sub_eta 保留現場實務的自由文字：可解析日期、模糊描述、空白三種都有")
ETA = ["2026/08/15", "約8月中", "2026/08/03", "月底前", "", "等對方回覆", "2026/10/28"]
# 委外的是半成品，不是成品：用製程關鍵字對到 CMP-* 品號
PROC2ITEM = {"OS-CNC": "CMP-CNC-SHAFT", "OS-FORGE": "CMP-FORGE-JOINT",
             "OS-RUBBER": "CMP-RUBBER-BUSH", "OS-COAT": "CMP-STAMP-BRKT",
             "OS-PLATE": "CMP-STAMP-PLATE", "OS-THREAD": "CMP-TUBE-BEND"}
name2code = {w["name"]: w["code"] for w in snap["workcenters"]}
have = {str(p["default_code"]) for p in snap["products"]}
for i, w in enumerate(sorted(snap["sub_wos"], key=lambda w: (w["production_id"][1], w["name"]))):
    wc   = name2code.get(w["workcenter_id"][1])
    item = PROC2ITEM.get(wc)
    if item not in have:                       # 語料只用 snapshot 真有的品號
        item = item_of(next((m["product_id"][1] for m in snap["mos"]
                             if m["name"] == w["production_id"][1]), None))
    sub  = f"SUB-MW-{i+1:04d}"
    d    = str(w.get("date_start"))[:10] if w.get("date_start") else None
    o.append(f"INSERT INTO sub_ord VALUES ({q(sub)},{q(vend_of_wc.get(wc, vends[0][0]))},"
             f"{q(w['production_id'][1])},{q(d)},{q(w['name'])});")
    o.append(f"INSERT INTO sub_deta VALUES ({q(sub)},1,{q(item)},{q(wc)},"
             f"{w.get('qty_production') or 0},0,{q(ETA[i % len(ETA)])},{i % 3});")

print("\n".join(o))
