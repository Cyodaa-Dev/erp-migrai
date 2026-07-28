"""文件不得說謊。

治理文件是整個 repo 裡宣稱密度最高的地方——README 說 `make demo` 跑得動、
能力表說某個階段「現在就有」、CONTRIBUTING 說補一條規則只要加幾行 YAML。
這些全都是會隨程式碼漂移的斷言，而傳統上沒有人測它們。

本模組把每一個可機器驗證的斷言釘到真實行為上。文件改了、或程式改了，
兩邊任一動而另一邊沒跟上，就紅燈。

範圍限制：本模組只做**靜態檔案掃描**，讀的是進 git 的檔案內容。任何在
執行期才組出來的字句（例如 CLI 用字串格式化拼出的輸出訊息）不在掃描
範圍內——那類字句仍受同樣的授權限制約束，只是這裡的機制照不到，需要
另外的測試（例如對 CLI 輸出做斷言）才能守住。
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

from erp_migrai.mapping.yamlio import safe_load_strict

REPO_ROOT = Path(__file__).resolve().parents[1]
GITHUB_DIR = REPO_ROOT / ".github"

# 會被散布出去、或會被外人讀到的檔案。`_local/`、`out/`、`.superpowers/`
# 不在其中——它們不進 git，也不會出現在任何人的機器上。
SHIPPED_GLOBS: tuple[str, ...] = (
    "*.md",
    "pyproject.toml",  # description 欄位是繁中且會顯示在 PyPI 上
    "Makefile",  # 目標說明文字會出現在 `make help`
    ".gitignore",  # 進 git 也進 sdist；註解文字外人讀得到，見 B2
    "src/**/*.py",
    "src/**/*.yaml",
    "src/**/*.json",
    "src/**/*.md",
    "src/**/*.lark",  # 文法檔含大量繁中散文註解，是禁用字句可能無聲落地的地方
    "tests/**/*.py",
    "corpus/**/*.md",
    "corpus/**/*.sql",
    "corpus/**/*.4gl",
    ".github/**/*.yml",
    ".github/**/*.md",
)

# 每個 glob 各派一個「哨兵」檔案。走訪結果必須包含全部哨兵——
# 這比「總數大於某個門檻」強得多：某一條 glob 打錯字只會少掉一兩個檔案，
# 門檻式的斷言吃得下這種損失而毫無反應，哨兵式的不會。
#
# `.github/**/*.yml` 與 `.github/**/*.md` 的哨兵刻意各選一個不同深度的檔案：
# `ISSUE_TEMPLATE/bug_report.yml` 在 `.github/` 底下再一層子目錄，
# `PULL_REQUEST_TEMPLATE.md` 則直接在 `.github/` 底下——藉此同時驗證
# `REPO_ROOT.glob(".github/**/*.yml")` 這種樣式在兩種深度下都真的抓得到檔案。
CANARY_FILES: tuple[str, ...] = (
    "README.md",
    "pyproject.toml",
    "Makefile",
    ".gitignore",
    "src/erp_migrai/mapping/engine.py",
    "src/erp_migrai/mapping/odoo17_catalog.yaml",
    "src/erp_migrai/mapping/schema/mapping.schema.json",
    "src/erp_migrai/mapping/rules/README.md",
    "src/erp_migrai/inventory/genero.lark",
    "tests/test_docs.py",
    "corpus/case01_partner/README.md",
    "corpus/case01_partner/src/schema.sql",
    "corpus/case01_partner/src/cust_maintain.4gl",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/PULL_REQUEST_TEMPLATE.md",
)

# 與 Apache-2.0 衝突的四句話。Apache-2.0 明確允許商業使用，任何暗示相反
# 意思的字句都會讓使用者對授權產生誤解，屬於法律風險而不只是文案問題。
FORBIDDEN_PHRASES: tuple[str, ...] = (
    "不得商業使用",
    "不得用於營利",
    "不得用於 SaaS",
    "不得重新販售",
)

# 🔴 這個檔案自己必須把那四句寫成字面字串才能拿來搜尋，於是掃描器會掃到自己。
# 因此明確把自己排除，並在這裡寫明理由——而不是用「把字串拆成片段再組合」
# 這種寫法來躲過自己的掃描。那種寫法半年後沒人看得懂，還會讓下一個人以為
# 有什麼玄機。豁免的範圍嚴格限定為本檔案一個。
_SELF = Path(__file__).resolve()

README = REPO_ROOT / "README.md"
ROADMAP = REPO_ROOT / "ROADMAP.md"
SECURITY = REPO_ROOT / "SECURITY.md"
COMMERCIAL = REPO_ROOT / "COMMERCIAL.md"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"
CODE_OF_CONDUCT = REPO_ROOT / "CODE_OF_CONDUCT.md"

# README 裡 demo 輸出區塊的界標。用註解而非「第幾個 code block」來定位，
# 因為 README 的結構會隨時間變動，而界標不會被順手改掉。
_DEMO_START = "<!-- demo-output:start -->"
_DEMO_END = "<!-- demo-output:end -->"

# 首發的治理文件。缺任何一份，GitHub 的社群健康度檢查與人的第一印象都會扣分。
# 前六份直接沿用上方已定義的 Path 常數（`.name`），避免同一份檔名清單要
# 手動同步兩次；`LICENSE`／`NOTICE` 沒有對應的 Path 常數，直接寫字面值。
REQUIRED_DOCS: tuple[str, ...] = (
    README.name,
    ROADMAP.name,
    CONTRIBUTING.name,
    CODE_OF_CONDUCT.name,
    SECURITY.name,
    COMMERCIAL.name,
    "LICENSE",
    "NOTICE",
)


def iter_shipped_files() -> Iterator[Path]:
    """走訪所有會被散布或被外人讀到的檔案，依路徑排序以確保確定性。"""
    seen: set[Path] = set()
    for pattern in SHIPPED_GLOBS:
        for path in REPO_ROOT.glob(pattern):
            if path.is_file():
                seen.add(path.resolve())
    yield from sorted(seen)


# 本專案自己撰寫、需要對連結與子指令提及做內容檢查的 markdown 文件。
# 只涵蓋根目錄與 `corpus/**`、`src/**`——刻意不含 `.github/**/*.md`
# （PR／issue 模板，連結是在 GitHub 動態頁面情境下解析，見 B3／test_docs.py
# 對 issue 表單的另一套檢查），也不含 `_local/`、`out/`、`.superpowers/`、
# `.venv/`、`node_modules/`（不進 git，或不是我們自己撰寫的文件）。
_MARKDOWN_SCAN_PATTERNS: tuple[str, ...] = ("*.md", "corpus/**/*.md", "src/**/*.md")
_MARKDOWN_SCAN_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {"_local", "out", ".superpowers", ".venv", "node_modules"}
)


def iter_project_markdown_files() -> Iterator[Path]:
    """走訪本專案自己撰寫的所有 markdown 文件，依路徑排序以確保確定性。"""
    seen: set[Path] = set()
    for pattern in _MARKDOWN_SCAN_PATTERNS:
        for path in REPO_ROOT.glob(pattern):
            if not path.is_file():
                continue
            parent_parts = set(path.relative_to(REPO_ROOT).parts[:-1])
            if parent_parts & _MARKDOWN_SCAN_EXCLUDED_DIRS:
                continue
            seen.add(path.resolve())
    yield from sorted(seen)


def test_no_forbidden_license_phrases_in_shipped_files():
    """出貨檔案不得出現與 Apache-2.0 衝突的字句。"""
    offenders: list[str] = []
    for path in iter_shipped_files():
        if path == _SELF:
            continue  # 見上方 _SELF 的說明
        text = path.read_text(encoding="utf-8")
        for phrase in FORBIDDEN_PHRASES:
            if phrase in text:
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel} 含有禁用字句「{phrase}」")
    assert not offenders, "出貨檔案含有與 Apache-2.0 衝突的字句：\n" + "\n".join(offenders)


def test_forbidden_phrase_scanner_covers_every_declared_glob():
    """守衛：確認每一條 glob 都真的抓得到東西。

    沒有這一條，`SHIPPED_GLOBS` 裡任一條打錯字都會讓上面那條掃描永遠綠燈——
    一條掃不到任何東西的 glob，跟沒有那條 glob 是一樣的，而且沒有人會發現。

    用哨兵而非「總數大於 N」：打錯一條 corpus 的 glob 只會少掉一兩個檔案，
    門檻式斷言吃得下這種損失，哨兵式的不會。
    """
    walked = {p.relative_to(REPO_ROOT).as_posix() for p in iter_shipped_files()}
    missing = [name for name in CANARY_FILES if name not in walked]
    assert not missing, f"這些哨兵檔案沒有被走訪到，對應的 glob 可能打錯字：{missing}"


def test_makefile_demo_target_has_no_rm_rf():
    """`demo` 目標不得含有 `rm -rf`。

    `OUT` 是使用者可完全覆寫的變數，而 `demo` 又是 README 明確叫新手
    第一個跑的目標——兩者疊在一起，任何形式的遞迴刪除都是一把對著使用者
    自己資料的槍（例如 `make demo OUT=$HOME`）。scan 與 map 都會覆寫自己
    的輸出、`events.jsonl` 每次執行都會被截斷重寫，所以這個目標從一開始
    就不需要靠刪除來保證乾淨；移除比加防護更安全。
    """
    text = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    start = text.index("\ndemo:")
    end = text.index("\n\n", start)
    demo_target = text[start:end]
    assert "rm -rf" not in demo_target, "`demo` 目標不得含有 `rm -rf`（見本測試 docstring）"


def run_demo(out_dir: Path) -> str:
    """跑真正的 `make demo` 目標，回傳合併後的 stdout+stderr。

    刻意呼叫 make 而不是直接呼叫兩行 CLI：README 向使用者承諾的是
    `make demo`，測試就必須驗證那個承諾本身。
    """
    result = subprocess.run(
        ["make", "demo", f"OUT={out_dir}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"make demo 失敗（returncode={result.returncode}）\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout + result.stderr


def test_make_demo_produces_three_output_files(tmp_path):
    """`make demo` 必須產出 README 承諾的三個檔案。"""
    out = tmp_path / "demo"
    run_demo(out)
    for name in ("inventory.json", "mapping.json", "mapping.csv"):
        assert (out / name).is_file(), f"make demo 沒有產出 {name}"


def test_make_demo_reports_full_coverage_on_case01(tmp_path):
    """demo 對 case01 必須是 100% 覆蓋率——這是 README 敢承諾的前提。

    若哪天 catalog 或規則檔改動讓 case01 不再全對，README 的示範輸出就過期了，
    這條測試會先叫，而不是等使用者跑出不一樣的結果才發現。
    """
    output = run_demo(tmp_path / "demo")
    assert "覆蓋率 100%" in output
    assert "auto 7" in output


def existing_subcommands() -> set[str]:
    """從 `migrai --help` 動態取得現存子指令。

    刻意不寫死清單：寫死就變成第二份需要同步的真相，而這個模組存在的
    理由正是消滅那種東西。
    """
    result = subprocess.run(
        ["uv", "run", "migrai", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    # typer 的說明畫面把子指令列在 Commands 區塊，每行形如 "│ scan  …"
    commands: set[str] = set()
    for line in result.stdout.splitlines():
        match = re.match(r"^\s*│\s+([a-z][a-z0-9-]*)\s", line)
        if match:
            commands.add(match.group(1))
    assert commands, f"沒能從 migrai --help 解析出任何子指令：\n{result.stdout}"
    return commands


def readme_demo_block() -> list[str]:
    """抓出 README 裡 demo 輸出區塊中，程式實際會印出的那些行（保留原始縮排）。

    刻意不 `.strip()` 每一行：`test_readme_demo_output_matches_reality` 要把
    這些行接回一段連續文字，逐字比對是不是實際輸出的一段連續子字串——
    縮排（例如 `→` 開頭那幾行前面的兩個空格）本身就是宣稱的一部分，
    strip 掉會讓縮排錯誤永遠測不出來。只濾掉圍籬（```）、使用者輸入的
    指令行（`$ make demo`）；區塊中間的空行會保留，因為那也是實際輸出
    版面的一部分。頭尾的空行（圍籬本身留下的）才丟棄。
    """
    text = README.read_text(encoding="utf-8")
    assert _DEMO_START in text and _DEMO_END in text, (
        f"README 必須用 {_DEMO_START} / {_DEMO_END} 標出 demo 輸出區塊"
    )
    block = text.split(_DEMO_START, 1)[1].split(_DEMO_END, 1)[0]
    lines: list[str] = []
    for raw in block.splitlines():
        line = raw.rstrip("\n")
        if line.startswith("```") or line.lstrip().startswith("$"):
            continue
        lines.append(line)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    assert lines, "demo 輸出區塊裡沒有任何被宣稱的輸出行"
    return lines


