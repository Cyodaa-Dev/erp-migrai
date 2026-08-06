"""對一台真實 Odoo 實例 introspect，產生欄位 catalog。

現有的 `odoo17_catalog.yaml` 是**人工整理**的（檔頭自己聲明可能有錯）。
本工具改用 `fields_get` 對真機核實，並保留原本的策展範圍——只驗我們實際
會映射的欄位，不把模型的全部欄位倒出來（res.partner 在 Odoo 19 有 200+ 欄，
全倒出來沒人讀得完，也不是 catalog 的用途）。

輸出與 `odoo17_catalog.yaml` 同一個 schema，因此 `load_catalog(path=...)`
可以直接吃，不需要改任何程式碼。

用法：
    python3 tools/introspect_catalog.py \
        --url http://127.0.0.1:8069 --db odoo19 --user U --password P \
        --base src/erp_migrai/mapping/odoo17_catalog.yaml \
        --extra extra_models.yaml \
        --out src/erp_migrai/mapping/odoo19_catalog.yaml
"""
from __future__ import annotations
import argparse, sys, xmlrpc.client
from datetime import datetime, timezone, timedelta

import yaml

TPE = timezone(timedelta(hours=8))


def connect(url: str, db: str, user: str, pw: str):
    uid = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common").authenticate(db, user, pw, {})
    if not uid:
        sys.exit("認證失敗：帳號、密碼或資料庫名稱不對")
    obj = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
    return uid, obj


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--base", required=True, help="既有 catalog（提供策展範圍與 help 文字）")
    ap.add_argument(
        "--extra",
        help="要新增的模型（YAML: {model: {module, description, fields: [...]}}）",
    )
    ap.add_argument("--out", required=True)
    ap.add_argument("--version", default="19.0")
    a = ap.parse_args()

    uid, obj = connect(a.url, a.db, a.user, a.password)
    base = yaml.safe_load(open(a.base, encoding="utf-8"))
    want: dict[str, list[str]] = {m: list(v["fields"]) for m, v in base["models"].items()}
    helps = {m: {f: d.get("help", "") for f, d in v["fields"].items()} for m, v in base["models"].items()}
    descs = {m: v.get("description", "") for m, v in base["models"].items()}

    extra_meta: dict[str, dict] = {}
    if a.extra:
        for m, spec in (yaml.safe_load(open(a.extra, encoding="utf-8")) or {}).items():
            extra_meta[m] = spec
            want.setdefault(m, [])
            for f in spec["fields"]:
                if f not in want[m]:
                    want[m].append(f)

    out: dict = {"models": {}}
    missing: list[str] = []
    added: list[str] = []

    for model, fields in want.items():
        try:
            got = obj.execute_kw(a.db, uid, a.password, model, "fields_get", [], {"attributes": ["type", "required", "string", "help"]})
        except Exception as e:                       # 模型本身不存在（模組沒裝）
            missing.append(f"{model}（整個模型不存在：{str(e)[:60]}）")
            continue
        desc = descs.get(model) or (extra_meta.get(model, {}).get("description") or "").strip()
        if not desc:
            sys.exit(f"模型 {model} 缺 description——catalog 的審核者需要它，請在 --extra 補上")
        entry: dict = {"description": desc, "fields": {}}
        for f in fields:
            if f not in got:
                missing.append(f"{model}.{f}")
                continue
            g = got[f]
            entry["fields"][f] = {
                "type": g.get("type"),
                "required": bool(g.get("required")),
                # module 無法由 fields_get 取得；沿用既有 catalog 的記載，新欄位標 unknown
                "module": (
                    (base["models"].get(model, {}).get("fields", {}).get(f, {}) or {}).get("module")
                    or extra_meta.get(model, {}).get("module")
                    or "unknown"
                ),
                "help": helps.get(model, {}).get(f) or (g.get("help") or g.get("string") or ""),
            }
            if model not in base["models"] or f not in base["models"][model]["fields"]:
                added.append(f"{model}.{f}")
        out["models"][model] = entry

    header = (
        f"# Odoo {a.version} 目標欄位 catalog —— **對真實實例 introspect 產生**\n"
        f"#\n"
        f"# 產生時間：{datetime.now(TPE).isoformat(timespec='seconds')}\n"
        f"# 產生方式：tools/introspect_catalog.py，對 {a.db} 執行 fields_get\n"
        f"# 基準策展範圍：{a.base}\n"
        f"#\n"
        f"# 與人工整理的 odoo17_catalog.yaml 不同，這份檔案的 type/required 是機器讀來的，\n"
        f"# 不是維護者抄的。但 `module` 欄位 fields_get 取不到，沿用基準檔記載；\n"
        f"# 新增模型的 module 取自 --extra 的宣告。\n"
        f"#\n"
        f"# 🔴 重跑本工具前請確認目標實例已安裝所需模組——欄位存在與否取決於模組是否安裝，\n"
        f"#    對一台沒裝 purchase 的 Odoo introspect，會誤判 purchase.order 不存在。\n"
    )
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(header + "\n")
        yaml.safe_dump(out, fh, allow_unicode=True, sort_keys=False, width=100)

    print(f"✅ {a.out}：{len(out['models'])} 模型 / {sum(len(m['fields']) for m in out['models'].values())} 欄位")
    if added:
        print(f"\n新增 {len(added)} 個欄位：")
        for x in added: print("   +", x)
    if missing:
        print(f"\n🔴 基準檔有、但目標實例查無 {len(missing)} 項（版本漂移或模組未裝）：")
        for x in missing: print("   -", x)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
