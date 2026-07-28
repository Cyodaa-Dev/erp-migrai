"""重新產生所有 corpus case 的 `expected/inventory.json` 與 `expected/mapping.json`。

⚠️ 生成後必須人工審閱過才 commit：golden 檔就是我們對工具行為的斷言，
未經審閱的 golden 只會把 bug 固化成「預期行為」。

審閱方式依 golden 是「新增」還是「變動」而不同（見下方 main() 的收尾提示）：
一份**變動**的 golden，`git diff corpus/` 看得到差異，照著讀即可；但一份
**全新**的 golden 是 untracked 檔案，`git diff corpus/` 對 untracked 檔案
完全不會印出任何內容——貢獻者若照著舊提示跑這個指令，看到空 diff 會誤以為
「沒有東西要審」，因而在完全沒讀過內容的情況下 commit 一份未經審閱的
golden。因此這裡改成直接判斷每一份 golden 檔在寫入前是否已存在（純粹比較
檔案系統狀態，不 shell out 到 git），新增與變動分開提示正確的審閱方式。

`inventory.json` 與 `mapping.json` 是同一個 case 的兩個獨立產物，各自的
新增／變動狀態要分開追蹤：`mapping.json` 是本次任務才新增的第二份 golden，
一個 case 完全可能出現「`inventory.json` 已存在、這次只是重新產生一份完全
相同或有差異的檔案」但「`mapping.json` 第一次出現」的情形（現有的
`case01_partner` 正是如此）——若把兩者混在同一個新／舊分類裡，這種混合
情形的提示會失真。

map 階段一律用套件內建的 catalog（`odoo17_catalog.yaml`）與內建規則
（`rules/*.yaml`），跟 `tests/mapping/test_engine.py`、
`tests/test_golden_mapping.py` 驗證的組態完全一致——golden 就是要凍住「預設
安裝長什麼樣子」這件事，不是某次呼叫端剛好傳入的組態。map 的輸入是重新寫出
的 `inventory.json` 本身（讀回檔案內容，而不是沿用記憶體裡的 `Inventory`
物件），這樣才忠實反映 map 階段實際消費的是磁碟上的 `inventory.json`，不是
`scan()` 回傳物件裡可能還沒序列化過的內部狀態。
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from jsonschema import ValidationError

from erp_migrai.events import EventLog
from erp_migrai.inventory.extractor import ScanFailed, scan, write_inventory
from erp_migrai.mapping.catalog import load_catalog
from erp_migrai.mapping.engine import MapFailed, build_mapping
from erp_migrai.mapping.rules_loader import load_rules
from erp_migrai.mapping.writer import write_mapping

CORPUS_ROOT = Path(__file__).resolve().parents[3] / "corpus"

# map 階段用的內建 catalog／規則，模組層級載入一次即可（跟兩份測試檔共用同一份
# 載入方式，行為與 golden 要凍住的「預設安裝」一致）。
_CATALOG = load_catalog()
_RULES = load_rules(_CATALOG)


@dataclass
class GoldenArtifact:
    """單一 golden 檔（`inventory.json` 或 `mapping.json`）在這次重新產生前後
    的新／舊狀態，供結尾的審閱提示分類使用。"""

    case_name: str
    label: str
    path: Path
    is_new: bool


@dataclass
class CaseResult:
    name: str
    tables: int
    programs: int
    unsupported: int
    mapped: int
    unmapped: int
    conflicts: int
    coverage_percent: int
    artifacts: list[GoldenArtifact] = field(default_factory=list)


def main() -> int:
    if not CORPUS_ROOT.is_dir():
        print(f"找不到語料庫目錄：{CORPUS_ROOT}", file=sys.stderr)
        return 1

    cases = sorted(p for p in CORPUS_ROOT.iterdir() if p.is_dir() and (p / "src").is_dir())
    if not cases:
        print(f"{CORPUS_ROOT} 底下沒有任何 case", file=sys.stderr)
        return 1

    # 追蹤成功與失敗的 case，以便最後統一報告。
    # 儘管某個 case 的掃描失敗，我們仍須處理其他 case：部份重寫如果沒有公告，
    # 比完全失敗還危險，因為貢獻者會在不知情的狀況下 commit 未經審閱的 golden。
    succeeded_cases: list[CaseResult] = []
    failed_cases: list[tuple[str, str]] = []

    for case in cases:
        expected = case / "expected"
        expected.mkdir(parents=True, exist_ok=True)

        inventory_path = expected / "inventory.json"
        # 寫入前先記下這份 golden 是否已存在——這是判斷「新增」還是「變動」
        # 唯一可靠的時機點，寫完之後檔案必然存在，就再也分不出來了。
        inventory_is_new = not inventory_path.is_file()

        with tempfile.TemporaryDirectory() as tmp:
            log = EventLog(path=Path(tmp) / "events.jsonl", run_id="update-golden")
            try:
                inventory = scan(case / "src", event_log=log)
            except ScanFailed as exc:
                failed_cases.append((case.name, str(exc)))
                continue

        write_inventory(inventory, expected)

        mapping_path = expected / "mapping.json"
        mapping_is_new = not mapping_path.is_file()

        inventory_dict = json.loads(inventory_path.read_text(encoding="utf-8"))
        # map 階段跟 scan 階段一樣，一個 case 失敗不能讓其餘 case 陪葬：
        # `build_mapping` 依契約在 inventory 缺必要鍵時拋 `MapFailed`，
        # `write_mapping` 會在寫出前驗證 schema、驗證失敗時拋
        # `jsonschema.ValidationError`（這是它刻意設計要驗證出來的情形，
        # 不是理論上才會發生的分支）。兩者都不是「根本沒東西可寫」，而是
        # 「上一步（scan）已經把這個 case 的 `inventory.json` 覆寫成新內容」
        # 之後才失敗——訊息裡明講這一點，讓人知道這個 case 目前是
        # inventory.json 已更新、mapping.json 未更新的混合狀態，不能直接當作
        # 「整個 case 這次沒動」略過不管。
        try:
            with tempfile.TemporaryDirectory() as tmp:
                log = EventLog(path=Path(tmp) / "events.jsonl", run_id="update-golden")
                mapping = build_mapping(inventory_dict, _CATALOG, _RULES, event_log=log)

            write_mapping(mapping, expected)
        except (MapFailed, ValidationError) as exc:
            detail = exc.message if isinstance(exc, ValidationError) else str(exc)
            failed_cases.append(
                (
                    case.name,
                    f"map 階段失敗（{detail}）；"
                    "inventory.json 已依本次重新掃描的結果覆寫，"
                    "但 mapping.json 未寫入，這個 case 目前是新舊混雜狀態，"
                    "commit 前務必先確認 inventory.json 的異動是否要保留",
                )
            )
            continue

        num_tables = len(inventory.tables)
        num_programs = len(inventory.programs)
        num_unsupported = len(inventory.unsupported_syntax)
        num_mapped = len(mapping.mappings)
        num_unmapped = len(mapping.unmapped)
        num_conflicts = len(mapping.conflicts)
        coverage_percent = mapping.stats.coverage_percent

        succeeded_cases.append(
            CaseResult(
                name=case.name,
                tables=num_tables,
                programs=num_programs,
                unsupported=num_unsupported,
                mapped=num_mapped,
                unmapped=num_unmapped,
                conflicts=num_conflicts,
                coverage_percent=coverage_percent,
                artifacts=[
                    GoldenArtifact(case.name, "inventory.json", inventory_path, inventory_is_new),
                    GoldenArtifact(case.name, "mapping.json", mapping_path, mapping_is_new),
                ],
            )
        )
        print(
            f"✓ {case.name}："
            f"{num_tables} 表 / {num_programs} 程式單元 / {num_unsupported} 處不支援語法"
            f"；map {num_mapped} 對應 / {num_unmapped} 未對應 / {num_conflicts} 衝突"
            f"（覆蓋率 {coverage_percent}%）"
            f" → {inventory_path}, {mapping_path}"
        )

    # 只要有任何 golden 被寫出，就需要提醒人工審閱——但「新增」與「變動」
    # 兩種情況下正確的審閱方式不一樣，分開提示。以每一份「檔案」（而不是每個
    # case）為單位分類：同一個 case 底下 inventory.json／mapping.json 兩份
    # 檔案的新舊狀態彼此獨立，混在一起會讓提示失真。
    if succeeded_cases:
        all_artifacts = [artifact for result in succeeded_cases for artifact in result.artifacts]
        changed = [a for a in all_artifacts if not a.is_new]
        new = [a for a in all_artifacts if a.is_new]
        print()
        if changed:
            print(
                "⚠️ 以下 golden 檔原本就存在（本次重新產生），"
                "請逐行審閱 `git diff corpus/` 之後再 commit"
                "（若這次跑出來跟原本完全一樣，diff 會是空的，那也是正常結果）："
            )
            for a in changed:
                print(f"  - {a.case_name} / {a.label}：{a.path}")
        if new:
            print(
                "⚠️ 以下 golden 檔是第一次產生：`git diff corpus/` 對尚未加入 git 的"
                "全新檔案不會顯示任何內容，看到空 diff 不代表沒有東西要審閱。"
                "請直接開啟並閱讀每一份檔案本身"
                "（或先執行 `git add -N corpus/ && git diff corpus/`，"
                "讓新檔案也能以「整份新增」的形式出現在 diff 裡）："
            )
            for a in new:
                print(f"  - {a.case_name} / {a.label}：{a.path}")

    # 輸出失敗摘要到 stderr。
    if failed_cases:
        for case_name, error_msg in failed_cases:
            print(f"✗ {case_name}：{error_msg}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