def mentioned_subcommands(text: str) -> set[str]:
    """抓出一段文字裡以 `migrai <子指令>` 形式被提到的子指令。

    負向後查排除 `erp-migrai`：專案目錄與套件名都叫 erp-migrai，文件裡幾乎
    必然出現 `cd erp-migrai`，而 `\\s+` 會跨行——沒有這個後查，下一行的
    `make install` 會被當成「不存在的子指令 make」而誤報。
    """
    return set(re.findall(r"(?<![\w-])migrai\s+([a-z][a-z0-9-]*)\b", text))


def test_no_governance_doc_mentions_a_nonexistent_subcommand():
    """本專案自己撰寫的任何 `*.md` 文件都不得提到不存在的 `migrai` 子指令。

    這是整組測試的核心：首發只有 scan 與 map，`load`／`verify`／`ui`
    都還沒做。往任何治理文件寫上它們就是承諾一個 repo 裡不存在的能力。

    走訪範圍見 `iter_project_markdown_files()`（根目錄 + `corpus/**` +
    `src/**`）：這條全域約束原本只掃根目錄，`corpus/README.md`、
    `src/erp_migrai/mapping/rules/README.md` 這類子目錄文件寫出
    `migrai load` 這種例子時，沒有任何機器守衛會叫。一次寫成走訪本專案
    所有自撰 markdown 文件，之後新增的子目錄文件也自動涵蓋在內，不必
    再開新測試。
    """
    real = existing_subcommands()
    offenders: list[str] = []
    for doc in iter_project_markdown_files():
        text = doc.read_text(encoding="utf-8")
        bogus = sorted(mentioned_subcommands(text) - real)
        if bogus:
            rel = doc.relative_to(REPO_ROOT)
            offenders.append(f"{rel} 提到了不存在的子指令 {bogus}")
    assert not offenders, (
        f"現存的子指令只有 {sorted(real)}；以下文件承諾了尚未實作的階段：\n" + "\n".join(offenders)
    )


