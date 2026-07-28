"""把 .4gl 切成頂層程式單元。這一層不使用文法，且絕不會拋出例外。"""

from erp_migrai.inventory.blocks import split_blocks

SOURCE = """DATABASE ds

GLOBALS
    DEFINE g_user CHAR(10)
END GLOBALS

MAIN
    LET g_user = "admin"
    CALL cust_maintain()
END MAIN

FUNCTION cust_maintain()
    DEFINE l_cnt INTEGER
END FUNCTION

REPORT cust_list(p_rec)
    DEFINE p_rec RECORD LIKE cust_mast.*
END REPORT
"""


def test_database_directive_is_captured():
    result = split_blocks(SOURCE, source_file="a.4gl")
    assert result.database == "ds"


def test_all_four_block_kinds_are_found_in_source_order():
    result = split_blocks(SOURCE, source_file="a.4gl")
    assert [(b.kind, b.name) for b in result.blocks] == [
        ("GLOBALS", None),
        ("MAIN", None),
        ("FUNCTION", "cust_maintain"),
        ("REPORT", "cust_list"),
    ]


def test_block_line_range_is_one_based_and_covers_the_keyword_lines():
    result = split_blocks(SOURCE, source_file="a.4gl")
    main = next(b for b in result.blocks if b.kind == "MAIN")
    assert (main.start_line, main.end_line) == (7, 10)


def test_block_body_excludes_the_keyword_lines():
    result = split_blocks(SOURCE, source_file="a.4gl")
    main = next(b for b in result.blocks if b.kind == "MAIN")
    assert [text.strip() for _, text in main.lines] == [
        'LET g_user = "admin"',
        "CALL cust_maintain()",
    ]
    assert [line for line, _ in main.lines] == [8, 9]


def test_all_blocks_are_marked_terminated():
    result = split_blocks(SOURCE, source_file="a.4gl")
    assert all(b.terminated for b in result.blocks)
    assert result.unsupported == []


def test_unterminated_block_runs_to_eof_and_is_recorded():
    result = split_blocks("MAIN\n    CALL foo()\n", source_file="a.4gl")
    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.terminated is False
    assert block.end_line == 2
    assert len(result.unsupported) == 1
    assert "未正常結束" in result.unsupported[0].reason


def test_top_level_junk_is_recorded_not_raised():
    result = split_blocks("SOMETHING WEIRD\n", source_file="a.4gl")
    assert result.blocks == []
    assert len(result.unsupported) == 1
    assert result.unsupported[0].line == 1


def test_globals_include_form_is_a_single_line_block():
    result = split_blocks('GLOBALS "common.4gl"\n', source_file="a.4gl")
    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.kind == "GLOBALS"
    assert (block.start_line, block.end_line) == (1, 1)
    assert block.lines == []
    assert result.unsupported == []


def test_comments_and_blank_lines_at_top_level_are_ignored():
    result = split_blocks("# 註解\n-- 另一種註解\n\nMAIN\nEND MAIN\n", source_file="a.4gl")
    assert len(result.blocks) == 1
    assert result.unsupported == []


def test_never_raises_on_arbitrary_bytes():
    """不管餵什麼進來都不能爆——這是 scan 永不中止的底線。"""
    weird = "END FUNCTION\n)))\nFUNCTION\nMAIN\nMAIN\n"
    result = split_blocks(weird, source_file="a.4gl")
    assert isinstance(result.blocks, list)


def test_never_raises_on_null_bytes_and_other_junk():
    """null bytes、雜亂的 BOM 字元夾雜在各處，都不能讓掃描器拋例外。"""
    weird = "MAIN\n\x00\x00\x00\nEND MAIN\n\x00FUNCTION\x00foo\x00(\x00)\x00\n﻿\n"
    result = split_blocks(weird, source_file="a.4gl")
    assert isinstance(result.blocks, list)
    assert isinstance(result.unsupported, list)


# --- Finding 1：未終止的區塊不能悄悄吞掉下一個區塊 ---------------------------


