"""直接測試遮罩掃描器 `mask_comments_and_strings`。

這是 `split_blocks` 唯一依賴的遮罩實作，是 `masking.py` 對外公開的入口函式，
重要到值得直接測試，不必每次都繞經 `split_blocks` 的區塊組裝邏輯。
"""

from erp_migrai.inventory.masking import mask_comments_and_strings


def test_mask_brace_inside_line_comment_does_not_open_block_comment():
    """-- 行註解裡出現的 { 不能被誤判為區塊註解的開頭。"""
    text = "-- has a { brace inside\nLET x = 1\n"
    masked, unsupported = mask_comments_and_strings(text, source_file="a.4gl")

    assert unsupported == []
    lines = masked.splitlines()
    assert lines[0].strip() == ""
    assert lines[1] == "LET x = 1"


def test_mask_dash_dash_inside_block_comment_does_not_start_line_comment():
    """{ } 區塊註解裡出現的 -- 不能觸發任何行註解邏輯（{ } 掃描只找 }）。"""
    text = "{ block comment -- not a real line comment }\nLET y = 2\n"
    masked, unsupported = mask_comments_and_strings(text, source_file="a.4gl")

    assert unsupported == []
    lines = masked.splitlines()
    assert lines[0].strip() == ""
    assert lines[1] == "LET y = 2"


def test_mask_hash_inside_string_literal_does_not_start_comment():
    """字串常值裡的 # 不能被誤判為行註解的開頭——否則字串會被提前腰斬，
    讓後面本來屬於字串內容的文字被誤當成程式碼或註解处理。"""
    text = 'LET s = "a#b"\nLET t = 1\n'
    masked, unsupported = mask_comments_and_strings(text, source_file="a.4gl")

    assert unsupported == []
    lines = masked.splitlines()
    assert lines[0] == 'LET s = "   "'
    assert lines[1] == "LET t = 1"


def test_mask_quote_inside_block_comment_does_not_open_string_literal():
    """{ } 區塊註解裡出現的引號不能被誤判為字串常值的開頭。"""
    text = '{ comment with a " lone quote }\nLET z = 1\n'
    masked, unsupported = mask_comments_and_strings(text, source_file="a.4gl")

    assert unsupported == []
    lines = masked.splitlines()
    assert lines[0].strip() == ""
    assert lines[1] == "LET z = 1"


def test_mask_brace_inside_string_literal_does_not_open_block_comment():
    """字串常值裡的 { 不能被誤判為區塊註解的開頭——否則掃描器會去找
    「下一個 }」，可能吃掉遠超過這個字串應有範圍的內容。"""
    text = 'LET s = "a { b"\nEND\n'
    masked, unsupported = mask_comments_and_strings(text, source_file="a.4gl")

    assert unsupported == []
    lines = masked.splitlines()
    assert lines[0] == 'LET s = "     "'
    assert lines[1] == "END"


def test_mask_preserves_character_count_for_traditional_chinese_text():
    """繁體中文字元在字串常值與註解裡都要被遮罩成等長的空白，行長度必須
    維持不變——split_blocks 依賴遮罩後文字與原始文字逐行對齊這個不變量。"""
    text = 'LET s = "測試字串"\n-- 這是中文註解，長度不一定跟英文一樣\nEND\n'
    masked, unsupported = mask_comments_and_strings(text, source_file="a.4gl")

    assert unsupported == []
    original_lines = text.splitlines()
    masked_lines = masked.splitlines()
    assert len(masked_lines) == len(original_lines)
    for orig, mk in zip(original_lines, masked_lines, strict=True):
        assert len(orig) == len(mk)
    assert masked_lines[2] == "END"


def test_mask_bare_cr_line_endings_advance_the_internal_line_counter():
    """Mac 傳統的 \\r-only 換行：遮罩掃描器內部的行號計數過去只認 \\n，導致
    \\r-only 來源檔案的所有 anomaly 都被誤歸到第 1 行。text.splitlines() 早就
    正確處理 \\r，這裡要讓遮罩掃描器的行號計數與它保持一致。"""
    text = "MAIN\r  { unterminated\r  CALL a()\rEND MAIN\r"
    _, unsupported = mask_comments_and_strings(text, source_file="a.4gl")

    assert len(unsupported) == 1
    assert unsupported[0].line == 2
    assert "未終止" in unsupported[0].reason


# --- Fix round 5：{ ... } 不支援巢狀，是判斷「疑似漏寫 }」的健全依據 ----------


def test_mask_nested_brace_inside_multiline_comment_reports_one_anomaly():
    """{ ... } 橫跨多行、內容裡又出現另一個 {：4GL 的 { } 註解本來就不支援
    巢狀，所以合法、正確關閉的註解內容絕不可能包含 {。內容裡出現 { 幾乎
    可以肯定是忘了寫對應的 }，讓外層的 { 一路配對到後面某個不相干的 } 為止。
    只回報一筆，line 指向外層 { 開啟的那一行，reason 要點出內層 { 的行號。"""
    text = "{ outer starts here\n  inner { brace appears\n}\nLET x = 1\n"
    masked, unsupported = mask_comments_and_strings(text, source_file="a.4gl")

    assert len(unsupported) == 1
    anomaly = unsupported[0]
    assert anomaly.line == 1
    assert "{" in anomaly.reason
    assert "}" in anomaly.reason
    assert "2" in anomaly.reason  # 內層 { 出現在第 2 行
    lines = masked.splitlines()
    assert lines[3] == "LET x = 1"


def test_mask_single_line_brace_comment_with_nested_brace_is_not_flagged():
    """單行的 { ... } 就算內容裡出現 {，也不觸發巢狀偵測——新規則的前提是
    「橫跨超過一行」。單行內容全部視為註解本體，沒有跨行吞掉不相干內容的
    風險，因此不需要（也不應該）在這裡報。"""
    text = "{ a { b }\nLET x = 1\n"
    masked, unsupported = mask_comments_and_strings(text, source_file="a.4gl")

    assert unsupported == []
    lines = masked.splitlines()
    assert lines[0].strip() == ""
    assert lines[1] == "LET x = 1"