def test_readme_demo_output_matches_reality(tmp_path):
    """README 宣稱的 demo 輸出，必須是實際輸出裡逐字、依原順序出現的連續片段。

    比對方式是「README 區塊接回一段連續文字後，整段必須是實際輸出的子字串」，
    不是「宣稱的每一行分別是實際輸出的子字串」（舊實作）。舊實作等價於
    `missing = [line for line in readme_demo_block() if line not in actual]`：
    只驗證每一行「存在」，不驗證「順序」與「完整性」——從 README 區塊裡刪掉
    中間一行、或把兩行對調，每一行依然個別存在於實際輸出中，舊測試照樣綠燈，
    而 README 那句「actual, unedited output」其實已經不成立。改成整段連續子
    字串比對後，刪行或調換順序都會破壞連續性而讓比對失敗。

    這同時也讓 `readme_demo_block()` 保留原始縮排（不再 `.strip()`）派上用場：
    `→` 開頭那幾行前面的縮排本身也是連續文字比對的一部分，縮排錯誤現在也
    會被這條測試抓到。

    測試跑在 tmp 目錄下（`OUT` 是使用者可覆寫的變數），所以實際輸出裡的路徑
    與 README 展示的 `out/demo` 不同。把 tmp 路徑正規化回 `out/demo` 之後，
    README 就能逐字展示完整的實測輸出——包含使用者真正會看到的 `→ 路徑` 那幾行。
    """
    out = tmp_path / "demo"
    actual = run_demo(out).replace(str(out), "out/demo")
    block_text = "\n".join(readme_demo_block())
    assert block_text in actual, (
        "README 宣稱的 demo 輸出區塊，必須是實際輸出中依原順序、原樣（含縮排與"
        "空行）出現的連續片段：\n\nREADME 區塊：\n" + block_text + f"\n\n實際輸出：\n{actual}"
    )