def test_unterminated_function_absorbing_next_function_is_reported_not_silent():
    """foo 沒有寫 END FUNCTION，緊接著 bar 開了新的 FUNCTION。修正前，bar 的
    END FUNCTION 會被誤判為 foo 的收尾，foo 被謊報 terminated=True，bar 整個消失、
    unsupported 是空的。修正後：foo 在 bar 開始前結束（terminated=False 並回報），
    bar 成為獨立的程式單元。"""
    source = "FUNCTION foo()\n    LET x = 1\nFUNCTION bar()\nEND FUNCTION\n"
    result = split_blocks(source, source_file="a.4gl")

    assert [(b.kind, b.name, b.terminated) for b in result.blocks] == [
        ("FUNCTION", "foo", False),
        ("FUNCTION", "bar", True),
    ]

    foo = result.blocks[0]
    assert foo.end_line == 2
    assert [text.strip() for _, text in foo.lines] == ["LET x = 1"]

    bar = result.blocks[1]
    assert (bar.start_line, bar.end_line) == (3, 4)

    assert any(u.line == 1 and "FUNCTION" in u.reason for u in result.unsupported), (
        result.unsupported
    )


def test_unterminated_function_absorbing_main_block_is_reported_not_silent():
    """foo 沒有寫 END FUNCTION，裡面又完整寫了一個 MAIN...END MAIN。修正前，MAIN
    整個被吞進 foo 的 body，foo 被誤判為已終止，MAIN 這個程式單元徹底消失且毫無
    回報。修正後：foo 在 MAIN 開始前結束（未終止並回報），MAIN 成為獨立的程式單元。"""
    source = "FUNCTION foo()\n    LET x = 1\nMAIN\nEND MAIN\nEND FUNCTION\n"
    result = split_blocks(source, source_file="a.4gl")

    assert [(b.kind, b.name, b.terminated) for b in result.blocks] == [
        ("FUNCTION", "foo", False),
        ("MAIN", None, True),
    ]

    foo = result.blocks[0]
    assert foo.end_line == 2

    main = result.blocks[1]
    assert (main.start_line, main.end_line) == (3, 4)

    assert any(u.line == 1 and "FUNCTION" in u.reason for u in result.unsupported), (
        result.unsupported
    )


def test_mismatched_end_kind_does_not_silently_terminate_the_block():
    """FUNCTION 被 END MAIN 結尾（種類不符）：不能被誤判為已正常結束，必須被回報。"""
    source = "FUNCTION foo()\n    LET x = 1\nEND MAIN\n"
    result = split_blocks(source, source_file="a.4gl")

    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.kind == "FUNCTION"
    assert block.terminated is False

    assert len(result.unsupported) == 1
    assert result.unsupported[0].line == 1
    assert "FUNCTION" in result.unsupported[0].reason


def test_crlf_line_endings_produce_clean_text_and_correct_line_numbers():
    """Windows 系的 ERP 廠常見 CRLF 換行；行內容與名稱都不該殘留 \\r，行號要正確。"""
    source = "FUNCTION foo()\r\n    LET x = 1\r\nEND FUNCTION\r\n"
    result = split_blocks(source, source_file="a.4gl")

    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.name == "foo"
    assert "\r" not in (block.name or "")
    assert all("\r" not in text for _, text in block.lines)
    assert [line for line, _ in block.lines] == [2]
    assert (block.start_line, block.end_line) == (1, 3)


def test_utf8_bom_prefixed_first_line_is_reported_not_silently_mis_split():
    """鎖定現況行為：BOM 由檔案讀取層（之後的任務會用 utf-8-sig）處理，
    這一層不做任何 BOM 剝除。這裡只驗證『有回報、不會靜默誤判』——
    帶 BOM 的第一行目前無法辨識為區塊起始，會被記錄為 UnsupportedSyntax。"""
    source = "﻿MAIN\n    LET x = 1\nEND MAIN\n"
    result = split_blocks(source, source_file="a.4gl")

    assert result.blocks == []
    assert len(result.unsupported) >= 1
    assert result.unsupported[0].line == 1


