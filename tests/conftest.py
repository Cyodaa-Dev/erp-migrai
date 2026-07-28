"""corpus 自動發現。

加一個 case 資料夾＝自動多一組測試，零程式碼修改。
這是貢獻點①能成立的技術前提。
"""

from pathlib import Path

import pytest

CORPUS_ROOT = Path(__file__).resolve().parents[1] / "corpus"


@pytest.fixture(scope="session")
def corpus_root() -> Path:
    """語料庫根目錄。用 fixture 而非 import，測試檔才不必倚賴 tests 是個 package。"""
    return CORPUS_ROOT


def corpus_cases() -> list[Path]:
    """所有具備 src/ 與 expected/ 的 case 目錄，依名稱排序。"""
    if not CORPUS_ROOT.is_dir():
        return []
    return sorted(
        path
        for path in CORPUS_ROOT.iterdir()
        if path.is_dir() and (path / "src").is_dir() and (path / "expected").is_dir()
    )


def pytest_generate_tests(metafunc):
    if "corpus_case" in metafunc.fixturenames:
        cases = corpus_cases()
        metafunc.parametrize("corpus_case", cases, ids=[c.name for c in cases])