def test_readme_is_bilingual():
    """README 必須同時服務英文讀者與繁中讀者，且英文段落必須排在繁中段落之前。

    `\\b(the|and|with)\\b` 這條英文偵測本身很弱——繁中段落裡一段 inline code
    或一個 URL 都可能滿足它，所以它只能證明「文件裡某處出現過這幾個英文字」，
    證明不了「有一段完整的英文版本」。「英文在前、繁中在後」是本專案的硬性
    排版約束（見 README 本身的段落順序），但先前完全沒有測試釘住這件事——
    把兩個語言段落對調，這條測試原本照樣綠燈。

    改用 README 自己的雙語標題當錨點：英文標題 `# ERP MIGRAI` 一定在檔案
    最前面，繁中標題 `# ERP MIGRAI（繁體中文）` 一定在後面的分隔線之後。
    這兩個錨點就是專案名稱本身，不像章節小標題那樣容易被改寫。
    """
    text = README.read_text(encoding="utf-8")
    assert re.search(r"[一-鿿]", text), "README 缺少繁體中文段落"
    assert re.search(r"\b(the|and|with)\b", text, re.IGNORECASE), "README 缺少英文段落"

    english_heading = "# ERP MIGRAI\n"
    chinese_heading = "# ERP MIGRAI（繁體中文）"
    english_pos = text.find(english_heading)
    chinese_pos = text.find(chinese_heading)
    assert english_pos != -1, f"README 找不到英文標題錨點 {english_heading!r}"
    assert chinese_pos != -1, f"README 找不到繁體中文標題錨點 {chinese_heading!r}"
    assert english_pos < chinese_pos, "README 必須英文段落排在繁體中文段落之前"


def test_readme_states_apache_allows_commercial_use():
    """授權說明必須正面陳述商業使用是允許的。

    Apache-2.0 允許商業使用；含糊其辭會讓使用者以為不行，
    那是禁用字句那條規則想防的同一件事的另一面。
    """
    text = README.read_text(encoding="utf-8")
    assert "商業使用完全允許" in text