def test_last_line_without_trailing_newline_is_still_parsed():
    """檔案最後一行沒有結尾換行也要能正常切分。"""
    source = "MAIN\n    LET x = 1\nEND MAIN"
    result = split_blocks(source, source_file="a.4gl")

    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.terminated is True
    assert (block.start_line, block.end_line) == (1, 3)


def test_function_with_no_name_returns_none_name():
    """FUNCTION 關鍵字後面沒有任何識別字時，name 應為 None 而非拋例外。
    這是頂層寬鬆判斷的行為，嚴格 header 只用於區塊內部的 opener 偵測，
    不影響頂層——此測試必須在修正後維持不變。"""
    result = split_blocks("FUNCTION\nEND FUNCTION\n", source_file="a.4gl")

    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.kind == "FUNCTION"
    assert block.name is None
    assert block.terminated is True


# --- Fix round 2：內層 opener 誤判一般敘述句為區塊起始 --------------------------


def test_record_field_named_main_does_not_fragment_the_block():
    """RECORD 欄位取名 main 是合理的真實 ERP 寫法，不該被誤判成 MAIN 區塊 opener。"""
    source = (
        "FUNCTION foo()\n"
        "DEFINE rec RECORD\n"
        "    main SMALLINT,\n"
        "    cnt INTEGER\n"
        "END RECORD\n"
        "END FUNCTION\n"
    )
    result = split_blocks(source, source_file="a.4gl")

    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.kind == "FUNCTION"
    assert block.name == "foo"
    assert block.terminated is True
    assert result.unsupported == []


def test_body_prose_lines_do_not_fragment_the_block():
    """body 裡出現的一般文字（非合法 4GL），只要開頭像關鍵字，也不該被誤判成 opener。
    這種文字本身不合法，之後會由 parser.py 那一層回報；這一層的職責只是不要把
    區塊切碎。"""
    source = "FUNCTION foo()\n    Main logic:\n    Report the totals to screen.\nEND FUNCTION\n"
    result = split_blocks(source, source_file="a.4gl")

    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.kind == "FUNCTION"
    assert block.terminated is True
    assert result.unsupported == []


def test_genuine_opener_with_trailing_comment_is_still_detected_mid_scan():
    """真正的 opener 後面帶行內註解，仍要能被偵測為 opener。"""
    source = "FUNCTION foo()\n    LET x = 1\nFUNCTION bar()  # 說明\nEND FUNCTION\n"
    result = split_blocks(source, source_file="a.4gl")

    assert [(b.kind, b.name, b.terminated) for b in result.blocks] == [
        ("FUNCTION", "foo", False),
        ("FUNCTION", "bar", True),
    ]


def test_bare_main_is_still_detected_as_mid_scan_opener():
    """MAIN 單獨一行仍要能被偵測為 mid-scan opener（避免矯枉過正）。"""
    source = "FUNCTION foo()\n    LET x = 1\nMAIN\nEND MAIN\n"
    result = split_blocks(source, source_file="a.4gl")

    assert [(b.kind, b.terminated) for b in result.blocks] == [
        ("FUNCTION", False),
        ("MAIN", True),
    ]


def test_globals_include_form_is_still_detected_as_mid_scan_opener():
    """GLOBALS "common.4gl" include 形式仍要能被偵測為 mid-scan opener。"""
    source = 'FUNCTION foo()\n    LET x = 1\nGLOBALS "common.4gl"\nEND FUNCTION\n'
    result = split_blocks(source, source_file="a.4gl")

    assert [(b.kind, b.terminated) for b in result.blocks] == [
        ("FUNCTION", False),
        ("GLOBALS", True),
    ]
    globals_block = result.blocks[1]
    assert (globals_block.start_line, globals_block.end_line) == (3, 3)


# --- Fix round 2：邊界情境回歸測試（已驗證行為正確，但先前沒有測試鎖住） -------


