"""錯誤碼與事件記錄。

錯誤碼沿用 PRD §9B 的原碼原意，使商業版讀取本工具的日誌時共用同一套語彙。
MVP 只含模組一／三／五，故不含 E-3001~E-3003；E-1003（掃描逾時）亦已剔除
——那是 SaaS 語境的門檻，本機工具的逾時應由使用者自行中斷。

事件格式是 PRD §9B.3 的子集：砍除 project_id／task_id／user_id（本機工具沒有
多租戶概念），trace_id 改為 run_id。商業版補回那三個欄位即為完整 PRD schema，
不需要轉換——相容而非相同。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class Severity(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class Stage(StrEnum):
    SCAN = "scan"
    MAP = "map"
    LOAD = "load"
    VERIFY = "verify"


@dataclass(frozen=True)
class ErrorCode:
    code: str
    name: str
    severity: Severity
    stage: Stage


E_1001 = ErrorCode("E-1001", "SCAN_FAILED", Severity.CRITICAL, Stage.SCAN)
E_1002 = ErrorCode("E-1002", "PARSE_ERROR", Severity.ERROR, Stage.SCAN)
E_2001 = ErrorCode("E-2001", "MAPPING_CONFLICT", Severity.ERROR, Stage.MAP)
E_2002 = ErrorCode("E-2002", "COVERAGE_LOW", Severity.WARN, Stage.MAP)
E_4001 = ErrorCode("E-4001", "EXTRACTION_FAILED", Severity.ERROR, Stage.LOAD)
E_4002 = ErrorCode("E-4002", "TRANSFORM_ERROR", Severity.ERROR, Stage.LOAD)
E_4003 = ErrorCode("E-4003", "LOAD_FAILED", Severity.ERROR, Stage.LOAD)
E_4004 = ErrorCode("E-4004", "BATCH_RETRY", Severity.WARN, Stage.LOAD)
E_4005 = ErrorCode("E-4005", "RATE_LIMIT_HIT", Severity.WARN, Stage.LOAD)
E_5001 = ErrorCode("E-5001", "TOTAL_MISMATCH", Severity.ERROR, Stage.VERIFY)
E_5002 = ErrorCode("E-5002", "BALANCE_ERROR", Severity.ERROR, Stage.VERIFY)

ERROR_CODES: dict[str, ErrorCode] = {
    c.code: c
    for c in (
        E_1001,
        E_1002,
        E_2001,
        E_2002,
        E_4001,
        E_4002,
        E_4003,
        E_4004,
        E_4005,
        E_5001,
        E_5002,
    )
}

# 每個階段「致命」的錯誤碼——只有這些會讓該階段的 exit code 變成 1。
#
# scan 的 E-1002（不支援語法）刻意不列入：不支援清單是 scan 的一級產出，
# 不是 scan 的失敗。若使用者想把它當成閘門，用 --strict。
FATAL_CODES: dict[Stage, frozenset[str]] = {
    Stage.SCAN: frozenset({"E-1001"}),
    Stage.MAP: frozenset({"E-2001"}),
    Stage.LOAD: frozenset({"E-4001", "E-4002", "E-4003"}),
    Stage.VERIFY: frozenset({"E-5001", "E-5002"}),
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_uuid() -> str:
    return str(uuid.uuid4())


@dataclass
class EventLog:
    """把事件同時累積在記憶體並附加寫入 JSONL。

    clock 與 id_factory 可注入，測試才能取得可預期的輸出。
    """

    path: Path
    run_id: str
    clock: Callable[[], datetime] = _utc_now
    id_factory: Callable[[], str] = _new_uuid
    events: list[dict[str, Any]] = field(default_factory=list)

    def emit(
        self,
        event_type: str,
        *,
        severity: Severity = Severity.INFO,
        code: ErrorCode | None = None,
        message: str = "",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # 驗證 ErrorCode 必須是規範表內的版本；11 個碼是與下游消費者的契約，
        # 未知碼是程式錯誤，必須在出錯點浮出，不可靜默通過。
        if code is not None:
            canonical = ERROR_CODES.get(code.code)
            if canonical is None or canonical != code:
                raise ValueError(f"不認可的錯誤碼或欄位不符：{code.code}")

        event: dict[str, Any] = {
            "event_id": self.id_factory(),
            "event_type": event_type,
            "timestamp": self.clock().isoformat().replace("+00:00", "Z"),
            "severity": str(code.severity if code is not None else severity),
            "error_code": code.code if code is not None else None,
            "message": message,
            "details": details if details is not None else {},
            "run_id": self.run_id,
        }
        self.events.append(event)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def has_fatal(self, stage: Stage, *, strict: bool = False) -> bool:
        """該階段是否出現足以決定非零 exit code 的錯誤。"""
        fatal = FATAL_CODES[stage]
        for event in self.events:
            code = event["error_code"]
            if code is None:
                continue
            if code in fatal:
                return True
            # 直接索引 ERROR_CODES[code] 是安全的，因為 emit() 已保證任何
            # 存入事件的錯誤碼都在規範表內且欄位相符。
            if strict and ERROR_CODES[code].severity in (
                Severity.ERROR,
                Severity.CRITICAL,
            ):
                return True
        return False