def program_unit_kind_count() -> int:
    """從 `models.py` 的 `ProgramUnit.kind` 型別註解數出程式單元有幾種。

    來源是 `kind: str  # MAIN | FUNCTION | REPORT | GLOBALS` 這一行——
    掃描器實際辨識的程式單元種類數，不是 corpus 語料示範的種類數（golden
    inventory 裡目前只出現 GLOBALS／MAIN／FUNCTION 三種，沒有 REPORT；
    「掃描器認得幾種」與「語料示範了幾種」是兩件不同的事，這裡量的是前者）。
    """
    models_path = REPO_ROOT / "src" / "erp_migrai" / "inventory" / "models.py"
    text = models_path.read_text(encoding="utf-8")
    match = re.search(r"class ProgramUnit:.*?kind: str  # (.+)", text, re.DOTALL)
    assert match, "在 inventory/models.py 找不到 ProgramUnit.kind 的型別註解"
    kinds_line = match.group(1).splitlines()[0]
    kinds = [k.strip() for k in kinds_line.split("|") if k.strip()]
    assert kinds, f"無法從 ProgramUnit.kind 的註解解析出任何種類：{kinds_line!r}"
    return len(kinds)


def test_readme_capability_table_program_unit_count_matches_models():
    """README 能力表宣稱的程式單元種類數，必須釘在 `ProgramUnit.kind` 上。

    見最終審查 A2：先前寫死的「3 syntax patterns／三種語法型態」在 repo 裡
    查無來源——程式單元實際上有 `ProgramUnit.kind`（`inventory/models.py`）
    列出的種類數，內嵌 SQL 另有五種（`SqlStatement.kind`），corpus 語料
    只示範了其中一部分。這條測試只釘「掃描器認得幾種程式單元」這件事：
    程式改了種類數而 README 沒跟上，或 README 的數字沒有程式碼依據，這裡
    都要紅燈。
    """
    count = program_unit_kind_count()
    text = README.read_text(encoding="utf-8")
    assert f"{count} program-unit kinds" in text, (
        f"README 英文能力表格未宣稱 {count} program-unit kinds（見 ProgramUnit.kind）"
    )
    assert f"{count} 種程式單元型態" in text, (
        f"README 中文能力表格未宣稱 {count} 種程式單元型態（見 ProgramUnit.kind）"
    )


def test_roadmap_promises_no_dates():
    r"""ROADMAP 不得寫日期承諾。

    猜的日期就是另一種「宣稱一件沒被驗證的事」。路線圖要寫的是
    「這一項依賴什麼」，不是「這一項哪一季交付」。

    範圍限制：這條只擋日期樣式，不擋版本承諾（例如「1.0 版交付」「v2 會有
    Web 介面」）。版本樣式（形如 `\d+\.\d+`）沒有加進來是刻意的——它會誤中
    `Apache-2.0`、`Python 3.12` 這類不是承諾的字串，做出「工具說你錯了但
    其實沒錯」的另一種缺陷。版本承諾這一半目前沒有機器守衛，靠人審。
    """
    text = ROADMAP.read_text(encoding="utf-8")
    date_like = re.findall(r"20\d{2}|[Qq][1-4](?!\w)|第[一二三四1-4]\s*季", text)
    assert not date_like, f"ROADMAP 出現了日期承諾：{date_like}"


def test_roadmap_documents_the_load_stage():
    """ROADMAP 應說明規劃中的載入階段。

    「不得用 `migrai <子指令>` 的形式寫成好像能跑」那一半已經由
    `test_no_governance_doc_mentions_a_nonexistent_subcommand()`（走訪
    `iter_project_markdown_files()` 範圍內所有 markdown 文件）涵蓋，這裡不
    重複斷言，只留下 ROADMAP 特有的內容要求。
    """
    text = ROADMAP.read_text(encoding="utf-8")
    assert "載入" in text, "ROADMAP 應說明載入階段的規劃"


def test_roadmap_pins_the_claims_it_makes_about_this_repos_own_toolchain():
    """ROADMAP 對本 repo 自身工具鏈的具體宣稱，必須釘在真實存在的東西上。

    ROADMAP 提到 `pyproject.toml` 保留了 `integration` 這個 pytest 標記、
    `make test` 預設會跳過它，還點名了 `tests/test_docs.py` 裡
    `test_make_demo_*` 與 `test_readme_demo_output_matches_reality` 這幾個
    測試函式的名字，當作「demo 輸出逐字受測試守衛」這句話的佐證。這些宣稱
    在寫下的當下都是真的，但改個 marker 名字、或幫某個測試函式改名，會讓
    ROADMAP 無聲說謊而 `make test` 依然全綠——沒有任何既有守衛會叫。

    這裡不解析 pytest 的內部設定物件，直接對 `pyproject.toml` 的原始文字
    做字串比對：這條測試要釘的是「ROADMAP 提到的字面設定值確實存在於
    pyproject.toml」，用字串比對就足夠證明，不需要額外的解析複雜度。
    """
    roadmap_text = ROADMAP.read_text(encoding="utf-8")
    assert "integration" in roadmap_text, "ROADMAP 應提到 `integration` pytest 標記"

    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "integration: 需要 docker Odoo 的整合測試（make test 預設跳過）" in pyproject_text, (
        "pyproject.toml 的 `integration` marker 宣告與 ROADMAP 的描述不一致，或該宣告已被改名／移除"
    )
    assert "-m 'not integration'" in pyproject_text, (
        "pyproject.toml 的 addopts 不再預設跳過 `integration` 標記，"
        "與 ROADMAP『make test 預設會跳過它』的宣稱不符"
    )

    test_docs_text = _SELF.read_text(encoding="utf-8")
    named_tests = (
        "test_make_demo_produces_three_output_files",
        "test_make_demo_reports_full_coverage_on_case01",
        "test_readme_demo_output_matches_reality",
    )
    missing = [name for name in named_tests if f"def {name}(" not in test_docs_text]
    assert not missing, (
        f"ROADMAP 點名的測試函式在 tests/test_docs.py 裡已經不存在（可能被改名）：{missing}"
    )