def test_opener_as_first_body_line_yields_empty_lines_block():
    """opener 是第一個 body 行時，第一個區塊的 lines 應為空、end_line == start_line。"""
    source = "FUNCTION foo()\nFUNCTION bar()\nEND FUNCTION\n"
    result = split_blocks(source, source_file="a.4gl")

    foo = result.blocks[0]
    assert foo.lines == []
    assert foo.end_line == foo.start_line


def test_three_consecutive_unterminated_blocks_each_reported():
    """連續三個都沒有 END 的區塊，各自成為獨立區塊，各自標記未終止並各回報一筆。"""
    source = "FUNCTION foo()\nFUNCTION bar()\nFUNCTION baz()\n"
    result = split_blocks(source, source_file="a.4gl")

    assert len(result.blocks) == 3
    assert all(b.terminated is False for b in result.blocks)
    assert len(result.unsupported) == 3


def test_globals_include_form_as_mid_scan_opener_is_single_line_block():
    """GLOBALS "x.4gl" 作為 mid-scan opener，應形成單行的 GLOBALS 區塊。"""
    source = 'MAIN\n    LET x = 1\nGLOBALS "x.4gl"\nEND MAIN\n'
    result = split_blocks(source, source_file="a.4gl")

    assert [(b.kind, b.terminated) for b in result.blocks] == [
        ("MAIN", False),
        ("GLOBALS", True),
    ]
    globals_block = result.blocks[1]
    assert (globals_block.start_line, globals_block.end_line) == (3, 3)
    assert globals_block.lines == []


# --- Fix round 3：遮罩註解／字串常值 + 多行參數列 header --------------------


def test_multiline_parameter_list_does_not_swallow_the_next_function():
    """foo() 的參數列跨行未寫完（bar 開了新的 FUNCTION），foo 沒有 END FUNCTION。
    修正前：bar 整個消失、foo 被誤判為已終止、zero anomalies。
    修正後：foo 在 bar 開始前結束（未終止並回報），bar 成為獨立且正常終止的區塊。"""
    source = "FUNCTION foo()\n    LET x = 1\nFUNCTION bar(a,\n    b)\n    LET y = 2\nEND FUNCTION\n"
    result = split_blocks(source, source_file="a.4gl")

    assert [(b.kind, b.name, b.terminated) for b in result.blocks] == [
        ("FUNCTION", "foo", False),
        ("FUNCTION", "bar", True),
    ]

    foo = result.blocks[0]
    assert foo.end_line == 2

    bar = result.blocks[1]
    assert (bar.start_line, bar.end_line) == (3, 6)
    assert [text.strip() for _, text in bar.lines] == ["b)", "LET y = 2"]

    assert len(result.unsupported) == 1
    assert result.unsupported[0].line == 1


def test_well_formed_multiline_parameter_function_is_recognised_as_one_block():
    """單獨一個跨行參數列的 FUNCTION，應正確辨識為一個已終止的區塊，zero anomalies。"""
    source = "FUNCTION bar(a,\n    b)\n    LET y = 2\nEND FUNCTION\n"
    result = split_blocks(source, source_file="a.4gl")

    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.kind == "FUNCTION"
    assert block.name == "bar"
    assert block.terminated is True
    assert result.unsupported == []


def test_trailing_brace_comment_on_header_does_not_swallow_the_next_function():
    """foo 沒有 END FUNCTION；bar 的 header 後面跟著一個 { ... } 行內註解。
    修正前：bar 消失、出現一筆指向錯誤位置的 anomaly。
    修正後：foo/bar 各自獨立成為區塊，且沒有因為那個註解而產生誤導性的 anomaly。"""
    source = (
        "FUNCTION foo()\n    LET x = 1\nFUNCTION bar()   { 說明文字 }\nEND FUNCTION\nEND FUNCTION\n"
    )
    result = split_blocks(source, source_file="a.4gl")

    assert [(b.kind, b.name, b.terminated) for b in result.blocks] == [
        ("FUNCTION", "foo", False),
        ("FUNCTION", "bar", True),
    ]
    assert not any("說明文字" in u.reason or "說明文字" in u.snippet for u in result.unsupported)


