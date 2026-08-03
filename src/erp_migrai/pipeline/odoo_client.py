"""Odoo 17 的 JSON-RPC 客戶端：`OdooClient`。

**這是整個 package 裡唯一知道 HTTP 存在的檔案。** 它只做三件事：組
JSON-RPC 封包、送出、把 HTTP／協定層的錯誤轉成 `OdooError`／`OdooAuthError`／
`RateLimited` 三種例外之一。**不含任何重試、退避、批次邏輯**——那些是
Task 10／11 `loader.py` 的責任，理由是唯有在那一層才知道「這一批要不要
整批重試」「退避幾秒」這些跨多次呼叫的狀態，這裡每次呼叫都是獨立的。

## 介面形狀是抓 Odoo 原始碼核對過的，不是猜的

- `/jsonrpc` 路由（`odoo/addons/base/controllers/rpc.py`）：
  `params` 必須是 `{"service", "method", "args"}`。
- `object` 服務的 args 順序（`odoo/service/model.py` 的 `dispatch()` 與
  `execute_kw()`）：`[db, uid, password, model, method, args, kwargs]`。
- 認證失敗**不是拋例外，是 JSON-RPC `result` 回傳 `false`**
  （`odoo/service/common.py` 的 `exp_authenticate`：
  `except AccessDenied: return False`）。`authenticate()` 因此必須自己
  檢查 `result` 的真假值，不能只靠「有沒有 `error` 鍵」判斷。
- 錯誤回應的真正訊息在 `error["data"]["message"]`，不是 `error["message"]`
  （後者固定是通用字串 `"Odoo Server Error"`，見 `odoo/http.py` 的
  `handle_error()`／`serialize_exception()`）。

## 密碼絕不外洩——這裡列出每一條被想到並封住的路徑

1. **`repr(OdooClient(...))`**：`password` 欄位標了
   `field(repr=False)`，dataclass 自動產生的 `__repr__` 就不會印出它。
   單靠「例外訊息裡沒有密碼」守不住這條——`repr()` 是完全獨立的另一扇門
   （任何 `logging.debug("client=%s", client)` 都會經過它）。
2. **`_raise_for_error`／`_extract_uid` 的例外訊息**：只組進 Odoo 給的
   `data.message`／`error.message`，絕不把整包 `error` 或 `body` 塞進訊息
   ——`error["data"]["debug"]` 是伺服器端完整 traceback，混進去的話不只
   洩漏敏感細節，還會把一大段伺服器內部堆疊倒進使用者終端與 CI 日誌。
3. **HTTP 層的例外轉換（`_send`）**：`urllib.error.HTTPError`／`URLError`
   轉成 `OdooError` 時，只取 `exc.code`／`exc.reason`（HTTP 狀態碼與
   reason phrase，兩者都由伺服器/連線層決定，不含請求內容），刻意不呼叫
   `exc.read()`——那是伺服器回應本文，是否會回顯請求內容不確定，不冒
   這個風險；也絕不把 `payload`（含密碼）本身塞進任何例外訊息。
4. **`payload` 字典本身**：正常路徑本來就含密碼（`object.execute_kw`
   的 args 第三個位置），這是協定要求、不是洩漏——洩漏指的是密碼跑進
   「人會看到」的地方（例外訊息、log、repr），而不是跑進「送給 Odoo
   的封包」這個本來就該有密碼的地方。這裡沒有任何地方對 `payload`
   呼叫 `repr()`／`str()` 再塞進例外或 log。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


class OdooError(Exception):
    """Odoo 回了錯誤，或連線失敗。"""


class OdooAuthError(OdooError):
    """認證失敗（帳密錯、資料庫不存在）。"""


class RateLimited(OdooError):
    """收到 429 或 503。載入階段據此發 E-4005 並退避。"""


@dataclass
class OdooClient:
    url: str
    db: str
    user: str
    # repr=False：dataclass 預設產生的 __repr__ 會把每個欄位的值印出來，
    # 密碼欄位若不排除，任何印出這個物件的地方（log、例外訊息裡不慎
    # 帶上 client 本身、REPL、debugger）都會把密碼原樣洩漏出去。這不是
    # 「例外訊息裡沒有密碼」那條測試守得住的範圍——repr() 是另一扇門。
    password: str = field(repr=False)
    # 認證後快取的 uid：execute_kw() 需要 uid 才能組封包，但介面本身
    # （見任務書）不讓呼叫端每次都手動傳 uid，因此第一次呼叫
    # execute_kw() 時若還沒認證過，就地呼叫一次 authenticate()。
    # 這不是重試／退避邏輯（那些一律留給 loader.py）：這裡只是
    # 「還沒登入就先登入一次」，同一次呼叫只嘗試一次，失敗就讓
    # OdooAuthError 原樣往外拋。
    _uid: int | None = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------------
    # 封包組裝（純函式，不碰網路，Step 1 的測試對象）
    # ------------------------------------------------------------------

    def _envelope(self, service: str, method: str, args: list[Any]) -> dict[str, Any]:
        """組出 JSON-RPC 2.0 的請求信封本身，不含任何服務特定的 args 語意。

        鍵的形狀（`jsonrpc`／`method`／`params`／`id`）抄自
        `odoo/http.py` `JsonRequest.dispatch()` docstring 裡列出的
        範例請求；`id` 該路由的 dispatcher 不會讀，但這是 JSON-RPC 2.0
        規格本身的一部分，帶著它不會錯，也符合官方文件給的範例形狀。
        """
        return {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
            "id": None,
        }

    def _build_payload(
        self,
        model: str,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any] | None = None,
        *,
        uid: int,
    ) -> dict[str, Any]:
        """組 `object.execute_kw` 的 JSON-RPC 封包。

        `args` 順序 `[db, uid, password, model, method, args, kwargs]`
        抓 `odoo/service/model.py` 的 `dispatch()`
        （`db, uid, passwd = params[0], int(params[1]), params[2]`）與
        `execute_kw(db, uid, obj, method, args, kw=None)` 核對過，不是
        憑記憶猜的形狀。

        `kwargs` 是 `None` 時**原樣傳 `None`**，不在這裡改寫成 `{}`：
        Odoo 自己的 `execute_kw(db, uid, obj, method, args, kw=None)`
        簽章預設值就是 `None`，伺服器那端用 `kw or {}` 處理——送 `None`
        與送 `{}` 對伺服器而言是同一件事，這裡沒有理由多做一層轉換。
        """
        return self._envelope(
            "object",
            "execute_kw",
            [self.db, uid, self.password, model, method, args, kwargs],
        )

    def _build_login_payload(self) -> dict[str, Any]:
        """組 `common.login` 的 JSON-RPC 封包。

        args 順序 `[db, user, password]` 抓
        `odoo/service/common.py` 的 `exp_login(db, login, password)`
        核對過——`common` 服務的 dispatcher 是
        `g['exp_' + method](*params)`，所以 `common.login` 的 args 就是
        直接對應 `exp_login` 的三個位置參數。
        """
        return self._envelope("common", "login", [self.db, self.user, self.password])

    # ------------------------------------------------------------------
    # 錯誤映射（純函式，不碰網路，Step 1 的測試對象）
    # ------------------------------------------------------------------

    def _raise_for_error(self, body: dict[str, Any]) -> None:
        """JSON-RPC 回應含 `error` 鍵時拋 `OdooError`；否則什麼都不做。

        真正的訊息在 `error["data"]["message"]`，不是 `error["message"]`
        ——後者固定是 `"Odoo Server Error"` 這種通用字串（見
        `odoo/http.py` 的 `handle_error()`）。`data` 或
        `data["message"]` 缺席時退回 `error["message"]`，兩者都缺席時
        用固定的通用訊息——**不能讓 `KeyError` 從這裡漏出去**，那會把
        「Odoo 到底回了什麼錯」這個問題換成一個更難懂的
        「我們自己的程式碼哪裡壞了」。

        刻意只組進 `data.message`／`error.message` 這兩個字串欄位，絕不
        把整包 `error`／`data` 塞進例外訊息——`data.debug` 是伺服器端的
        完整 traceback，混進去等於把一大段伺服器內部堆疊倒進使用者
        終端與 CI 日誌。
        """
        error = body.get("error")
        if error is None:
            return
        data = error.get("data") or {}
        message = data.get("message") or error.get("message") or "Odoo 回傳未知錯誤"
        raise OdooError(message)

    def _extract_uid(self, body: dict[str, Any]) -> int:
        """把 `common.login` 的 JSON-RPC 回應轉成 uid。

        Odoo 的 `exp_authenticate` 對認證失敗（帳密錯、資料庫不存在）
        **不拋例外，是回傳 `False`**（`odoo/service/common.py`：
        `except AccessDenied: return False`）。也就是說帳密錯的那次
        JSON-RPC 呼叫仍然是「成功」的（沒有 `error` 鍵），只是
        `result` 的值是 `False`——只檢查有沒有 `error` 鍵完全抓不到
        這種失敗，必須額外檢查 `result` 本身的真假值。
        """
        self._raise_for_error(body)
        uid = body.get("result")
        if not uid:
            raise OdooAuthError(f"登入失敗：帳號或密碼錯誤，或資料庫 {self.db!r} 不存在")
        return uid

    def _raise_for_http_status(self, status: int, reason: str) -> None:
        """把 HTTP 錯誤狀態碼轉成對應的例外。

        呼叫端只在真的收到非 2xx 狀態碼（`urllib.error.HTTPError`）時
        才呼叫這個方法，所以這裡不處理「成功」的情況——這是一個窄函式，
        兩個分支都一定會拋例外。

        429（Too Many Requests）／503（Service Unavailable）是限流／
        服務暫時不可用的訊號，載入階段要據此退避重試（`RateLimited`，
        對應 E-4005）；其他 4xx／5xx 是一般協定層錯誤，沒有「等一下再試
        大概率會成功」這個語意，歸為普通 `OdooError`。

        只取 `status`／`reason`（HTTP 狀態碼與 reason phrase），不取
        HTTP 回應本文——避免任何請求內容經由回應回顯而外洩。
        """
        if status in (429, 503):
            raise RateLimited(f"Odoo 回應 HTTP {status}（限流或服務不可用）：{reason}")
        raise OdooError(f"Odoo 回應 HTTP {status}：{reason}")

    # ------------------------------------------------------------------
    # 網路層（本任務的誠實界線之外：Task 12 的 CI 整合測試才驗證
    # 「Odoo 真的接受這個封包」；這裡刻意寫得很薄，可測的邏輯都已經
    # 抽成上面幾個純函式）
    # ------------------------------------------------------------------

    def _send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """把 `payload` POST 到 `/jsonrpc`，回傳解析後的 JSON body。

        這是這個檔案裡唯一真的碰網路的方法：不重試、不退避、不批次
        ——出錯只管把例外類型轉對，要不要再試一次是 loader.py 的責任。
        本機不對這個方法起假 HTTP server 測試（Global Constraints 第
        2 條禁止），它的正確性留給 Task 12 的 CI 整合測試驗證。
        """
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request) as response:  # noqa: S310
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # 只取狀態碼與 reason phrase，不呼叫 exc.read()——那是伺服器
            # 回應本文，會不會回顯請求內容不確定，不冒這個風險。
            self._raise_for_http_status(exc.code, exc.reason or "")
        except urllib.error.URLError as exc:
            # exc.reason 是連線層失敗原因（DNS 解析失敗、連線被拒絕、
            # 逾時……），不含任何請求內容。
            raise OdooError(f"連線 Odoo 失敗：{exc.reason}") from None
        return json.loads(raw)

    # ------------------------------------------------------------------
    # 對外介面
    # ------------------------------------------------------------------

    def authenticate(self) -> int:
        """呼叫 `common.login`，回傳 uid；失敗拋 `OdooAuthError`。"""
        body = self._send(self._build_login_payload())
        uid = self._extract_uid(body)
        self._uid = uid
        return uid

    def execute_kw(
        self,
        model: str,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        """呼叫 `/jsonrpc` 的 `object.execute_kw`。

        還沒呼叫過 `authenticate()` 時，就地認證一次再送出這次呼叫——
        這不是重試邏輯，只是「execute_kw 需要 uid 才能組封包，還沒有
        就先拿一個」，同一次呼叫只嘗試一次，認證失敗就讓
        `OdooAuthError` 原樣往外拋，不吞、不重試。
        """
        if self._uid is None:
            self.authenticate()
        payload = self._build_payload(model, method, args, kwargs, uid=self._uid)
        body = self._send(payload)
        self._raise_for_error(body)
        return body["result"]
