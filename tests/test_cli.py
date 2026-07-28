"""migrai scan 的 CLI 契約：exit code 與雙軌輸出。"""

import json
import os

import pytest
from typer.testing import CliRunner

from erp_migrai.cli import app

# chmod 500 在 root 下沒有意義（root 會略過檔案權限檢查），這類測試在以 root
# 執行的環境（例如某些容器）下必須乾淨地跳過，而不是用假陽性通過或報錯。
_IS_ROOT = hasattr(os, "getuid") and os.getuid() == 0

runner = CliRunner()

FGL_CLEAN = """DATABASE ds

MAIN
    CALL foo()
END MAIN
"""

FGL_WITH_UNSUPPORTED = """DATABASE ds

MAIN
    TRY
    END TRY
END MAIN
"""

SQL = """CREATE TABLE t (a CHAR(1) NOT NULL, PRIMARY KEY (a));
"""


@pytest.fixture
def src(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    (d / "a.4gl").write_text(FGL_CLEAN, encoding="utf-8")
    (d / "schema.sql").write_text(SQL, encoding="utf-8")
    return d


def test_clean_scan_exits_zero_and_writes_both_outputs(src, tmp_path):
    out = tmp_path / "out"
    result = runner.invoke(app, ["scan", "--src", str(src), "--out", str(out)])
    assert result.exit_code == 0
    assert (out / "inventory.json").is_file()
    assert (out / "events.jsonl").is_file()


def test_stdout_reports_the_summary_in_chinese(src, tmp_path):
    result = runner.invoke(app, ["scan", "--src", str(src), "--out", str(tmp_path / "out")])
    assert "掃描完成" in result.stdout
    assert "inventory.json" in result.stdout


def test_unsupported_syntax_still_exits_zero(src, tmp_path):
    """不支援語法是產出，不是失敗——預設不影響 exit code。"""
    (src / "a.4gl").write_text(FGL_WITH_UNSUPPORTED, encoding="utf-8")
    out = tmp_path / "out"
    result = runner.invoke(app, ["scan", "--src", str(src), "--out", str(out)])
    assert result.exit_code == 0
    inventory = json.loads((out / "inventory.json").read_text(encoding="utf-8"))
    assert len(inventory["unsupported_syntax"]) > 0


def test_strict_turns_unsupported_syntax_into_exit_one(src, tmp_path):
    (src / "a.4gl").write_text(FGL_WITH_UNSUPPORTED, encoding="utf-8")
    result = runner.invoke(
        app, ["scan", "--src", str(src), "--out", str(tmp_path / "out"), "--strict"]
    )
    assert result.exit_code == 1


def test_missing_source_directory_exits_one_with_error_code_in_output(tmp_path):
    result = runner.invoke(
        app, ["scan", "--src", str(tmp_path / "nope"), "--out", str(tmp_path / "out")]
    )
    assert result.exit_code == 1
    assert "E-1001" in result.stdout


def test_events_jsonl_lines_are_valid_json(src, tmp_path):
    out = tmp_path / "out"
    runner.invoke(app, ["scan", "--src", str(src), "--out", str(out)])
    for line in (out / "events.jsonl").read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        assert set(event) == {
            "event_id",
            "event_type",
            "timestamp",
            "severity",
            "error_code",
            "message",
            "details",
            "run_id",
        }


def test_rerunning_does_not_duplicate_events(src, tmp_path):
    """events.jsonl 是 append 的，所以每次執行必須用新的檔案或明確清除。"""
    out = tmp_path / "out"
    runner.invoke(app, ["scan", "--src", str(src), "--out", str(out)])
    first = len((out / "events.jsonl").read_text(encoding="utf-8").splitlines())
    runner.invoke(app, ["scan", "--src", str(src), "--out", str(out)])
    second = len((out / "events.jsonl").read_text(encoding="utf-8").splitlines())
    assert second == first


def test_missing_required_option_exits_two(tmp_path):
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 2


# --- Resolution 1：輸出目錄無法寫入時，不能讓 traceback 外洩 ---


def test_unwritable_out_dir_exits_one_without_traceback(src, tmp_path):
    """--out 指向一個已存在的一般檔案時，mkdir 必然失敗；CLI 必須把這個
    OSError 轉成可讀訊息並以 exit 1 結束，而不是讓 traceback 洩漏給使用者。
    """
    blocked = tmp_path / "blocked"
    blocked.write_text("我是一個檔案，不是目錄", encoding="utf-8")

    result = runner.invoke(app, ["scan", "--src", str(src), "--out", str(blocked)])

    assert result.exit_code == 1
    assert str(blocked) in result.stdout
    assert "Traceback" not in result.stdout
    assert result.exception is None or not isinstance(result.exception, OSError)


# --- Resolution 3：摘要必須誠實描述不支援語法，且超過 10 筆時要截斷 ---


FGL_MANY_UNSUPPORTED = "DATABASE ds\n\nMAIN\n" + "    TRY\n    END TRY\n" * 11 + "END MAIN\n"


def test_unsupported_syntax_summary_states_it_is_not_an_error(src, tmp_path):
    (src / "a.4gl").write_text(FGL_WITH_UNSUPPORTED, encoding="utf-8")
    result = runner.invoke(app, ["scan", "--src", str(src), "--out", str(tmp_path / "out")])
    assert result.exit_code == 0
    assert "不是錯誤" in result.stdout
    assert "issue" in result.stdout or "回報" in result.stdout


def test_unsupported_syntax_list_truncates_after_ten_entries(src, tmp_path):
    (src / "a.4gl").write_text(FGL_MANY_UNSUPPORTED, encoding="utf-8")
    out = tmp_path / "out"
    result = runner.invoke(app, ["scan", "--src", str(src), "--out", str(out)])

    assert result.exit_code == 0
    inventory = json.loads((out / "inventory.json").read_text(encoding="utf-8"))
    total = len(inventory["unsupported_syntax"])
    assert total > 10

    printed = result.stdout.count("[E-1002]")
    assert printed == 10
    assert f"另有 {total - 10} 處" in result.stdout


# --- Resolution 2：掃描進行中若無法寫入事件日誌，必須顯示清晰訊息，不能洩漏 traceback ---


@pytest.mark.skipif(_IS_ROOT, reason="root 會略過檔案權限檢查，chmod 500 測試在 root 下沒有意義")
def test_mid_scan_event_log_write_failure_exits_one_without_traceback(src, tmp_path):
    """掃描進行中若事件日誌無法寫入（例如磁碟滿或權限改變），CLI 必須把這個
    OSError 轉成可讀訊息並以 exit 1 結束，而不是讓 traceback 洩漏給使用者。

    測試方式：先執行一次掃描以建立輸出目錄，再把該目錄設為無寫入權限，
    第二次執行時會在 scan() 期間的第一次 emit() 時失敗。
    """
    out = tmp_path / "out"

    # 第一次掃描：成功建立輸出目錄
    result = runner.invoke(app, ["scan", "--src", str(src), "--out", str(out)])
    assert result.exit_code == 0
    assert (out / "events.jsonl").is_file()

    # 移除 events.jsonl 以模擬磁碟已滿的情況（下次執行無法重新建立）
    (out / "events.jsonl").unlink()

    # 讓輸出目錄不可寫（這會讓 emit() 無法建立新的 events.jsonl）
    out.chmod(0o500)

    try:
        # 第二次掃描：應該在 scan() 期間失敗
        result = runner.invoke(app, ["scan", "--src", str(src), "--out", str(out)])

        assert result.exit_code == 1
        assert str(out) in result.stdout
        assert "Traceback" not in result.stdout
        # 確保沒有洩漏 OSError 或其他異常
        assert result.exception is None or not isinstance(result.exception, OSError)
    finally:
        # 必須恢復權限以便 pytest 清理 tmp_path
        out.chmod(0o755)
