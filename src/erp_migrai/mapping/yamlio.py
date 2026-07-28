"""共用的「嚴格」YAML 讀取工具，供 catalog 與 rules 載入器共用。

這裡只有一件事：把 PyYAML 的 `safe_load` 加嚴，讓同一個 mapping 裡出現
重複 key 時直接丟例外，而不是像預設行為那樣悄悄保留最後一次、蓋掉前面的
定義。catalog（`odoo17_catalog.yaml`）與 rules（`rules/*.yaml`）都是「人送
的資料檔」（例如透過 pull request），共用同一份嚴格度要求，所以只寫一次、
兩邊 import。
"""

from __future__ import annotations

import io
from typing import Any

import yaml
from yaml.constructor import ConstructorError


class StrictSafeLoader(yaml.SafeLoader):
    """比預設 `yaml.SafeLoader` 更嚴格：mapping 內出現重複 key 就直接丟例外。

    PyYAML 的預設行為是「後蓋前」——同一個 mapping 裡出現兩次的 key，只會
    悄悄保留最後一次出現的那個，前一次連同它底下所有的內容一起消失，且完全
    不會有任何警告或例外。對這份 catalog（以及 rules 檔）來說，這代表複製
    貼上一個模型／欄位／規則區塊時忘記改名稱，前面整段定義會人間蒸發，而
    載入過程看起來完全正常——這正是本專案要不計代價避免的「悄悄產出錯誤
    結果」。

    只覆寫 `construct_mapping`（mapping 節點怎麼組成 dict 的那一步），其餘
    完全繼承自 `SafeLoader`。這代表它與 `safe_load` 有完全相同的安全性——
    不會執行任意 Python、不會建構任意 Python 物件——只是在 `safe_load` 既有
    的安全基礎上「加嚴」，不是換成不安全的 `yaml.load` 預設 Loader。
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, yaml.MappingNode):
            raise ConstructorError(
                None,
                None,
                f"預期是一個 mapping 節點，實際是 {node.id}",
                node.start_mark,
            )

        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                raise ConstructorError(
                    None,
                    None,
                    f"重複的 key `{key}`",
                    key_node.start_mark,
                )
            value = self.construct_object(value_node, deep=deep)
            mapping[key] = value
        return mapping


def safe_load_strict(text: str, source_name: str) -> Any:
    """等同 `yaml.safe_load`，但額外偵測 mapping 內的重複 key（見
    `StrictSafeLoader`）。

    catalog 與 rules 的所有 YAML 讀取都必須經過這個函式，而不是直接呼叫
    `yaml.safe_load`——這樣任何層級（頂層、巢狀 mapping、單一項目定義自己的
    key）的重複 key 才會一視同仁地被攔下來，不會有「只檢查頂層」的死角。

    `source_name` 只是用來讓 PyYAML 回報的錯誤（例如「第幾行」）能點名是
    哪個檔案，不影響解析結果本身。
    """
    stream = io.StringIO(text)
    stream.name = source_name  # PyYAML 的錯誤訊息會讀這個屬性當作檔名
    return yaml.load(stream, Loader=StrictSafeLoader)
