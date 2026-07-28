"""組裝 Inventory、寫出 inventory.json、依 JSON Schema 驗證。

scan 的失敗語意：只有「根本無法開始」才算失敗（E-1001，致命）。
解析不了的語法一律是 E-1002，會進不支援清單但不影響 exit code
——不支援清單是 scan 的產出，不是 scan 的失敗。
"""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from erp_migrai.events import E_1001, E_1002, EventLog, Severity
from erp_migrai.inventory.blocks import split_blocks
from erp_migrai.inventory.ddl import extract_tables
from erp_migrai.inventory.models import (
    SCHEMA_VERSION,
    Inventory,
    ProgramUnit,
    Table,
    UnsupportedSyntax,
    snippet,
)
from erp_migrai.inventory.parser import parse_block

SYNTHETIC_MARKER = "SYNTHETIC"
SCHEMA_PATH = Path(__file__).parent / "schema" / "inventory.schema.json"

_SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
_VALIDATOR = Draft202012Validator(_SCHEMA)

# 讀取失敗時只給一份「原始位元組的失真預覽」，長度上限與 snippet() 的預設值一致
# 即可——這裡只是給人看個大概，不需要跟原始碼片段的長度限制脫鉤。
_DECODE_PREVIEW_BYTES = 200


class ScanFailed(Exception):
    """scan 無法開始或無法完成。對應 E-1001。"""


# --- C1（branch review）：副檔名／檔名大小寫不敏感地發現來源檔案 ----------------
#
# 舊版用 `src_dir.glob("*.4gl")`（大小寫敏感）與 `src_dir / "schema.sql"`
# （透過 `Path.is_file()` 委派給 `os.stat`，是否大小寫敏感取決於檔案系統）
# 這兩種不同、且互相不一致的判準找檔案。真實 T100 原始碼常來自 Windows
# 工作站，大寫副檔名（`.4GL`）稀鬆平常；同一份輸入在大小寫不敏感的檔案系統
# （macOS APFS 預設）與大小寫敏感的檔案系統（Linux）上會找到不同的檔案，
# 讓 inventory.json 變得依賴平台，golden 測試因此失去意義。
#
# 修正做法：只掃描目錄一次（`iterdir()`），用 `.lower()` 判斷副檔名／檔名，
# 兩個平台的行為就會一致；同時把「目錄裡沒被消化的檔案」明確列舉出來並回報，
# 讓「我們忽略了某個檔案」這件事在結構上不可能再悄悄發生——這是比「找到
# 大小寫變化的副檔名」更重要的那一半修正。


def _discover_source_files(
    src_dir: Path,
) -> tuple[list[Path], Path | None, list[UnsupportedSyntax]]:
    """掃描 `src_dir`（不遞迴子目錄）一次，回傳 `(程式檔清單, schema 檔案或
    None, 未消化檔案的 UnsupportedSyntax 清單)`。

    程式檔：副檔名（不分大小寫）為 `.4gl` 的一般檔案，依實際檔名排序，
    全部視為消化對象（多個程式檔本來就是正常情形，沒有互斥可言）。

    schema 檔案：檔名（不分大小寫）為 `schema.sql` 的一般檔案。正常情況下
    只有一個；萬一同一個大小寫敏感的檔案系統上同時存在 `schema.sql` 與
    `SCHEMA.SQL` 這種相異大小寫的重複檔案，依排序取第一個當作 schema，
    其餘的透過下面的「未消化」機制回報，而不是悄悄消失。

    未消化：目錄裡除了上述兩類、以及 `SYNTHETIC` 標記檔之外的每一個一般
    檔案，都記一筆 `UnsupportedSyntax`（`line=1`，`snippet` 是檔名），理由
    點名這個檔案沒有被本階段辨識為它認得的來源、並列出認得的兩種副檔名／
    檔名。子目錄不是一般檔案（`is_file()` 為 False），因此天然地不會被
    遞迴進去，也不會被誤報成未消化。
    """
    all_files = sorted((p for p in src_dir.iterdir() if p.is_file()), key=lambda p: p.name)

    fgl_files = [p for p in all_files if p.suffix.lower() == ".4gl"]
    schema_candidates = [p for p in all_files if p.name.lower() == "schema.sql"]
    schema_file = schema_candidates[0] if schema_candidates else None

    consumed_names = {p.name for p in fgl_files}
    if schema_file is not None:
        consumed_names.add(schema_file.name)

    unsupported: list[UnsupportedSyntax] = []
    for path in all_files:
        if path.name in consumed_names or path.name == SYNTHETIC_MARKER:
            continue
        unsupported.append(
            UnsupportedSyntax(
                file=path.name,
                line=1,
                snippet=snippet(path.name),
                reason=(
                    "這個檔案沒有被本階段辨識為認得的來源：本階段只處理副檔名"
                    "（不分大小寫）為 .4gl 的程式檔，以及檔名（不分大小寫）為"
                    " schema.sql 的資料庫結構檔"
                ),
            )
        )

    return fgl_files, schema_file, unsupported