def test_security_points_at_github_private_reporting():
    """安全回報必須指向真的存在的管道。

    一個不通的安全聯絡方式比沒有更糟——研究者找不到人就會直接公開揭露。
    """
    text = SECURITY.read_text(encoding="utf-8")
    assert "security/advisories/new" in text, "SECURITY.md 必須給出私下回報的實際連結"
    # 找的是「真的 email 位址」而不是「有沒有出現 @ 或 email 這個字」：
    # GitHub 帳號寫成 @cyodaa 是正常的，不該被誤判。
    emails = re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    assert not emails, f"依 P5-4，不公開 email 作為回報管道，改用 GitHub 私下回報：{emails}"


def test_commercial_has_no_price_and_points_at_discussions():
    """COMMERCIAL.md 只負責功能邊界與去哪裡問；價格會變，且文件測試管不到 repo 外的事實。

    價格樣式除了「幣別／單位標記緊接數字」（例如 `NT$1,000`、`每年 3`），
    也要抓「數字（含千分位逗號）緊接『元』」（例如「30,000 元」）與
    「中文數字緊接『元』」（例如「三萬元」）這兩種最自然的中文寫法——
    這兩種都不需要先出現 `NT$`／`每月`／`每年` 之類的標記字，原本的樣式
    抓不到。中文數字字元集刻意不含「單」，避免誤中「單元」「程式單元」
    這類含「元」但不是價格的詞。
    """
    text = COMMERCIAL.read_text(encoding="utf-8")
    assert "discussions" in text.lower(), "商業洽詢必須導向 GitHub Discussions"
    price_like = re.findall(
        r"(?:NT\$|US\$|\$|元/|每月|每年)\s*\d"
        r"|\d[\d,]*\s*元"
        r"|[一二三四五六七八九十百千萬億兩]+\s*元",
        text,
    )
    assert not price_like, f"COMMERCIAL.md 不應寫價格：{price_like}"


def test_readme_and_contributing_declare_corpus_is_synthetic():
    """D2 的誠實義務：語料是合成的，不是真實客戶資料。

    這句話在改寫文件時很容易被順手刪掉，而它是這個專案對外誠實的底線之一。
    """
    for doc in (README, CONTRIBUTING):
        text = doc.read_text(encoding="utf-8")
        assert "合成" in text, f"{doc.name} 必須聲明 corpus 是合成語料"


def test_contributing_documents_dco_signoff():
    """D9 選了 DCO；沒有簽署說明，貢獻者的 PR 會被 dco job 擋下卻不知道為什麼。"""
    text = CONTRIBUTING.read_text(encoding="utf-8")
    assert "Signed-off-by" in text
    assert "git commit -s" in text


def test_contributing_points_at_the_three_contribution_paths():
    """三個貢獻點都要有入口，否則貢獻門檻極低這件事只是自稱。

    貢獻點①原本用 `"corpus" in text` 當守衛，但 `corpus` 這個字也出現在
    檔案上方的合成語料聲明裡，就算把整個「新增一筆 corpus case」章節刪掉，
    那條斷言依然綠燈——形同沒有守衛。改用 `make update-golden`：那是
    貢獻點①的核心步驟，不會出現在檔案裡的其他段落。
    """
    text = CONTRIBUTING.read_text(encoding="utf-8")
    assert "make update-golden" in text, "缺少貢獻點①（新增 corpus case）"
    assert "parser" in text.lower(), "缺少貢獻點②（回報 parser 落差）"
    assert "rules" in text, "缺少貢獻點③（新增映射規則）"


