"""`update_golden.main()` 的失敗處理契約：修正輪次 1 對 Important 1 的回歸測試。

`main()` 對 scan 階段的承諾是「處理每一個 case，收集失敗、總是印出審閱提示、
區分新建與更新、有失敗才回傳 1」，這裡驗證 map 階段（`build_mapping` /
`write_mapping`）也遵守同一套契約：一個 case 在 map 階段失敗，不能讓其餘
case 陪葬，也不能讓失敗被悄悄吞掉。

刻意不把 `build_mapping` 整個換成假的成功/失敗開關——那樣測到的只是我們
自己設計的假行為。這裡包一層薄殼：只在讀到 `inventory_dict` 裡出現一張
專門為了觸發失敗而命名的表（`zzz_map_fail_trigger`）時，才動手腳拿掉
`inventory_dict` 的 `synthetic` 鍵（`engine._REQUIRED_INVENTORY_KEYS` 要求
的必要鍵之一），再原封不動呼叫真正的 `build_mapping`；其餘 case 完全呼叫
未經改動的真正 `build_mapping`。真正拋出 `MapFailed` 的，因此仍然是
`engine.py` 自己的 `_validate_inventory`，不是測試捏造出來的例外——這個
案例會失敗，是因為它的輸入真的違反了 `build_mapping` 的契約，不是因為我們
攔截了函式呼叫本身。
"""

from __future__ import annotations

from pathlib import Path

from erp_migrai.mapping.engine import build_mapping
from erp_migrai.tools import update_golden

_MINIMAL_4GL = """DATABASE ds

MAIN
    DEFINE g_dummy INTEGER
END MAIN
"""

_OK_SCHEMA_SQL = """CREATE TABLE cust_mast (
    cust_no CHAR(10) NOT NULL,
    PRIMARY KEY (cust_no)
);
"""

# 表名刻意取得顯眼、不會跟任何真實語料撞名，讓下面的 monkeypatch 只鎖定這一
# 個 case，不會誤傷其他 case。
_BAD_TABLE_NAME = "zzz_map_fail_trigger"
_BAD_SCHEMA_SQL = f"""CREATE TABLE {_BAD_TABLE_NAME} (
    dummy CHAR(1),
    PRIMARY KEY (dummy)
);
"""


def _make_case(corpus_root: Path, name: str, schema_sql: str) -> Path:
    case_dir = corpus_root / name
    src = case_dir / "src"
    src.mkdir(parents=True)
    (src / "schema.sql").write_text(schema_sql, encoding="utf-8")
    (src / "prog.4gl").write_text(_MINIMAL_4GL, encoding="utf-8")
    return case_dir


def _build_corpus(tmp_path: Path) -> Path:
    """三個 case：前後各一個乾淨的、中間一個會在 map 階段失敗。"""
    corpus_root = tmp_path / "corpus"
    _make_case(corpus_root, "case_a_ok", _OK_SCHEMA_SQL)
    _make_case(corpus_root, "case_b_bad", _BAD_SCHEMA_SQL)
    _make_case(corpus_root, "case_c_ok", _OK_SCHEMA_SQL)
    return corpus_root


def _build_mapping_break_one_case(inventory_dict, catalog, rules, *, event_log):
    """真正的 `build_mapping`，只在讀到觸發表名時才餵它壞資料。"""
    table_names = {t.get("name") for t in inventory_dict.get("tables", [])}
    if _BAD_TABLE_NAME in table_names:
        broken = {k: v for k, v in inventory_dict.items() if k != "synthetic"}
        return build_mapping(broken, catalog, rules, event_log=event_log)
    return build_mapping(inventory_dict, catalog, rules, event_log=event_log)


def test_map_stage_failure_is_collected_and_other_cases_still_processed(
    tmp_path, monkeypatch, capsys
):
    corpus_root = _build_corpus(tmp_path)
    monkeypatch.setattr(update_golden, "CORPUS_ROOT", corpus_root)
    monkeypatch.setattr(update_golden, "build_mapping", _build_mapping_break_one_case)

    exit_code = update_golden.main()
    captured = capsys.readouterr()

    # 1. 有失敗，回傳 1（不是被吃掉的 0）。
    assert exit_code == 1

    # 2. 其他 case 仍然照跑：兩個乾淨 case 的 golden 都完整寫出。
    for name in ("case_a_ok", "case_c_ok"):
        assert (corpus_root / name / "expected" / "inventory.json").is_file()
        assert (corpus_root / name / "expected" / "mapping.json").is_file()
        assert f"✓ {name}" in captured.out

    # 3. 失敗的 case 被收集、指名道姓，且訊息點出 inventory.json／mapping.json
    #    的混合狀態（inventory 已覆寫、mapping 未寫入）。
    assert "✓ case_b_bad" not in captured.out
    assert "✗ case_b_bad" in captured.err
    assert "inventory.json" in captured.err
    assert "mapping.json" in captured.err
    assert (corpus_root / "case_b_bad" / "expected" / "inventory.json").is_file()
    assert not (corpus_root / "case_b_bad" / "expected" / "mapping.json").is_file()

    # 4. 審閱提示一定要印出來，即使有 case 失敗——這正是這個工具存在的理由。
    assert "審閱" in captured.out