def validate_inventory(data: dict) -> None:
    """不符合 inventory.schema.json 時拋出 jsonschema.ValidationError。"""
    _VALIDATOR.validate(data)


def _read_source_text(path: Path) -> tuple[str | None, UnsupportedSyntax | None]:
    """讀取一份來源檔案的文字內容。

    這是整個 inventory pipeline 裡**唯一**處理 BOM 的地方。真實 T100 原始碼
    來自 Windows 工作站，`.4gl`／`schema.sql` 常常帶 UTF-8 BOM；BOM 落在檔案
    第一行，會讓那一行對每一種結構樣式（`DATABASE`、區塊起始等）的比對都失敗，
    使檔案的第一個構造被誤報成「無法辨識」——明明原始碼本身完全合法。
    `encoding="utf-8-sig"` 在有 BOM 時會剝掉它、沒有 BOM 時行為與 "utf-8"
    完全相同，一個機制就同時涵蓋兩種情形。`blocks.py`／`masking.py` 刻意
    **不**處理 BOM：剝 BOM 這件事只能發生在這一處，否則同一份原始碼可能被剝
    兩次，或者換一個消費者（例如未來直接呼叫 `split_blocks` 的工具）忘記剝、
    在別處留下同樣的假異常。

    真實 T100 原始碼也可能是 Big5 等舊有編碼（並非合法的 UTF-8 位元組序列），
    此時 `utf-8-sig` 會拋出 `UnicodeDecodeError`。scan「絕不中止」的原則同樣
    適用在檔案層級：這裡把解碼失敗轉成一筆 `UnsupportedSyntax`（呼叫端會發
    對應的 E-1002 事件），讓損失可見，而不是任由例外逃出整個 scan，也不會
    因為單一壞檔就放棄目錄裡其他能正常解碼的檔案。

    除了解碼失敗，讀檔本身也可能因為非解碼原因失敗：檔案在 `glob()` 之後、
    讀取之前被刪除或替換（例如同時有另一個程序在重新產生 `.4gl`）、`is_file()`
    檢查通過之後權限被改掉、符號連結形成迴圈、或者路徑其實是一個目錄。這些
    都會讓 `read_text()` 拋出 `OSError`（`UnicodeDecodeError` 不是它的子類別，
    是獨立的例外階層），若不接住一樣會逃出整個 `scan()`，違反「scan 絕不
    中止」的不變量。這裡用完全相同的方式處理：轉成一筆 `UnsupportedSyntax`
    並繼續處理其他檔案，理由字串用「I/O 錯誤」與「解碼失敗」明確區分（避免
    使用者誤以為 Big5 壞檔跟檔案消失是同一種問題），並點名實際的例外型別方便
    追查。注意 `OSError` 情況下無法安全嘗試 `path.read_bytes()` 做預覽——很多
    造成 `OSError` 的原因（權限、目錄、檔案已消失）本身就會讓補讀位元組同樣
    失敗，因此這裡不附加原始碼片段預覽，只留空字串。

    回傳 `(文字, None)` 表示成功；`(None, UnsupportedSyntax)` 表示讀取失敗
    （解碼失敗或 I/O 錯誤），呼叫端應跳過這個檔案的內容擷取，但仍把檔名列進
    `source_files`——檔案確實存在，只是這次掃描讀不懂或讀不到它的內容，這
    兩件事不該混為一談。
    """
    try:
        return path.read_text(encoding="utf-8-sig"), None
    except UnicodeDecodeError as exc:
        preview = path.read_bytes()[:_DECODE_PREVIEW_BYTES].decode("utf-8", errors="replace")
        entry = UnsupportedSyntax(
            file=path.name,
            line=1,
            snippet=snippet(preview),
            reason=(
                "檔案無法以 UTF-8（含 BOM）解碼，可能是 Big5 或其他舊有編碼："
                f"{type(exc).__name__}：{snippet(str(exc))}；"
                "此檔案本次掃描已跳過內容擷取"
            ),
        )
        return None, entry
    except OSError as exc:
        entry = UnsupportedSyntax(
            file=path.name,
            line=1,
            snippet="",
            reason=(
                "讀取檔案時發生 I/O 錯誤（並非解碼問題），可能是檔案在掃描過程中"
                f"消失、權限被變更、或路徑其實不是一般檔案：{type(exc).__name__}："
                f"{snippet(str(exc))}；此檔案本次掃描已跳過內容擷取"
            ),
        )
        return None, entry


