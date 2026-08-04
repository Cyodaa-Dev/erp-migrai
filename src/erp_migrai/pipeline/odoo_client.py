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

## 密碼外洩：已封住的路徑，以及呼叫端自己要避開的路徑

**已封住**：

1. **`repr(OdooClient(...))`／`"%s" % client`**（走 `__repr__`）：`password`
   欄位標了 `field(repr=False)`，dataclass 自動產生的 `__repr__` 就不會
   印出它。單靠「例外訊息裡沒有密碼」守不住這條——`repr()` 是完全獨立的
   另一扇門（任何 `logging.debug("client=%s", client)` 都會經過它）。
2. **`_raise_for_error`／`_extract_uid` 的例外訊息**：只組進 Odoo 給的
   `data.message`／`error.message`，絕不把整包 `error` 或 `body` 塞進訊息
   ——`error["data"]["debug"]` 是伺服器端完整 traceback，混進去的話不只
   洩漏敏感細節，還會把一大段伺服器內部堆疊倒進使用者終端與 CI 日誌。
3. **HTTP 層的例外轉換（`_send`／`_map_connection_error`）**：
   `urllib.error.HTTPError`／`URLError` 轉成 `OdooError` 時，只取
   `exc.code`／`exc.reason`（HTTP 狀態碼與 reason phrase，兩者都由
   伺服器/連線層決定，不含請求內容），刻意不呼叫 `exc.read()`——那是
   伺服器回應本文，是否會回顯請求內容不確定，不冒這個風險；也絕不把
   `payload`（含密碼）本身塞進任何例外訊息。

**未封住、呼叫端自己要避開**：`dataclasses.asdict(client)`／
`astuple(client)`／`vars(client)`／`client.__dict__`——這幾條路徑都繞過
`__repr__`，密碼會原樣出現在回傳的 dict／tuple 裡。**尤其是
`json.dumps(dataclasses.asdict(client))`**：把整個 `OdooClient` 序列化進
`events.jsonl` 或任何 log，是這個 codebase 最可能踩到這條的地方——
**不要把 `OdooClient` 整個序列化進事件或 log，只序列化你需要的欄位**
（例如 `client.url`）。

刻意不用 `Secret(str)` 之類的包裝類別封住 `password` 欄位：那種包裝只
封得住 `repr()` 這一條路，`json.dumps(dataclasses.asdict(client))` 照樣會
把底層字串明文吐出來——包一層會製造「密碼已經安全」的錯覺，而真正的
風險路徑（`json.dumps`）完全沒被封住，這是假的安心，比不包更危險。
真正的防線是**呼叫端的紀律**（不要把整個 client 物件序列化），這份
docstring 就是那個紀律的文件化，不是「每條路徑都已經被想到並封住」的
宣稱。
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


class OdooError(Exception):
    """Odoo 回了錯誤，或連線失敗。"""


class OdooAuthError(OdooError):
    """認證失敗（帳密錯、資料庫不存在）。"""


class RateLimited(OdooError):
    """收到 429 或 503。載入階段據此發 E-4005 並退避。"""


