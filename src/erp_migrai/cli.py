"""migrai 的命令列介面。

CLI 與 Web UI 是兩個平等的「面板」，四段管線才是「水管」。
本模組只負責參數解析、輸出摘要與決定 exit code——
任何業務邏輯都不能只存在於這裡，否則 Web UI 會與 CLI 行為分岔。

exit code 契約：
    0  該階段完成（即使有不支援語法或 WARN）
    1  該階段出現致命錯誤（見 events.FATAL_CODES），或 --strict 下出現 ERROR；
       或產出目錄無法寫入（環境問題，不是掃描發現）
    2  用法錯誤（由 Typer 產生）
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from collections import Counter
from pathlib import Path

import typer

from erp_migrai import __version__
from erp_migrai.events import EventLog, Stage
from erp_migrai.inventory.extractor import ScanFailed, write_inventory
from erp_migrai.inventory.extractor import scan as run_scan
from erp_migrai.mapping.catalog import CatalogError
from erp_migrai.mapping.engine import MapFailed
from erp_migrai.mapping.pipeline import InventoryReadError, InventoryValidationError, run_map
from erp_migrai.mapping.rules_loader import RuleError
from erp_migrai.mapping.writer import MappingSchemaError, MappingSerializationError
from erp_migrai.pipeline.loader import LogEntrySchemaError, run_load
from erp_migrai.pipeline.odoo_client import OdooClient
from erp_migrai.pipeline.source import SourceError, SqliteSourceReader

app = typer.Typer(
    add_completion=False,
    help="ERP MIGRAI — 鼎新 T100（Genero 4GL）遷移至 Odoo 17 CE 的開源核心工具",
)

EVENTS_FILENAME = "events.jsonl"

# P3-2：密碼只收環境變數或 --password-stdin，不實作 --password 旗標——
# 密碼進 argv 會被同機器上任何使用者用 `ps` 看到，也會落進 shell 歷史。
PASSWORD_ENV_VAR = "MIGRAI_ODOO_PASSWORD"


def _fresh_event_log(out_dir: Path) -> EventLog:
    """每次執行都從乾淨的 events.jsonl 開始，避免多次執行互相污染。

    立刻把檔案 touch 出來（而不是等第一筆事件才建立）：`map` 階段在完全
    乾淨（無 conflicts、覆蓋率達標）時可能一筆事件都不會發，若不在這裡先
    建好空檔案，`events.jsonl` 就會違背「每次執行都會寫入」的介面承諾
    （scan 不受影響——它一律至少會發一筆 INVENTORY_SCAN_STARTED 事件）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / EVENTS_FILENAME
    path.unlink(missing_ok=True)
    path.touch()
    return EventLog(path=path, run_id=str(uuid.uuid4()))


@app.command()
def scan(
    src: Path = typer.Option(  # noqa: B008
        ...,
        "--src",
        help="來源目錄，需含 .4gl 檔（schema.sql 若存在會一併解析）",
    ),
    out: Path = typer.Option(  # noqa: B008
        Path("out"),
        "--out",
        help="產出目錄，會寫入 inventory.json 與 events.jsonl",
    ),
    strict: bool = typer.Option(  # noqa: B008
        False,
        "--strict",
        help="把不支援的語法視為失敗（exit 1）。預設不支援語法只是產出的一部分。",
    ),
) -> None:
    """盤點來源程式與資料結構，產出 inventory.json。"""
    # 產出目錄本身若無法建立或寫入（例如 --out 指到一個已存在的一般檔案、
    # 或者權限不足），是環境問題而不是掃描發現：庫函式讓 OSError 逃出是對的
    # （沒有合理的預設行為可以代替使用者決定），但使用者不該看到 traceback，
    # 這裡把它轉成一則指名路徑與原因的中文訊息、以 exit 1 結束。
    try:
        event_log = _fresh_event_log(out)
    except OSError as exc:
        typer.echo(f"✗ 無法寫入產出目錄 {out}：{exc}")
        raise typer.Exit(code=1) from exc

    try:
        try:
            inventory = run_scan(src, event_log=event_log)
        except ScanFailed as exc:
            typer.echo(f"✗ [E-1001] 掃描失敗：{exc}")
            raise typer.Exit(code=1) from exc
    except OSError as exc:
        typer.echo(f"✗ 無法寫入產出目錄 {out}：{exc}")
        raise typer.Exit(code=1) from exc

    try:
        path = write_inventory(inventory, out)
    except OSError as exc:
        typer.echo(f"✗ 無法寫入產出目錄 {out}：{exc}")
        raise typer.Exit(code=1) from exc

    typer.echo(
        f"✓ 掃描完成："
        f"{len(inventory.tables)} 個表、"
        f"{len(inventory.programs)} 個程式單元、"
        f"{len(inventory.unsupported_syntax)} 處不支援語法"
    )
    typer.echo(f"  → {path}")
    typer.echo(f"  → {event_log.path}")

    if inventory.unsupported_syntax:
        typer.echo("\n不支援的語法（這是盤點結果的一部分，不是錯誤）：")
        for item in inventory.unsupported_syntax[:10]:
            typer.echo(f"  [E-1002] {item.file}:{item.line} {item.reason}")
        remaining = len(inventory.unsupported_syntax) - 10
        if remaining > 0:
            typer.echo(f"  …另有 {remaining} 處，完整清單見 inventory.json")
        typer.echo(
            "\n若你手上有真實 T100 環境，歡迎回報落差："
            "請開 parser-gap-report issue，只需貼出失敗片段與錯誤訊息，不需要附上你的原始碼。"
        )

    if event_log.has_fatal(Stage.SCAN, strict=strict):
        raise typer.Exit(code=1)