def scan(src_dir: Path, *, event_log: EventLog) -> Inventory:
    """掃描來源目錄，回傳 Inventory。

    Raises:
        ScanFailed: 目錄不存在、路徑存在但不是目錄，或目錄裡沒有任何 .4gl 檔。
    """
    event_log.emit(
        "INVENTORY_SCAN_STARTED",
        message=f"開始掃描 {src_dir.name}",
        details={"src": src_dir.name},
    )

    # 「不存在」與「存在但不是目錄」（例如 --src 指到一個一般檔案）是兩種不同的
    # 使用者錯誤，訊息應該分開講清楚是哪一種；但兩者對 scan 而言都是「根本無法
    # 開始」，因此都維持 E-1001、都是致命錯誤，錯誤碼與 exit code 契約不變。
    if not src_dir.exists():
        message = f"來源目錄不存在：{src_dir}"
        event_log.emit("INVENTORY_SCAN_FAILED", code=E_1001, message=message)
        raise ScanFailed(message)

    if not src_dir.is_dir():
        message = f"來源路徑存在，但不是目錄：{src_dir}"
        event_log.emit("INVENTORY_SCAN_FAILED", code=E_1001, message=message)
        raise ScanFailed(message)

    # 只看檔案系統裡有沒有 .4gl 檔（不分大小寫）——這是「根本無法開始」的唯一
    # 判準。單一檔案讀不懂（BOM 以外的編碼問題）是內容層級的落差，屬於
    # E-1002，不影響這裡。
    fgl_files, schema_file, unconsumed = _discover_source_files(src_dir)
    if not fgl_files:
        message = f"來源目錄裡沒有任何 .4gl 檔：{src_dir}"
        event_log.emit("INVENTORY_SCAN_FAILED", code=E_1001, message=message)
        raise ScanFailed(message)

    tables: list[Table] = []
    programs: list[ProgramUnit] = []
    unsupported: list[UnsupportedSyntax] = list(unconsumed)
    source_files: list[str] = [p.name for p in fgl_files]
    database: str | None = None
    database_source: str | None = None
    # 除了 database（勝出者）之外，其他每個曾經被宣告過的相異名稱都收在這裡
    # （用 set 天然去重），最後排序後寫進 Inventory.database_conflicts——這是
    # inventory.json 本身唯一能表達「還有別的宣告存在」這件事的地方，events.jsonl
    # 的 INVENTORY_DATABASE_CONFLICT 事件則額外記錄「哪個名稱來自哪個檔案」。
    other_databases: set[str] = set()

    if schema_file is not None:
        source_files.append(schema_file.name)
        text, decode_error = _read_source_text(schema_file)
        if decode_error is not None:
            unsupported.append(decode_error)
        else:
            ddl_tables, ddl_unsupported = extract_tables(text, source_file=schema_file.name)
            tables.extend(ddl_tables)
            unsupported.extend(ddl_unsupported)

    for path in fgl_files:
        text, decode_error = _read_source_text(path)
        if decode_error is not None:
            unsupported.append(decode_error)
            continue

        split = split_blocks(text, source_file=path.name)

        if split.database is not None:
            if database is None:
                database = split.database
                database_source = path.name
            elif split.database != database:
                # inventory.json 的 database 欄位只能表達單一名稱（models.py 不在
                # 本任務可修改範圍內），兩個檔案宣告不同名稱時勢必有一個「輸」。
                # 「輸」不代表可以悄悄消失：這裡發一個沒有錯誤碼的事件（衝突不是
                # 語法落差，用 E-1002 會誤導閱讀 events.jsonl 的人），把「哪個
                # 名稱被採用、哪個被捨棄」如實記下來，讓損失至少在稽核紀錄裡
                # 是可見的，即使 inventory.json 本身受限於 schema 只能留一個值。
                event_log.emit(
                    "INVENTORY_DATABASE_CONFLICT",
                    severity=Severity.WARN,
                    message=(
                        f"{path.name} 宣告 DATABASE {split.database}，但 "
                        f"{database_source} 已先宣告 DATABASE {database}；"
                        f"inventory.json 的 database 欄位只能表達單一名稱，"
                        f"本次採用先出現（依檔名排序）的宣告，{path.name} "
                        "的宣告不會出現在 inventory.json 裡"
                    ),
                    details={
                        "file": path.name,
                        "declared_database": split.database,
                        "kept_database": database,
                        "kept_from_file": database_source,
                    },
                )
                other_databases.add(split.database)

        unsupported.extend(split.unsupported)
        for block in split.blocks:
            unit = parse_block(block)
            programs.append(unit)
            unsupported.extend(unit.unsupported)

    # 每一筆不支援語法發一個 E-1002 事件。逐筆而非彙總，是為了讓落差回報
    # 能直接以 events.jsonl 統計——這批資料未來會沉澱成語法落差圖譜。
    for item in unsupported:
        event_log.emit(
            "INVENTORY_PARSE_ERROR",
            code=E_1002,
            message=f"{item.file}:{item.line} {item.reason}",
            details={
                "file": item.file,
                "line": item.line,
                "reason": item.reason,
                "snippet": item.snippet,
            },
        )

    # (file, line) 不是 total order：同一個檔案同一行可能同時被兩個不同的
    # producer 各記一筆（例如同一行既觸發「單引號字串未終止」又觸發「非
    # CREATE TABLE 語句」）。單靠 (file, line) 排序時，平手的兩筆順序其實是
    # 靠 Python 穩定排序＋今天剛好如此的產生順序在撐，這個假設沒有寫在任何
    # 地方，未來任何一個 producer 調整內部順序都可能悄悄打破輸出的確定性。
    # 把 reason、snippet 也納入排序鍵，讓順序只取決於這筆紀錄自身的內容，
    # 不再依賴任何呼叫端隱含的產生順序。
    sorted_unsupported = sorted(unsupported, key=lambda u: (u.file, u.line, u.reason, u.snippet))

    inventory = Inventory(
        synthetic=(src_dir / SYNTHETIC_MARKER).is_file(),
        schema_version=SCHEMA_VERSION,
        database=database,
        source_files=sorted(source_files),
        tables=tables,
        programs=programs,
        unsupported_syntax=sorted_unsupported,
        database_conflicts=sorted(other_databases),
    )

    event_log.emit(
        "INVENTORY_SCAN_COMPLETED",
        message=(
            f"掃描完成：{len(inventory.tables)} 個表、"
            f"{len(inventory.programs)} 個程式單元、"
            f"{len(inventory.unsupported_syntax)} 處不支援語法"
        ),
        details={
            "tables": len(inventory.tables),
            "programs": len(inventory.programs),
            "unsupported": len(inventory.unsupported_syntax),
        },
    )
    return inventory


def write_inventory(inventory: Inventory, out_dir: Path) -> Path:
    """寫出 inventory.json 並回傳其路徑。輸出是確定性的。"""
    payload_dict = inventory.to_dict()
    # 寫檔前先依 JSON Schema 驗證：inventory.json 是對外（含商業版 consumer）
    # 承諾的公開格式，這裡驗證失敗代表程式碼實際產出的形狀已經偏離自己宣告的
    # schema——這是程式本身的錯誤，不是掃描層級「解析不了某段語法」的落差，
    # 因此讓 jsonschema.ValidationError 原樣往外傳，不吞掉、不轉成
    # UnsupportedSyntax，也不新增任何錯誤碼去包裝它。
    validate_inventory(payload_dict)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "inventory.json"
    payload = json.dumps(payload_dict, ensure_ascii=False, indent=2) + "\n"
    path.write_text(payload, encoding="utf-8")
    return path