@dataclass
class OdooClient:
    # base URL，例如 "http://localhost:8069"——**這是 base URL，不是完整
    # 端點，不要自己接 /jsonrpc**：`_endpoint()` 會處理這件事，這是全
    # repo 唯一定義這個欄位語意的地方。
    url: str
    db: str
    user: str
    # repr=False：dataclass 預設產生的 __repr__ 會把每個欄位的值印出來，
    # 密碼欄位若不排除，任何印出這個物件的地方（log、例外訊息裡不慎
    # 帶上 client 本身、REPL、debugger）都會把密碼原樣洩漏出去。這不是
    # 「例外訊息裡沒有密碼」那條測試守得住的範圍——repr() 是另一扇門。
    password: str = field(repr=False)
    # 單次 HTTP 呼叫的逾時秒數（單位：秒）。urlopen 預設
    # timeout=socket._GLOBAL_DEFAULT_TIMEOUT，也就是永不逾時——Odoo
    # 半死不活（TCP 連得上、應用層不回）時 _send 會無限期掛住。這不是
    # 重試／退避邏輯：它是單次呼叫的參數，不牽涉「這批要不要整批重試」
    # 「退避幾秒」這種跨多次呼叫的狀態，落在本模組的責任範圍內。
    timeout: float = 30.0
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
        """JSON-RPC 回應含 `error` 鍵時拋例外；否則什麼都不做。

        真正的訊息在 `error["data"]["message"]`，不是 `error["message"]`
        ——後者固定是 `"Odoo Server Error"` 這種通用字串（見
        `odoo/http.py` 的 `handle_error()`）。`data` 或
        `data["message"]` 缺席時退回 `error["message"]`，兩者都缺席時
        用固定的通用訊息——**不能讓 `KeyError` 從這裡漏出去**，那會把
        「Odoo 到底回了什麼錯」這個問題換成一個更難懂的
        「我們自己的程式碼哪裡壞了」。真實 Odoo 回應通常兩個鍵都有
        （`data.message` 才是真訊息），優先序寫反的話每一則錯誤都會
        退化成固定字串 `"Odoo Server Error"`，這裡的順序不能鬆動。

        刻意只組進 `data.message`／`error.message` 這兩個字串欄位，絕不
        把整包 `error`／`data` 塞進例外訊息——`data.debug` 是伺服器端的
        完整 traceback，混進去等於把一大段伺服器內部堆疊倒進使用者
        終端與 CI 日誌。

        `error`／`data` 若不是 dict（協定被破壞，理論上不該發生但不能
        假設一定不發生），一律當空處理，不能讓 `AttributeError` 從這裡
        漏出去。

        `data["name"]` 以 `"AccessDenied"` 結尾時拋 `OdooAuthError` 而
        不是普通 `OdooError`：`execute_kw` 每次 dispatch 都會跑
        `security.check(db, uid, passwd)`（`odoo/service/model.py`），
        認證失效（例如 session 過期、密碼被改）會在這裡拋
        `odoo.exceptions.AccessDenied`——這是除了 `common.login` 回傳
        `False` 之外，認證失敗的第二條路。Task 11 若把 E-4002 綁在
        `except OdooAuthError` 上，漏掉這條會把認證問題誤判成普通資料
        錯誤。
        """
        error = body.get("error")
        if error is None:
            return
        if not isinstance(error, dict):
            raise OdooError(f"Odoo 回傳的 error 不是預期的物件形狀（{type(error).__name__}）")
        data = error.get("data")
        if not isinstance(data, dict):
            data = {}
        message = data.get("message") or error.get("message") or "Odoo 回傳未知錯誤"
        if str(data.get("name", "")).endswith("AccessDenied"):
            raise OdooAuthError(message)
        raise OdooError(message)

    def _extract_result(self, body: dict[str, Any]) -> Any:
        """把 `execute_kw` 的 JSON-RPC 回應轉成呼叫端拿到的回傳值。

        Odoo 的方法回傳 `None` 時，JSON-RPC 回應根本不會有 `result` 鍵
        （`odoo/http.py`：`if result is not None: response['result'] =
        result`）——例如 `account.move.button_draft()` 最後一行是
        `self.state = 'draft'`，沒有 `return`。這裡因此不能直接
        `body["result"]`，那會讓一個正常的「方法沒有回傳值」變成一個
        逃出本模組職責契約的裸 `KeyError`。呼叫端應假設「沒有
        `result` 鍵」與「`result` 是 `None`」是同一件事。

        呼叫端要先呼叫 `_raise_for_error(body)`，這個方法本身不檢查
        `error` 鍵——它只負責 `result` 這一半的形狀轉換。
        """
        if "result" not in body:
            return None
        return body["result"]

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

    def _endpoint(self) -> str:
        """組出 `/jsonrpc` 的完整端點字串（純函式，不碰網路）。

        `self.url` 的語意是 base URL（見欄位註解），呼叫端可能給
        `"http://host:8069"` 或 `"http://host:8069/"`——`rstrip("/")`
        確保兩種寫法都組出同一個端點，不會出現
        `http://host:8069//jsonrpc` 這種雙斜線。這是全 repo 唯一知道
        `/jsonrpc` 這個路由字面值的地方，呼叫端不必也不該自己記得接。

        scheme 檢查刻意放在這個**純函式**裡，而不是放在 `_send`：
        `urlopen` 對 `file://` 一樣會動作，`url` 又來自使用者設定，這是
        必須擋的；但放在 `_send` 裡的守衛量不準——把它拿掉之後，
        `ftp://`／`file://` 的測試仍然會通過，因為那些 URL 連不上、
        `urlopen` 拋的 `URLError` 照樣被轉成 `OdooError`，測試分辨不出
        「scheme 被擋下」與「連線失敗」。放在這裡就沒有這個歧義：這個
        函式完全不碰網路，唯一會拋 `OdooError` 的原因就是 scheme 不對。
        """
        scheme = urlparse(self.url).scheme
        if scheme not in ("http", "https"):
            raise OdooError(f"不支援的 URL scheme：{self.url!r}（只接受 http／https）")
        return self.url.rstrip("/") + "/jsonrpc"

    def _parse_response(self, raw: bytes) -> dict[str, Any]:
        """把 HTTP 回應本文解析成 JSON-RPC 的 body（純函式，不碰網路）。

        `url` 給錯（例如漏接 `/jsonrpc`，或指到 Odoo 的網頁路由）常見
        的結果是拿到 HTML 而不是 JSON：Odoo 對 `/` 回 303 轉址到
        `/web`，`urllib` 預設會自動跟隨轉址且把 POST 降級成 GET
        （`urllib/request.py` 對 301/302/303 的處理）。`json.loads`
        對 HTML 會拋 `json.JSONDecodeError`（`ValueError` 的子類，不在
        `OdooError` 家族裡）——這裡接住，轉成 `OdooError`，不讓它逃出
        這個模組唯一的職責契約。

        訊息**只帶原始回應的長度與解析失敗的位置，絕不把 `raw` 本身
        塞進去**——回應本文有可能回顯了請求內容（含密碼），混進例外
        訊息就是另一條密碼外洩路徑。
        """
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OdooError(
                f"Odoo 回應不是合法 JSON（{len(raw)} bytes，解析失敗於位置 {exc.pos}）"
            ) from None
        # 合法 JSON 但不是物件（`null`／陣列／字串／數字）同樣要在這裡收斂：
        # 下游一律 `body.get(...)`，非 dict 會噴裸 `AttributeError`。最外層
        # 這一層恰好是最可能被中介（反向代理、WAF、企業防火牆的攔截頁）
        # 控制的，回一個 `null` 或 JSON 陣列並不罕見。同理，型別名稱可以進
        # 訊息，內容不行。
        if not isinstance(body, dict):
            raise OdooError(f"Odoo 回應不是 JSON-RPC 物件（收到 {type(body).__name__}）")
        return body

    def _map_connection_error(self, exc: BaseException) -> None:
        """把 `urlopen` 丟出的連線層例外轉成 `OdooError` 家族之一。

        `HTTPError` 是 `URLError` 的子類別，這裡用單一 `isinstance`
        判斷式而不是兩段依序比對的 `except HTTPError` / `except
        URLError`——早先的寫法一旦順序被改動，`HTTPError`（例如 429）
        就會先被寬鬆的 `URLError` 分支接走，誤判成「連線 Odoo 失敗」
        而不是限流，Task 11 的退避邏輯就完全失效。抽成單一純函式後，
        這個順序疑慮變成一行可以直接單元測試的邏輯，不必
        monkeypatch `urlopen`（本地建構一個 `HTTPError` 例外物件只是
        建物件，不是假扮 Odoo 回話）。
        """
        if isinstance(exc, urllib.error.HTTPError):
            # 只取狀態碼與 reason phrase，不呼叫 exc.read()——那是伺服器
            # 回應本文，會不會回顯請求內容不確定，不冒這個風險；也絕不
            # 把 self.password 塞進這裡任何字串。
            self._raise_for_http_status(exc.code, exc.reason or "")
            return
        if isinstance(exc, urllib.error.URLError):
            # exc.reason 是連線層失敗原因（DNS 解析失敗、連線被拒絕、送出
            # 請求階段逾時……），不含任何請求內容，也不含 self.password。
            raise OdooError(f"連線 Odoo 失敗：{exc.reason}") from None
        # 🔴 逾時**不一定**是 URLError。CPython 的
        # `urllib.request.AbstractHTTPHandler.do_open` 只把「送出請求」那一段
        # 包起來（`except OSError as err: raise URLError(err)`），緊接著的
        # `r = h.getresponse()` 與後續的 `response.read()` 都在那個 try 之外
        # ——它們逾時時拋的是裸 `TimeoutError`（`socket.timeout is
        # TimeoutError`），而 `TimeoutError` 不是 `URLError` 的子類別。
        #
        # 而「TCP 連得上、應用層不回」——也就是 timeout 欄位註解自己寫的那個
        # 情境——保證連線與送出都成功、卡在 getresponse()，也就保證走到這一條。
        # 少了這個分支，加了 timeout 只解決「不會無限期掛住」，沒解決「掛住
        # 之後要能被 Task 11 接住並轉成 E-4001」。
        #
        # 回應本文被截斷則是 `http.client.IncompleteRead`（`HTTPException` 那
        # 一支，同樣不是 URLError）。兩者都在這裡收斂。
        # 訊息只帶例外類別名稱：`str(TimeoutError)` 之類的內容由連線層決定，
        # 不含請求內容，但類別名稱已足夠定位，也絕不含 self.password。
        raise OdooError(f"連線 Odoo 失敗：{type(exc).__name__}") from None

    def _send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """把 `payload` POST 到 `{url}/jsonrpc`，回傳解析後的 JSON body。

        `self.url` 是 base URL（見欄位註解），這裡自己接上 `/jsonrpc`
        ——這是這個模組唯一該知道的路由細節，不該要求每個呼叫端都記得。

        這是這個檔案裡唯一真的碰網路的方法：不重試、不退避、不批次
        ——出錯只管把例外類型轉對，要不要再試一次是 loader.py 的責任。
        本機不對這個方法起假 HTTP server 測試（Global Constraints 第
        2 條禁止），它的正確性留給 Task 12 的 CI 整合測試驗證。
        """
        # scheme 檢查住在 `_endpoint()` 裡（見該函式 docstring：放在這裡的話
        # 守衛量不準），這裡呼叫它就會順帶擋下 file://、ftp:// 這類 URL，
        # 而且是在 urlopen 之前。
        endpoint = self._endpoint()
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                raw = response.read()
        except (OSError, http.client.HTTPException) as exc:
            # 攔截範圍刻意比 `urllib.error.URLError` 寬：`URLError` 本身就是
            # `OSError` 的子類別，而 `TimeoutError`（等回應／讀本文階段逾時）
            # 也是 `OSError`、卻**不是** `URLError`；`IncompleteRead`
            # （回應被截斷）則在 `HTTPException` 那一支。只接 `URLError` 會
            # 讓這兩種漏出去變成 Task 11 接不住的裸例外——見
            # `_map_connection_error` 的說明。
            self._map_connection_error(exc)
            raise AssertionError(
                "unreachable：_map_connection_error 三個分支都必定拋例外"
            ) from None
        return self._parse_response(raw)

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

        `result` 鍵可能缺席（見 `_extract_result` docstring），這裡不
        直接 `body["result"]`，改交給那個純函式處理。
        """
        if self._uid is None:
            self.authenticate()
        payload = self._build_payload(model, method, args, kwargs, uid=self._uid)
        body = self._send(payload)
        self._raise_for_error(body)
        return self._extract_result(body)