def test_fake_end_main_inside_brace_comment_is_invisible():
    """{ ... } 裡藏著一個假的 END MAIN，不能被當成真正的區塊結尾。
    修正前：真正的 MAIN 被腰斬、產生三筆 spurious anomaly。
    修正後：MAIN 應正常涵蓋整段構造，terminated=True，zero anomalies。"""
    source = "MAIN\n  { 這段是註解\n  END MAIN\n  }\n  CALL a()\nEND MAIN\n"
    result = split_blocks(source, source_file="a.4gl")

    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.kind == "MAIN"
    assert (block.start_line, block.end_line) == (1, 6)
    assert block.terminated is True
    assert result.unsupported == []


def test_multiline_brace_comment_with_fake_header_is_invisible_and_line_numbers_stay_correct():
    """多行 { ... } 註解裡藏一個假的 FUNCTION header，不能被當成真正的 opener；
    註解結束後的行號也要維持正確。"""
    source = (
        "MAIN\n  {\n  FUNCTION fake_one()\n  more comment text\n  }\n  CALL real_one()\nEND MAIN\n"
    )
    result = split_blocks(source, source_file="a.4gl")

    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.kind == "MAIN"
    assert (block.start_line, block.end_line) == (1, 7)
    assert block.terminated is True
    assert result.unsupported == []
    assert [line for line, _ in block.lines] == [2, 3, 4, 5, 6]


def test_keyword_inside_string_literal_does_not_close_the_block():
    """DISPLAY "END MAIN" 是字串常值，裡面的 END MAIN 文字不能被誤判為真正的區塊結尾。"""
    source = 'MAIN\n    DISPLAY "END MAIN"\n    CALL a()\nEND MAIN\n'
    result = split_blocks(source, source_file="a.4gl")

    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.kind == "MAIN"
    assert block.terminated is True
    assert (block.start_line, block.end_line) == (1, 4)
    assert result.unsupported == []


def test_unterminated_brace_comment_recovers_at_end_of_line_and_later_block_is_found():
    """{ 沒有對應的 }，只能在開啟的那一行結尾復原，並回報一筆 UnsupportedSyntax
    （理由需提到「未終止」）；檔案後面的合法區塊仍要能被正確辨識。"""
    source = (
        "FUNCTION foo()\n"
        "    { 未結束的註解\n"
        "    LET x = 1\n"
        "END FUNCTION\n"
        "FUNCTION bar()\n"
        "    LET y = 2\n"
        "END FUNCTION\n"
    )
    result = split_blocks(source, source_file="a.4gl")

    assert [(b.kind, b.name, b.terminated) for b in result.blocks] == [
        ("FUNCTION", "foo", True),
        ("FUNCTION", "bar", True),
    ]

    assert len(result.unsupported) == 1
    assert result.unsupported[0].line == 2
    assert "未終止" in result.unsupported[0].reason


def test_unterminated_string_literal_recovers_at_end_of_line_and_block_still_closes():
    """字串常值沒有結尾引號，只能在該行結尾復原，並回報一筆 UnsupportedSyntax
    （理由需提到「未終止」）；區塊本身仍要能正常結束。"""
    source = 'MAIN\n    LET x = "unterminated\n    CALL a()\nEND MAIN\n'
    result = split_blocks(source, source_file="a.4gl")

    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.kind == "MAIN"
    assert block.terminated is True
    assert (block.start_line, block.end_line) == (1, 4)

    assert len(result.unsupported) == 1
    assert result.unsupported[0].line == 2
    assert "未終止" in result.unsupported[0].reason


def test_block_lines_still_carries_original_text_not_masked_text():
    """Block.lines 必須保留原始文字（含註解），不是遮罩後的空白版本。"""
    source = "MAIN\n    LET x = 1  # 註解\nEND MAIN\n"
    result = split_blocks(source, source_file="a.4gl")

    block = result.blocks[0]
    assert any("# 註解" in text for _, text in block.lines)


