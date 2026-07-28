"""golden-file 測試：每個 corpus case 的 map 產出必須與 expected/mapping.json
逐位元組相符。

跟 `test_golden_inventory.py` 同樣的結構：golden 是回歸保護，case01 的具名
性質斷言才是真正說明「我們期待 map 階段做什麼」的地方（ruling 6）。這裡的
`Mapping` 一律用內建 catalog／規則、從 `expected/inventory.json`（已經由
`test_golden_inventory.py` 證明過與 `scan()` 逐位元組相符的 golden）算出，
與 `update_golden.py` 實際會跑的管線（scan → map，皆用內建規則）一致。
"""

import json

import pytest

from erp_migrai.events import EventLog
from erp_migrai.mapping.catalog import load_catalog
from erp_migrai.mapping.engine import build_mapping
from erp_migrai.mapping.models import Tier
from erp_migrai.mapping.rules_loader import load_rules
from erp_migrai.mapping.writer import validate_mapping, write_mapping

CATALOG = load_catalog()
RULES = load_rules(CATALOG)


def _log(tmp_path):
    return EventLog(path=tmp_path / "events.jsonl", run_id="test")


def _inventory_dict(case):
    golden = case / "expected" / "inventory.json"
    return json.loads(golden.read_text(encoding="utf-8"))


def _map_case(case, tmp_path):
    inventory = _inventory_dict(case)
    log = _log(tmp_path)
    mapping = build_mapping(inventory, CATALOG, RULES, event_log=log)
    return mapping, log


def test_golden_mapping_matches(corpus_case, tmp_path):
    mapping, _ = _map_case(corpus_case, tmp_path)
    produced = write_mapping(mapping, tmp_path / "out")
    golden = corpus_case / "expected" / "mapping.json"
    assert golden.is_file(), f"{corpus_case.name} 缺少 expected/mapping.json"
    assert produced.read_text(encoding="utf-8") == golden.read_text(encoding="utf-8")


def test_golden_mapping_passes_schema(corpus_case):
    golden = corpus_case / "expected" / "mapping.json"
    assert golden.is_file(), f"{corpus_case.name} 缺少 expected/mapping.json"
    validate_mapping(json.loads(golden.read_text(encoding="utf-8")))


def test_golden_mapping_is_deterministic_across_two_runs(corpus_case, tmp_path):
    mapping_1, _ = _map_case(corpus_case, tmp_path / "a")
    mapping_2, _ = _map_case(corpus_case, tmp_path / "b")
    assert mapping_1.to_dict() == mapping_2.to_dict()


class TestCase01PartnerMapping:
    """case01 的具名性質斷言（ruling 6 要求的那 6 條）。

    這些斷言獨立於 golden 檔本身：golden 若跟這裡的期待值不符，以這裡為準去
    查原因，而不是回頭改斷言去遷就 golden。
    """

    @pytest.fixture
    def mapping(self, corpus_root, tmp_path):
        case01 = corpus_root / "case01_partner"
        return _map_case(case01, tmp_path)[0]

    def test_seven_mappings_all_auto_with_full_confidence(self, mapping):
        assert len(mapping.mappings) == 7
        assert all(fm.tier == Tier.AUTO for fm in mapping.mappings)
        assert all(fm.confidence == 100 for fm in mapping.mappings)

    def test_no_unmapped_columns(self, mapping):
        assert mapping.unmapped == []

    def test_no_conflicts(self, mapping):
        assert mapping.conflicts == []

    def test_coverage_percent_is_100(self, mapping):
        assert mapping.stats.coverage_percent == 100

    def test_inventory_synthetic_flag_is_true(self, mapping):
        assert mapping.inventory_synthetic is True