@app.command(name="map")
def map_command(
    inventory: Path = typer.Option(  # noqa: B008
        ...,
        "--inventory",
        help="scan 產出的 inventory.json 路徑",
    ),
    out: Path = typer.Option(  # noqa: B008
        Path("out"),
        "--out",
        help="產出目錄，會寫入 mapping.json、mapping.csv 與 events.jsonl",
    ),
    rules: Path | None = typer.Option(  # noqa: B008
        None,
        "--rules",
        help="規則目錄，預設用套件內建的 rules/",
    ),
    catalog: Path | None = typer.Option(  # noqa: B008
        None,
        "--catalog",
        help="Odoo catalog 檔案，預設用套件內建的 odoo17_catalog.yaml",
    ),
    strict: bool = typer.Option(  # noqa: B008
        False,
        "--strict",
        help="把 ERROR／CRITICAL 等級的事件也視為失敗（exit 1）；"
        "WARN 等級（例如覆蓋率偏低的 E-2002）不受影響，仍視為完成。",
    ),
) -> None:
    """把 inventory.json 對應到 Odoo 17 目標欄位，產出 mapping.json。"""
    # 跟 scan 一樣：產出目錄本身無法建立或寫入是環境問題，不是映射結果，
    # 這裡把 OSError 轉成點名路徑與原因的中文訊息、以 exit 1 結束，絕不讓
    # traceback 洩漏給使用者。
    try:
        event_log = _fresh_event_log(out)
    except OSError as exc:
        typer.echo(f"✗ 無法寫入產出目錄 {out}：{exc}")
        raise typer.Exit(code=1) from exc

    # 四段管線是水管，CLI 只是面板：讀檔→驗證→載入 catalog／rules→
    # build_mapping→寫出 mapping.json／mapping.csv 這七步，連同每一步該接住
    # 哪種例外，都住在 mapping.pipeline.run_map（precedent：inventory.
    # extractor.scan()）。這裡只把每一種例外轉成清楚的中文訊息與 exit 1，
    # 絕不讓 traceback 洩漏給使用者。
    try:
        result = run_map(inventory, out, rules_dir=rules, catalog_path=catalog, event_log=event_log)
    except InventoryReadError as exc:
        typer.echo(f"✗ {exc}")
        raise typer.Exit(code=1) from exc
    except json.JSONDecodeError as exc:
        typer.echo(f"✗ --inventory {inventory} 不是合法的 JSON：{exc}")
        raise typer.Exit(code=1) from exc
    except InventoryValidationError as exc:
        typer.echo(f"✗ {exc}")
        raise typer.Exit(code=1) from exc
    except CatalogError as exc:
        typer.echo(f"✗ 無法載入 catalog：{exc}")
        raise typer.Exit(code=1) from exc
    except RuleError as exc:
        typer.echo(f"✗ 無法載入規則檔：{exc}")
        raise typer.Exit(code=1) from exc
    except MapFailed as exc:
        typer.echo(f"✗ 映射失敗：{exc}")
        raise typer.Exit(code=1) from exc
    except MappingSchemaError as exc:
        # 最終整分支審查修正 Important 4：這是映射引擎自己的缺陷（產出的
        # mapping 不符合它自己宣告的 schema），不是使用者 --inventory 的
        # 問題——訊息本身（見 writer.MappingSchemaError）已經點名這一點，
        # 這裡不再加一層可能誤導成「你的輸入有問題」的措辭。
        typer.echo(f"✗ {exc}")
        raise typer.Exit(code=1) from exc
    except MappingSerializationError as exc:
        typer.echo(f"✗ 映射結果無法寫出：{exc}")
        raise typer.Exit(code=1) from exc
    except OSError as exc:
        # 涵蓋兩種情況，訊息刻意相同：build_mapping() 執行過程中透過
        # event_log.emit() 持續寫入 events.jsonl 時中途失敗，或最終寫出
        # mapping.json／mapping.csv 時失敗——兩者都是「開場已經成功、但
        # 執行中途或收尾時磁碟或權限出狀況」的環境問題，不是映射結果。
        typer.echo(f"✗ 無法寫入產出目錄 {out}：{exc}")
        raise typer.Exit(code=1) from exc

    mapping = result.mapping
    stats = mapping.stats
    typer.echo(
        f"✓ 映射完成："
        f"{stats.source_columns} 個來源欄位、對應 {stats.mapped_columns} 個"
        f"（auto {stats.auto}、suggested {stats.suggested}、needs_human {stats.needs_human}）、"
        f"覆蓋率 {stats.coverage_percent}%"
    )
    typer.echo(f"  → {result.json_path}")
    typer.echo(f"  → {result.csv_path}")
    typer.echo(f"  → {event_log.path}")

    unmapped_count = len(mapping.unmapped)
    if unmapped_count:
        typer.echo(
            f"\n{unmapped_count} 個欄位未對應（這是盤點結果的一部分，不是錯誤）："
            "只要在 rules/ 底下補一條規則（不需要改任何 Python），就能讓這些欄位對上，"
            "詳見套件內建 rules/README.md。"
        )
    else:
        typer.echo("\n0 個欄位未對應。")

    conflicts_count = len(mapping.conflicts)
    if conflicts_count:
        typer.echo(
            f"\n{conflicts_count} 個衝突：輸出不可信，需要人工裁決"
            "（mapping.json 仍已寫出，方便你檢視衝突內容）。"
        )
    else:
        typer.echo("\n0 個衝突。")

    if result.rule_set.ignored_files:
        ignored = result.rule_set.ignored_files
        typer.echo(
            f"\n規則目錄裡有 {len(ignored)} 個檔案沒有被讀取"
            "（只有 *.yaml／*.yml 會被當成規則檔讀取；README.md 以外的其他檔案"
            "會被忽略——這通常代表副檔名打錯字）："
        )
        for name in ignored:
            typer.echo(f"  {name}")

    if result.unused_rule_origins:
        unused = result.unused_rule_origins
        typer.echo(
            f"\n{len(unused)} 條規則沒有命中任何來源欄位（這是盤點結果的一部分，不是錯誤，"
            "但通常代表規則鍵名打錯字，值得檢查）："
        )
        for origin in unused:
            typer.echo(f"  {origin}")

    if event_log.has_fatal(Stage.MAP, strict=strict):
        raise typer.Exit(code=1)