# --- Fix round 5：{ ... } 不支援巢狀，內容裡出現 { 才是漏寫 } 的健全訊號 ------


def test_unclosed_brace_comment_swallowing_program_units_is_reported():
    """alpha 的 { 忘了寫對應的 }；全文下一個 } 出現在 gamma 一個正確關閉的
    註解裡。{ ... } 本來就是配對「下一個」}，這是 4GL 編譯器本身的行為，
    我們的讀法沒有錯、錯的是原始碼——因此刻意不縮小遮罩範圍，不去猜測 }
    「應該」出現在哪裡。alpha 仍然被判定為 terminated=True、橫跨到第 13 行
    （beta、gamma 都被吃掉、消失在 blocks 之外）：這個行為本身不變，
    改變的只是「這件事現在會被回報」——因為這段被吞掉的範圍裡，gamma 那個
    正確關閉的註解自己的 { 也落在 alpha 的 { ... } 涵蓋範圍內形成巢狀，
    而 { } 註解本來就不支援巢狀，這是判斷「疑似漏寫 }」的健全依據。"""
    source = (
        "FUNCTION alpha()\n"
        "  { forgot to close this one\n"
        "  LET x = 1\n"
        "END FUNCTION\n"
        "\n"
        "FUNCTION beta()\n"
        "  LET y = 2\n"
        "END FUNCTION\n"
        "\n"
        "FUNCTION gamma()\n"
        "  { a totally unrelated, correctly closed comment }\n"
        "  LET z = 3\n"
        "END FUNCTION\n"
    )
    result = split_blocks(source, source_file="a.4gl")

    # 行為明確釘死：不縮小遮罩範圍，beta、gamma 仍然消失，alpha 仍被誤判為
    # terminated=True，橫跨到 gamma 那個正確關閉的 } 為止（第 13 行）。
    assert [(b.kind, b.name, b.terminated, b.start_line, b.end_line) for b in result.blocks] == [
        ("FUNCTION", "alpha", True, 1, 13)
    ]

    assert len(result.unsupported) == 1
    anomaly = result.unsupported[0]
    assert anomaly.line == 2  # { 開啟的那一行，不是遠方那個 } 所在的行
    assert "{" in anomaly.reason  # 提及內容裡又出現了一個 {
    assert "}" in anomaly.reason  # 提及可能漏寫對應的 }
    assert "11" in anomaly.reason  # 內層那個 { 出現的行號（gamma 的註解開頭）


def test_multiline_brace_comment_with_ordinary_prose_produces_zero_anomalies():
    """單純的多行敘述性註解，內容裡沒有任何 {，不該觸發「疑似漏寫 }」的
    回報——這是這個規則低誤報率的保證：{ } 註解不支援巢狀，正常內容裡
    本來就不會出現 {。"""
    source = (
        "MAIN\n"
        "  { This module handles month-end closing.\n"
        "    It reads the ledger, applies adjustments,\n"
        "    and writes the summary report. }\n"
        "  CALL do_closing()\n"
        "END MAIN\n"
    )
    result = split_blocks(source, source_file="a.4gl")

    assert len(result.blocks) == 1
    assert result.blocks[0].terminated is True
    assert result.unsupported == []


def test_multiline_brace_comment_mentioning_end_kind_mid_sentence_produces_zero_anomalies():
    """註解裡提到 END MAIN，但內容裡沒有任何 {，不該被誤判為漏寫 }。"""
    source = (
        "MAIN\n"
        "  { Don't forget to write END MAIN\n"
        "    at the very bottom of this program. }\n"
        "  CALL do_something()\n"
        "END MAIN\n"
    )
    result = split_blocks(source, source_file="a.4gl")

    assert len(result.blocks) == 1
    assert result.blocks[0].terminated is True
    assert result.unsupported == []


