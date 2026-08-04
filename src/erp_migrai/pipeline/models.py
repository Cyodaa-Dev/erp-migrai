"""載入階段的資料模型：`LogEntry`，`migration_log.jsonl` 每一列的內容。

跟 `mapping/models.py` 的分工一致：這裡只放資料模型本身，schema 載入／
驗證／寫檔的邏輯住在 `loader.py`（precedent：`mapping/writer.py` 對
`mapping/models.py` 的關係）。

`action` 的四個值（設計 spec §3.5）意義互斥且窮盡，不得有第五種：

| 值 | 意義 | `odoo_id` | `error_code` |
|---|---|---|---|
| `created` | Odoo 中原本沒有這個 external_id，這次新建 | 非 null | null |
| `updated` | Odoo 中已有這個 external_id，這次覆寫（冪等重跑的正常結果） | 非 null | null |
| `failed` | 轉換或寫入失敗 | null | 非 null |
| `skipped` | 未嘗試寫入（上游記錄本身失敗，或本次是 dry-run） | null | null |

`skipped` 不另外發錯誤碼——根因那一筆已經發過 E-4002 了，重複發碼會讓
`events.jsonl` 的事件數量說謊；`message` 必須點名是哪一筆父記錄失敗，或
說明這是 dry-run。

`__post_init__` 在建構當下就把上面這張表的四個互斥約束釘死，是
`migration_log.schema.json` 之外的第二道防線：schema 只在**寫出前**驗證
最終的 dict，這裡在**物件建構當下**就擋，讓錯誤更早浮出、訊息也能直接
點名是哪個欄位組合不合法。

刻意不帶時間戳——時間只住在 `events.jsonl`，兩份檔案用 `run_id` join。
"""

from __future__ import annotations

from dataclasses import dataclass

# 四個值互斥且窮盡；`test_action_values_are_exactly_four` 之類的測試（Task 10
# 測試檔）直接比對這個集合，不得有第五種（precedent：`mapping/models.py`
# 的 `ConflictKind` 三值測試 `test_conflict_kinds_are_exactly_three`）。
ACTIONS = frozenset({"created", "updated", "failed", "skipped"})

# 每個 action 對 (odoo_id, error_code) 是否允許非 null 的約束表，直接對應
# 上面 docstring 的表格——集中成一份資料而不是散在 if/elif 裡，讓
# `__post_init__` 的邏輯與這份表格永遠一致，不會漏改其中一邊。
_ODOO_ID_REQUIRED_ACTIONS = frozenset({"created", "updated"})
_ERROR_CODE_REQUIRED_ACTIONS = frozenset({"failed"})


@dataclass(frozen=True)
class LogEntry:
    """`migration_log.jsonl` 一列的內容。"""

    run_id: str
    source_table: str
    source_pk: tuple[str, ...]
    external_id: str
    target_model: str
    action: str
    odoo_id: int | None
    error_code: str | None
    message: str

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"LogEntry.action 必須是 {sorted(ACTIONS)} 之一，收到 {self.action!r}")
        if not self.source_pk:
            raise ValueError("LogEntry.source_pk 不能是空序列")
        odoo_id_required = self.action in _ODOO_ID_REQUIRED_ACTIONS
        if odoo_id_required and self.odoo_id is None:
            raise ValueError(f"action={self.action!r} 時 odoo_id 必須非 null")
        if not odoo_id_required and self.odoo_id is not None:
            raise ValueError(f"action={self.action!r} 時 odoo_id 必須是 null")
        error_code_required = self.action in _ERROR_CODE_REQUIRED_ACTIONS
        if error_code_required and self.error_code is None:
            raise ValueError(f"action={self.action!r} 時 error_code 必須非 null")
        if not error_code_required and self.error_code is not None:
            raise ValueError(f"action={self.action!r} 時 error_code 必須是 null")

    def to_dict(self) -> dict[str, object]:
        """轉成可直接 `json.dumps` 的 dict，鍵的順序與
        `migration_log.schema.json` 的 `required` 一致。"""
        return {
            "run_id": self.run_id,
            "source_table": self.source_table,
            "source_pk": list(self.source_pk),
            "external_id": self.external_id,
            "target_model": self.target_model,
            "action": self.action,
            "odoo_id": self.odoo_id,
            "error_code": self.error_code,
            "message": self.message,
        }