def test_contributing_does_not_stale_promise_the_parser_gap_template():
    """條件式守衛：parser-gap-report issue 表單一旦建立，CONTRIBUTING 就不得再說『日後會補上』。

    CONTRIBUTING.md 目前寫「專用的 issue 表單日後會補上；在那之前，用一般
    Issue 描述以上內容即可」——這句話在今天（表單還不存在）是誠實的，但
    Task 6 會在 `.github/ISSUE_TEMPLATE/` 底下建立 parser-gap-report 表單，
    屆時這句話會變成假的，卻沒有任何既有測試會叫。

    這條測試是條件式的：`.github/ISSUE_TEMPLATE/` 底下還沒有 parser-gap-report
    表單時，條件不成立，測試自動通過（不是跳過）；表單一旦出現，就會強迫
    連動更新 CONTRIBUTING.md，而不是任由文件描述落後於實際基礎設施。
    """
    issue_template_dir = REPO_ROOT / ".github" / "ISSUE_TEMPLATE"
    if not issue_template_dir.is_dir():
        return
    template_exists = any("parser-gap-report" in p.name for p in issue_template_dir.glob("*"))
    if not template_exists:
        return
    text = CONTRIBUTING.read_text(encoding="utf-8")
    assert "日後會補上" not in text, (
        "parser-gap-report issue 表單已經存在，CONTRIBUTING.md 不得再說"
        "「專用的 issue 表單日後會補上」——請改成指向該表單。"
    )


def test_code_of_conduct_records_its_source():
    """行為準則必須註明來源，證明它是官方原文而非改寫版。

    `contributor-covenant.org` 與 `2.1` 這兩個字串也出現在官方正文自己的
    「來源」章節裡（`## 來源` / `## Attribution`），所以就算把檔頭整段
    出處區塊（取得日期、raw URL、CC BY 4.0 授權標示）整個刪掉，這兩條
    斷言依然綠燈。額外釘住只存在於檔頭的取得日期與授權標示，才是真的守住
    控制者裁決要求的可追溯資訊。
    """
    text = CODE_OF_CONDUCT.read_text(encoding="utf-8")
    assert "contributor-covenant.org" in text
    assert "2.1" in text
    assert "2026-07-28" in text, "檔頭必須記錄原文的取得日期"
    assert "CC BY 4.0" in text, "檔頭必須標示原文的授權方式"


# ---- T4：文件存在性與相對連結（八份到齊了才驗得動）----


def test_all_required_governance_docs_exist_and_are_not_empty():
    missing = [name for name in REQUIRED_DOCS if not (REPO_ROOT / name).is_file()]
    assert not missing, f"缺少治理文件：{missing}"
    thin = [
        name
        for name in REQUIRED_DOCS
        if len((REPO_ROOT / name).read_text(encoding="utf-8").strip()) < 200
    ]
    assert not thin, f"這些文件過短，可能只是佔位：{thin}"


def test_markdown_relative_links_resolve():
    """壞掉的相對連結在 GitHub 上是 404，是最廉價的失信。

    範圍見 `iter_project_markdown_files()`（根目錄 + `corpus/**` +
    `src/**`），且只認得 inline 形式的 `](path)` 連結；reference-style
    連結（`[text][ref]` 搭配另起一行的 `[ref]: path`）與 HTML 的
    `<a href>` 都不在掃描範圍內。目前掃描範圍內的相對連結全部是 inline
    形式且都通，所以沒有假陰性，但這條測試不保證涵蓋上述形式。

    解析基準是**該檔案所在目錄**，不是 repo 根目錄——GitHub 渲染 markdown
    時，相對連結就是相對於該檔案自己的路徑解析。`corpus/README.md` 裡的
    `../.github/...` 這種上溯路徑，只有用檔案自己的目錄當基準才能正確
    判斷是否存在；一律拿 repo 根目錄當基準會把子目錄文件裡每一條相對
    連結都判成錯的。
    """
    broken: list[str] = []
    for doc in iter_project_markdown_files():
        text = doc.read_text(encoding="utf-8")
        rel_doc = doc.relative_to(REPO_ROOT)
        for target in re.findall(r"\]\(([^)]+)\)", text):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            if not (doc.parent / path_part).exists():
                broken.append(f"{rel_doc} → {target}")
    assert not broken, "相對連結指向不存在的檔案：\n" + "\n".join(broken)


# ---- T6：`.github/` 的 issue 表單、PR 模板、dependabot ----


def test_all_github_yaml_files_are_valid():
    """`.github/` 底下的 YAML 必須合法，且不得有重複 key。

    workflow 與 issue 表單的語法錯誤推上去才會發現；用既有的嚴格 loader
    在本機就擋下來。重複 key 尤其危險——PyYAML 預設是後蓋前，
    複製貼上一個欄位區塊忘記改名，前一段會無聲消失。
    """
    yaml_files = sorted(set(GITHUB_DIR.rglob("*.yml")) | set(GITHUB_DIR.rglob("*.yaml")))
    assert yaml_files, ".github/ 底下找不到任何 YAML，路徑可能有誤"
    for path in yaml_files:
        rel = path.relative_to(REPO_ROOT)
        loaded = safe_load_strict(path.read_text(encoding="utf-8"), str(rel))
        assert loaded is not None, f"{rel} 是空的"