def test_doc_comment_showing_a_header_end_pair_produces_zero_anomalies():
    """說明性註解裡示範一段程式碼長什麼樣子（含 FUNCTION header 與
    END FUNCTION），是正常、常見的寫法，且註解本身正確關閉——內容裡沒有
    任何 {，新規則不會誤報。這正是舊「命中門檻」機制曾經誤報的案例，
    新規則從結構上就不會誤觸。"""
    source = (
        "MAIN\n  { Example:\n  FUNCTION sample()\n  END FUNCTION\n  }\n  CALL real()\nEND MAIN\n"
    )
    result = split_blocks(source, source_file="a.4gl")

    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.kind == "MAIN"
    assert block.terminated is True
    assert result.unsupported == []


def test_unclosed_brace_comment_with_content_containing_no_brace_is_not_reported():
    """已知殘留限制（刻意接受，見模組 docstring）：foo 忘了寫對應的 }，
    全文下一個 } 出現在 bar 的 END FUNCTION 之後（junk }）。這段被吞掉的
    內容（"oops" / "END FUNCTION" / "junk"）裡沒有任何 {，所以新規則
    （內容裡出現 { 才回報）不會觸發——這正是文件裡承認的殘留限制：
    猜不出來就不猜，寧可不報也不憑空捏造 } 應該在哪裡。foo 因此被誤判為
    terminated=True，bar 真正的 END FUNCTION 與後面的 junk 都被吞進 foo
    的 body，且沒有任何 anomaly。"""
    source = "FUNCTION foo()\n  { oops\nEND FUNCTION\njunk }\nmore junk\nEND FUNCTION\n"
    result = split_blocks(source, source_file="a.4gl")

    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.kind == "FUNCTION"
    assert block.name == "foo"
    assert block.terminated is True
    assert block.end_line == 6
    assert result.unsupported == []


# --- Task 5 Resolution 1：Block.masked_lines 供 parser.py 做關鍵字判斷 -------


def test_block_masked_lines_is_masked_while_lines_is_original():
    """Block.lines 保留原始文字（含行內註解），Block.masked_lines 同一行的註解
    字元已換成空白——parser.py 靠這個欄位做語句關鍵字判斷，才不會被註解或
    字串常值裡剛好長得像關鍵字的文字誤導，這是 blocks.py 自身遮罩判斷同一套
    原則的延伸。"""
    source = "MAIN\n    LET x = 1  # 註解\nEND MAIN\n"
    result = split_blocks(source, source_file="a.4gl")

    block = result.blocks[0]
    raw_line = next(text for line, text in block.lines if line == 2)
    masked_line = next(text for line, text in block.masked_lines if line == 2)

    assert "# 註解" in raw_line
    assert "註解" not in masked_line
    assert masked_line.strip().startswith("LET x = 1")
    # 遮罩只把字元換成空白，不改變行數或行長度。
    assert len(masked_line) == len(raw_line)
    assert [line for line, _ in block.masked_lines] == [line for line, _ in block.lines]


def test_unclosed_brace_comment_with_no_closing_brace_anywhere_reports_only_one_anomaly():
    """{ 全文都找不到任何對應的 }（未終止）時，只回報既有的「未終止」anomaly
    一筆。這裡故意讓 { 未終止的復原（只在開啟那一行行尾復原）之後緊接著出現
    真正的 END FUNCTION、以及另一個 FUNCTION bar() header——藉此確認：找到
    一個遙遠但存在的 } 與完全找不到 } 是兩種不同情境，彼此的回報邏輯必須
    互斥，不會在同一份輸入上重複回報。"""
    source = (
        "FUNCTION foo()\n"
        "  { forgot to close this comment\n"
        "  END FUNCTION\n"
        "  FUNCTION bar()\n"
        "  LET y = 1\n"
        "END FUNCTION\n"
    )
    result = split_blocks(source, source_file="a.4gl")

    assert [(b.kind, b.name, b.terminated) for b in result.blocks] == [
        ("FUNCTION", "foo", True),
        ("FUNCTION", "bar", True),
    ]
    assert len(result.unsupported) == 1
    assert "未終止" in result.unsupported[0].reason