def _resolve_password(*, password_stdin: bool) -> str:
    """密碼只從 `--password-stdin` 或 `MIGRAI_ODOO_PASSWORD` 讀（P3-2）。

    刻意沒有 `--password` 旗標：密碼進 argv 會被同機器上任何使用者用
    `ps` 看到，也會落進 shell 歷史，README／`make` 目標裡的範例指令
    會教使用者養成這個危險習慣。
    """
    if password_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    return os.environ.get(PASSWORD_ENV_VAR, "")


@app.command(name="load")
def load_command(
    mapping: Path = typer.Option(  # noqa: B008
        ...,
        "--mapping",
        help="map 產出的 mapping.json 路徑",
    ),
    source: Path = typer.Option(  # noqa: B008
        ...,
        "--source",
        help="SQLite 來源資料庫（make corpus 產生的 data.sqlite）",
    ),
    source_id: str = typer.Option(  # noqa: B008
        ...,
        "--source-id",
        help="區隔不同來源系統或批次的字串，會進 external_id",
    ),
    odoo_url: str | None = typer.Option(  # noqa: B008
        None,
        "--odoo-url",
        help="Odoo base URL，例如 http://localhost:8069（--dry-run 時可省略）",
    ),
    odoo_db: str | None = typer.Option(  # noqa: B008
        None,
        "--odoo-db",
        help="Odoo 資料庫名稱（--dry-run 時可省略）",
    ),
    odoo_user: str | None = typer.Option(  # noqa: B008
        None,
        "--odoo-user",
        help="Odoo 使用者帳號（--dry-run 時可省略）",
    ),
    password_stdin: bool = typer.Option(  # noqa: B008
        False,
        "--password-stdin",
        help="從標準輸入讀取 Odoo 密碼；未指定則讀環境變數 MIGRAI_ODOO_PASSWORD"
        "（--dry-run 時不需要密碼）。",
    ),
    out: Path = typer.Option(  # noqa: B008
        Path("out"),
        "--out",
        help="產出目錄，會寫入 migration_log.jsonl 與 events.jsonl",
    ),
    batch_size: int = typer.Option(  # noqa: B008
        100,
        "--batch-size",
        help="整批寫入 Odoo 的批次大小；整批失敗會自動降級逐筆重試",
    ),
    dry_run: bool = typer.Option(  # noqa: B008
        False,
        "--dry-run",
        help="不連線 Odoo，走完讀取／轉換／排序／組 payload 的全部路徑；"
        "migration_log.jsonl 的 action 一律 skipped。給本機沒有 Odoo 的貢獻者用。",
    ),
    strict: bool = typer.Option(  # noqa: B008
        False,
        "--strict",
        help="把 ERROR／CRITICAL 等級的事件也視為失敗（exit 1）；"
        "E-4004／E-4005（WARN）不受影響，仍視為完成。",
    ),
) -> None:
    """把 `mapping.json` 描述的對應關係寫進 Odoo，產出 migration_log.jsonl。"""
    try:
        event_log = _fresh_event_log(out)
    except OSError as exc:
        typer.echo(f"✗ 無法寫入產出目錄 {out}：{exc}")
        raise typer.Exit(code=1) from exc

    client: OdooClient | None = None
    if not dry_run:
        # --dry-run 是為本機沒有 Odoo 的貢獻者服務的（D1 貢獻門檻）：
        # 非 --dry-run 執行才需要真的湊齊連線資訊與密碼。
        missing = [
            name
            for name, value in (
                ("--odoo-url", odoo_url),
                ("--odoo-db", odoo_db),
                ("--odoo-user", odoo_user),
            )
            if not value
        ]
        if missing:
            typer.echo(f"✗ 非 --dry-run 執行必須提供 {'、'.join(missing)}")
            raise typer.Exit(code=1)

        password = _resolve_password(password_stdin=password_stdin)
        if not password:
            typer.echo(
                "✗ 未提供密碼：請用 --password-stdin 從標準輸入讀取，"
                f"或設定環境變數 {PASSWORD_ENV_VAR}（--dry-run 不需要密碼）"
            )
            raise typer.Exit(code=1)
        client = OdooClient(url=odoo_url, db=odoo_db, user=odoo_user, password=password)

    try:
        with SqliteSourceReader(source) as reader:
            try:
                mapping_text = mapping.read_text(encoding="utf-8")
            except OSError as exc:
                typer.echo(f"✗ 讀不到 --mapping {mapping}：{exc}")
                raise typer.Exit(code=1) from exc
            try:
                mapping_data = json.loads(mapping_text)
            except json.JSONDecodeError as exc:
                typer.echo(f"✗ --mapping {mapping} 不是合法的 JSON：{exc}")
                raise typer.Exit(code=1) from exc

            result = run_load(
                mapping_data,
                reader,
                client,
                source_id=source_id,
                out_dir=out,
                event_log=event_log,
                batch_size=batch_size,
                dry_run=dry_run,
            )
    except SourceError as exc:
        typer.echo(f"✗ {exc}")
        raise typer.Exit(code=1) from exc
    except (LogEntrySchemaError, ValueError) as exc:
        typer.echo(f"✗ {exc}")
        raise typer.Exit(code=1) from exc
    except OSError as exc:
        typer.echo(f"✗ 無法寫入產出目錄 {out}：{exc}")
        raise typer.Exit(code=1) from exc

    # Task 10 審查修正輪次 1 Important I5：E-4001 中止時（例如來源表沒有
    # 主鍵）原本 stdout 仍然印綠勾，原因只寫進 events.jsonl——本機沒有
    # Odoo 的貢獻者只有這個畫面可看，看到綠勾會誤以為真的成功了。這裡先
    # 判斷這場執行有沒有致命事件，再決定印 ✓ 還是 ✗，中止時額外印出最後
    # 一則致命事件的原因。
    fatal_event = event_log.last_fatal(Stage.LOAD, strict=strict)
    counts = Counter(entry.action for entry in result.entries)
    symbol = "✗" if fatal_event is not None else "✓"
    typer.echo(
        f"{symbol} 載入完成：共 {len(result.entries)} 筆"
        f"（created {counts['created']}、updated {counts['updated']}、"
        f"failed {counts['failed']}、skipped {counts['skipped']}）"
    )
    if fatal_event is not None:
        typer.echo(f"  原因：[{fatal_event['error_code']}] {fatal_event['message']}")
    typer.echo(f"  → {result.log_path}")
    typer.echo(f"  → {event_log.path}")

    if fatal_event is not None:
        raise typer.Exit(code=1)


@app.command()
def version() -> None:
    """顯示版本。"""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