def test_issue_forms_have_required_structure():
    """GitHub Issue Form 需要 name / description / body 三個頂層欄位。

    缺了 GitHub 不會渲染成表單，而是靜靜地退回自由格式——
    這正是「悄悄退化」而沒有人會發現的那種失敗。
    """
    forms = sorted((GITHUB_DIR / "ISSUE_TEMPLATE").glob("*.yml"))
    forms = [p for p in forms if p.name != "config.yml"]
    assert len(forms) == 3, f"預期三個 issue 表單，實際 {[p.name for p in forms]}"
    for path in forms:
        rel = path.relative_to(REPO_ROOT)
        data = safe_load_strict(path.read_text(encoding="utf-8"), str(rel))
        for key in ("name", "description", "body"):
            assert key in data, f"{rel} 缺少頂層欄位 `{key}`"
        assert isinstance(data["body"], list) and data["body"], f"{rel} 的 body 必須是非空清單"


# GitHub issue form body 區塊的合法 type。這不是完整的 GitHub 規格——
# 只列本 repo 實際用到的型別（YAGNI），規格若擴充需要新型別，屆時再補。
_VALID_BLOCK_TYPES = frozenset({"markdown", "input", "textarea", "checkboxes", "dropdown"})

# 各型別要求要有 attributes.label（markdown 例外，它要求的是 attributes.value）。
_TYPES_REQUIRING_LABEL = frozenset({"input", "textarea", "dropdown", "checkboxes"})

# 要求 attributes.options 為非空清單的型別。
_TYPES_REQUIRING_OPTIONS = frozenset({"dropdown", "checkboxes"})


def test_issue_form_body_blocks_are_individually_valid():
    """驗每個 body 區塊本身的內容，不只驗 body 是不是非空清單。

    GitHub 遇到不合法的表單（型別打錯字、少了必要屬性、id 重複）不會報錯，
    而是靜靜地整份退回自由格式——本機沒有任何東西會叫。例如把
    `type: textarea` 打成 `type: text_area`，或某個 checkboxes 區塊漏了
    `attributes.options`，`test_issue_forms_have_required_structure` 那條
    「body 是非空 list」的檢查完全擋不住，必須逐區塊檢查型別與必要屬性。
    """
    forms = sorted((GITHUB_DIR / "ISSUE_TEMPLATE").glob("*.yml"))
    forms = [p for p in forms if p.name != "config.yml"]
    for path in forms:
        rel = path.relative_to(REPO_ROOT)
        data = safe_load_strict(path.read_text(encoding="utf-8"), str(rel))
        seen_ids: dict[str, int] = {}
        for i, block in enumerate(data["body"]):
            where = f"{rel} 的第 {i} 個 body 區塊"
            assert isinstance(block, dict), f"{where} 不是 mapping：{block!r}"
            block_type = block.get("type")
            assert block_type, f"{where} 缺少 `type`"
            assert block_type in _VALID_BLOCK_TYPES, (
                f"{where} 的 type `{block_type}` 不是合法值，"
                f"必須是 {sorted(_VALID_BLOCK_TYPES)} 之一"
            )
            attributes = block.get("attributes", {})
            if block_type == "markdown":
                assert attributes.get("value"), f"{where}（markdown）缺少 attributes.value"
            if block_type in _TYPES_REQUIRING_LABEL:
                assert attributes.get("label"), f"{where}（{block_type}）缺少 attributes.label"
            if block_type in _TYPES_REQUIRING_OPTIONS:
                options = attributes.get("options")
                assert options, f"{where}（{block_type}）缺少非空的 attributes.options"
                if block_type == "checkboxes":
                    for j, option in enumerate(options):
                        assert isinstance(option, dict) and option.get("label"), (
                            f"{where}（checkboxes）的第 {j} 個 option 缺少 label：{option!r}"
                        )
            block_id = block.get("id")
            if block_id is not None:
                assert block_id not in seen_ids, (
                    f"{where} 的 id `{block_id}` 與第 {seen_ids.get(block_id)} 個區塊重複"
                    "——重複 id 會讓 GitHub 拒絕渲染整份表單"
                )
                seen_ids[block_id] = i


def test_sensitive_contribution_forms_require_confirmation_checkbox():
    """貢獻點①②會讓人貼出自己環境裡的東西，必須有一個必勾的確認框。

    沒有這個框，第一個熱心的貢獻者可能就把客戶的專有邏輯貼上公開 issue。
    """
    for name in ("parser-gap-report.yml", "new_corpus_case.yml"):
        path = GITHUB_DIR / "ISSUE_TEMPLATE" / name
        data = safe_load_strict(path.read_text(encoding="utf-8"), name)
        required_checkboxes = [
            option
            for block in data["body"]
            if block.get("type") == "checkboxes"
            for option in block.get("attributes", {}).get("options", [])
            if option.get("required") is True
        ]
        assert required_checkboxes, f"{name} 缺少必勾的確認框"
