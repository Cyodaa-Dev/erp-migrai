"""`pipeline/odoo_client.py` 的行為契約。

⚠️ **誠實界線**：本檔案只測「不需要 Odoo 存在」的部分——封包組裝、錯誤
映射、密碼不外洩、架構鐵律。「Odoo 會不會接受這個封包」一律留給 Task 12
的 CI 整合測試；本檔案不起任何本機 HTTP server 假扮 Odoo，也不
monkeypatch `urllib.request.urlopen`——那等於自己寫了一個 fake Odoo。
因此 `_send()`／`authenticate()` 整個網路往返、`execute_kw()` 整個網路
往返都沒有對應的單元測試：會被測到的邏輯已經抽成 `_build_payload`／
`_build_login_payload`／`_raise_for_error`／`_extract_uid`／
`_extract_result`／`_raise_for_http_status`／`_endpoint`／
`_parse_response`／`_map_connection_error` 九個純函式（不碰網路），
這裡逐一釘住。

本地建構 `urllib.error.HTTPError`／`URLError` 例外物件（不呼叫
`urlopen`）餵給 `_map_connection_error` 是允許的——那只是建一個例外
物件，不是假扮 Odoo 回話。

## 密碼字串選擇

fixture 一律用 `"s3cret"` 而非任一字元的值——像 `"p"` 這種短字串幾乎必然
出現在 `http`／`payload` 等常見詞彙裡，會讓「有洩漏」與「沒洩漏」分辨不
出來。`"s3cret"` 不會巧合出現在 url／db／user／model 名稱或任何 Odoo 的
內建詞彙裡。

## 為什麼 `authenticate()` 失敗不是拋例外，是 `result: false`

`odoo/service/common.py` 的 `exp_authenticate`：帳密錯時
`except AccessDenied: return False`，所以那次 JSON-RPC 呼叫本身仍然
「成功」（沒有 `error` 鍵），只是 `result` 的值是 `False`。
`_extract_uid()` 因此必須自己檢查 `result` 的真假值，不能只靠
`_raise_for_error()` 判斷——`test_extract_uid_raises_auth_error_on_false_result`
直接釘住這件事。
"""

from __future__ import annotations

import ast
import urllib.error
from pathlib import Path

import pytest

from erp_migrai.pipeline.odoo_client import (
    OdooAuthError,
    OdooClient,
    OdooError,
    RateLimited,
)

PASSWORD = "s3cret"


def _client(**overrides: object) -> OdooClient:
    kwargs: dict[str, object] = {
        "url": "http://x",
        "db": "d",
        "user": "u",
        "password": PASSWORD,
    }
    kwargs.update(overrides)
    return OdooClient(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 封包組裝：_build_payload / _build_login_payload
# --------------------------------------------------------------------------


def test_payload_shape_matches_jsonrpc_execute_kw():
    client = OdooClient(url="http://x", db="d", user="u", password="p")
    payload = client._build_payload("res.partner", "search_read", [[]], {"fields": ["id"]}, uid=2)
    assert payload["jsonrpc"] == "2.0"
    assert payload["method"] == "call"
    assert payload["params"]["service"] == "object"
    assert payload["params"]["method"] == "execute_kw"
    assert payload["params"]["args"][:3] == ["d", 2, "p"]
    assert payload["params"]["args"][3] == "res.partner"
    assert payload["params"]["args"][4] == "search_read"


def test_build_payload_args_full_shape_including_kwargs():
    """任務書的測試只驗到 args[4]；補上第 5、6 個位置（args、kwargs）。

    `dispatch()`／`execute_kw()` 的簽章是
    `execute_kw(db, uid, obj, method, args, kw=None)`，第 5、6 個位置
    (index 5、6) 對應呼叫端傳的 `args`、`kwargs`——不驗到這兩格，
    payload 少帶或多帶一格都不會被抓到。
    """
    client = _client()
    payload = client._build_payload(
        "res.partner", "search_read", [[("id", "=", 1)]], {"fields": ["id"]}, uid=7
    )
    args = payload["params"]["args"]
    assert len(args) == 7
    assert args == [
        "d",
        7,
        PASSWORD,
        "res.partner",
        "search_read",
        [[("id", "=", 1)]],
        {"fields": ["id"]},
    ]


def test_build_payload_default_kwargs_is_none():
    """`kwargs` 省略時，payload 裡原樣是 `None`，不是被改寫成 `{}`。

    Odoo 自己的 `execute_kw(db, uid, obj, method, args, kw=None)` 預設值
    就是 `None`，伺服器端用 `kw or {}` 處理——這裡沒有理由多做一層轉換。
    """
    client = _client()
    payload = client._build_payload("res.partner", "search_read", [[]], uid=1)
    assert payload["params"]["args"][6] is None


def test_build_login_payload_shape_matches_common_login():
    """`common.login` 的 args 順序是 `[db, user, password]`

    （`odoo/service/common.py` 的 `exp_login(db, login, password)`）。
    """
    client = _client()
    payload = client._build_login_payload()
    assert payload["jsonrpc"] == "2.0"
    assert payload["params"]["service"] == "common"
    assert payload["params"]["method"] == "login"
    assert payload["params"]["args"] == ["d", "u", PASSWORD]


# --------------------------------------------------------------------------
# 錯誤映射：_raise_for_error
# --------------------------------------------------------------------------


def test_jsonrpc_error_becomes_odoo_error_with_odoo_message():
    client = OdooClient(url="http://x", db="d", user="u", password="s3cret")
    body = {"error": {"data": {"message": "Record does not exist"}}}
    with pytest.raises(OdooError) as exc:
        client._raise_for_error(body)
    assert "Record does not exist" in str(exc.value)
    assert type(exc.value) is OdooError


def test_password_never_appears_in_error_messages():
    """密碼進了 log 或例外訊息，就等於進了 CI 的公開輸出。

    P3-2 已經把密碼擋在 argv 之外，這條守的是另一半——擋在輸出之外。
    """
    client = OdooClient(url="http://x", db="d", user="u", password="s3cret")
    body = {"error": {"data": {"message": "boom"}}}
    with pytest.raises(OdooError) as exc:
        client._raise_for_error(body)
    assert "s3cret" not in str(exc.value)
    assert "s3cret" not in repr(exc.value)
    assert type(exc.value) is OdooError


def test_raise_for_error_noop_when_no_error_key():
    """正常回應（沒有 `error` 鍵）什麼都不做，不誤判成錯誤。"""
    client = _client()
    client._raise_for_error({"result": 42})  # 不應該拋任何例外


def test_raise_for_error_missing_data_falls_back_to_error_message():
    """`error` 有，但沒有 `data` 鍵——退回 `error.message`，不噴 KeyError。"""
    client = _client()
    with pytest.raises(OdooError) as exc:
        client._raise_for_error({"error": {"message": "generic top-level message"}})
    assert "generic top-level message" in str(exc.value)
    assert type(exc.value) is OdooError


def test_raise_for_error_data_present_but_no_message_falls_back():
    """`data` 存在但沒有 `message` 鍵——一樣退回 `error.message`。"""
    client = _client()
    with pytest.raises(OdooError) as exc:
        client._raise_for_error(
            {"error": {"data": {"debug": "traceback..."}, "message": "generic message"}}
        )
    assert "generic message" in str(exc.value)
    assert type(exc.value) is OdooError


def test_raise_for_error_data_debug_never_leaks_into_message():
    """`data.debug`（伺服器端完整 traceback）絕不能混進例外訊息。"""
    client = _client()
    with pytest.raises(OdooError) as exc:
        client._raise_for_error(
            {"error": {"data": {"message": "clean message", "debug": "TRACEBACK-MARKER"}}}
        )
    assert "TRACEBACK-MARKER" not in str(exc.value)
    assert type(exc.value) is OdooError


def test_raise_for_error_completely_empty_error_does_not_raise_keyerror():
    """`error` 鍵存在但整個是空 dict——仍要拋 OdooError，不能是 KeyError。"""
    client = _client()
    with pytest.raises(OdooError) as exc:
        client._raise_for_error({"error": {}})
    assert type(exc.value) is OdooError


def test_raise_for_error_default_message_is_not_literal_none():
    """L1：拿掉 `or "Odoo 回傳未知錯誤"` 這個預設值後，訊息會退化成
    Python 印出 `None` 的字面樣子 `"None"`——這裡釘住訊息真的是有意義
    的中文提示，不是這個退化字串。
    """
    client = _client()
    with pytest.raises(OdooError) as exc:
        client._raise_for_error({"error": {}})
    assert str(exc.value) == "Odoo 回傳未知錯誤"


def test_raise_for_error_prefers_data_message_over_error_message_when_both_present():
    """Important 5：真實 Odoo 回應兩個鍵通常都有——`error.message` 固定是
    `"Odoo Server Error"`，`error.data.message` 才是真訊息。優先序一旦
    寫反，每一則錯誤都會退化成這個無資訊的固定字串。

    這個 fixture 刻意讓兩個鍵同時存在（先前的 fixture 都只給一個，
    Mutation D1 對調優先序因此能全綠存活），用真實 Odoo 回應的結構
    （`odoo/http.py` 的 `serialize_exception()`）核對過。
    """
    client = _client()
    body = {
        "error": {
            "code": 200,
            "message": "Odoo Server Error",
            "data": {
                "name": "odoo.exceptions.UserError",
                "message": "統一編號重複",
                "debug": "TRACEBACK",
            },
        }
    }
    with pytest.raises(OdooError) as exc:
        client._raise_for_error(body)
    assert "統一編號重複" in str(exc.value)
    assert "Odoo Server Error" not in str(exc.value)
    assert "TRACEBACK" not in str(exc.value)
    assert type(exc.value) is OdooError


def test_raise_for_error_data_not_a_dict_does_not_raise_attributeerror():
    """Mi4：`error.data` 不是 dict（協定被破壞）時要拋 `OdooError`，
    不能是裸 `AttributeError`。"""
    client = _client()
    with pytest.raises(OdooError) as exc:
        client._raise_for_error({"error": {"data": "boom", "message": "m"}})
    assert type(exc.value) is OdooError


def test_raise_for_error_error_not_a_dict_does_not_raise_attributeerror():
    """Mi4：`error` 本身不是 dict 時也要拋 `OdooError`，不能是裸
    `AttributeError`。"""
    client = _client()
    with pytest.raises(OdooError) as exc:
        client._raise_for_error({"error": "boom"})
    assert type(exc.value) is OdooError


def test_raise_for_error_access_denied_becomes_odoo_auth_error():
    """Mi2：`execute_kw` 每次 dispatch 都會跑 `security.check`，認證失效
    （session 過期、密碼被改）會拋 `odoo.exceptions.AccessDenied`——這是
    除了 `common.login` 回傳 `False` 之外，認證失敗的第二條路，
    Task 11 若把 E-4002 綁在 `except OdooAuthError` 上，漏掉這條就會
    誤判成普通資料錯誤。
    """
    client = _client()
    body = {
        "error": {
            "message": "Odoo Server Error",
            "data": {"name": "odoo.exceptions.AccessDenied", "message": "Session expired"},
        }
    }
    with pytest.raises(OdooAuthError) as exc:
        client._raise_for_error(body)
    assert type(exc.value) is OdooAuthError


def test_raise_for_error_user_error_stays_plain_odoo_error():
    """反例：普通 `UserError` 不該被誤判成 `OdooAuthError`。"""
    client = _client()
    body = {
        "error": {
            "message": "Odoo Server Error",
            "data": {"name": "odoo.exceptions.UserError", "message": "統一編號重複"},
        }
    }
    with pytest.raises(OdooError) as exc:
        client._raise_for_error(body)
    assert type(exc.value) is OdooError


# --------------------------------------------------------------------------
# 認證：_extract_uid（result: false 的邊界——不是拋例外，是假值）
# --------------------------------------------------------------------------


def test_extract_uid_raises_auth_error_on_false_result():
    """帳密錯時 Odoo 的 JSON-RPC 回應是 `{"result": false}`，不是 `error` 鍵。"""
    client = _client()
    with pytest.raises(OdooAuthError):
        client._extract_uid({"result": False})


def test_extract_uid_returns_uid_on_success():
    client = _client()
    assert client._extract_uid({"result": 7}) == 7


def test_extract_uid_auth_error_does_not_leak_password():
    client = _client(password=PASSWORD)
    with pytest.raises(OdooAuthError) as exc:
        client._extract_uid({"result": False})
    assert PASSWORD not in str(exc.value)
    assert PASSWORD not in repr(exc.value)


def test_extract_uid_server_error_is_odoo_error_not_auth_error():
    """Mi1：三條既有 `_extract_uid` 測試餵的都是 `{"result": ...}`，沒有
    一條餵含 `error` 鍵的 body。少了 `_raise_for_error` 那一行的保護，
    登入時的伺服器錯誤（例如資料庫不存在）會被講成「你密碼打錯」，
    Task 11 會發錯的錯誤碼（E-4002 而非 E-4001）。
    """
    client = _client()
    body = {"error": {"data": {"message": 'database "nope" does not exist'}}}
    with pytest.raises(OdooError) as exc:
        client._extract_uid(body)
    assert type(exc.value) is OdooError
    assert "does not exist" in str(exc.value)


# --------------------------------------------------------------------------
# HTTP 狀態碼映射：_raise_for_http_status
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 503])
def test_raise_for_http_status_rate_limited(status):
    client = _client()
    with pytest.raises(RateLimited):
        client._raise_for_http_status(status, "reason")


def test_raise_for_http_status_other_error_is_odoo_error_not_rate_limited():
    client = _client()
    with pytest.raises(OdooError) as exc:
        client._raise_for_http_status(500, "Internal Server Error")
    assert not isinstance(exc.value, RateLimited)


def test_rate_limited_is_a_subclass_of_odoo_error():
    """loader.py 若只 `except OdooError`，也要接得住 `RateLimited`。"""
    assert issubclass(RateLimited, OdooError)


def test_odoo_auth_error_is_a_subclass_of_odoo_error():
    assert issubclass(OdooAuthError, OdooError)


# --------------------------------------------------------------------------
# 連線層例外分流：_map_connection_error（純函式，接一個本地建構的例外
# 物件——不是 monkeypatch urlopen，也不是起假 HTTP server）
# --------------------------------------------------------------------------


def test_map_connection_error_http_error_429_is_rate_limited():
    client = _client()
    exc = urllib.error.HTTPError("http://x/jsonrpc", 429, "Too Many Requests", {}, None)
    with pytest.raises(RateLimited):
        client._map_connection_error(exc)


def test_map_connection_error_http_error_500_is_plain_odoo_error():
    """Mi6／M7：`HTTPError` 是 `URLError` 的子類別，分流順序一旦反過來，
    每一個 429 都會被寬鬆的 `URLError` 分支接走，變成「連線 Odoo 失敗」，
    Task 11 的退避完全失效。這裡釘住 500 這種一般錯誤仍走
    `_raise_for_http_status` 那條路（不是 `_map_connection_error` 自己
    當成連線失敗處理）。
    """
    client = _client()
    exc = urllib.error.HTTPError("http://x/jsonrpc", 500, "Internal Server Error", {}, None)
    with pytest.raises(OdooError) as exc_info:
        client._map_connection_error(exc)
    assert not isinstance(exc_info.value, RateLimited)


def test_map_connection_error_http_error_message_does_not_leak_password():
    """Mi6／F6：訊息只能帶狀態碼與 reason phrase，不能帶上 `self.password`。"""
    client = _client(password=PASSWORD)
    exc = urllib.error.HTTPError("http://x/jsonrpc", 500, "Internal Server Error", {}, None)
    with pytest.raises(OdooError) as exc_info:
        client._map_connection_error(exc)
    assert PASSWORD not in str(exc_info.value)


def test_map_connection_error_plain_url_error_is_odoo_error():
    """普通連線失敗（DNS 解析失敗、連線被拒絕……）沒有 `.code`，走
    `URLError` 那個分支，一樣要落在 `OdooError`，不能誤判成
    `RateLimited`。
    """
    client = _client()
    exc = urllib.error.URLError("Connection refused")
    with pytest.raises(OdooError) as exc_info:
        client._map_connection_error(exc)
    assert not isinstance(exc_info.value, RateLimited)
    assert "Connection refused" in str(exc_info.value)


# --------------------------------------------------------------------------
# 端點組裝：_endpoint（Important 3——url 是 base URL，這裡自己接 /jsonrpc）
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://localhost:8069", "http://localhost:8069/jsonrpc"),
        ("http://localhost:8069/", "http://localhost:8069/jsonrpc"),
        ("https://odoo.example.com", "https://odoo.example.com/jsonrpc"),
    ],
)
def test_endpoint_appends_jsonrpc_to_base_url(url, expected):
    client = _client(url=url)
    assert client._endpoint() == expected


# --------------------------------------------------------------------------
# 回應解析：_parse_response（Important 3——JSONDecodeError 不能逃出契約）
# --------------------------------------------------------------------------


def test_parse_response_returns_parsed_dict_on_valid_json():
    client = _client()
    assert client._parse_response(b'{"result": 1}') == {"result": 1}


def test_parse_response_wraps_json_decode_error_as_odoo_error():
    """`url` 給錯常見的結果是拿到 HTML（Odoo 對 `/` 的 303 轉址），
    `json.loads` 對此拋 `JSONDecodeError`（`ValueError` 子類，不在
    `OdooError` 家族裡）——這裡要接住，不能讓它逃出契約。
    """
    client = _client()
    with pytest.raises(OdooError):
        client._parse_response(b"<html><body>Not Found</body></html>")


def test_parse_response_json_decode_error_does_not_leak_raw_content():
    """訊息只能帶長度與解析失敗位置，絕不能把 `raw` 內容塞進去——回應
    本文有可能回顯了請求內容（含密碼）。
    """
    client = _client()
    raw = b"<html>s3cret-should-not-leak-here</html>"
    with pytest.raises(OdooError) as exc:
        client._parse_response(raw)
    assert "s3cret-should-not-leak-here" not in str(exc.value)


# --------------------------------------------------------------------------
# 逾時：Important 4——urlopen 預設永不逾時
# --------------------------------------------------------------------------


def test_default_timeout_is_thirty_seconds():
    client = _client()
    assert client.timeout == 30.0


def test_send_passes_self_timeout_to_urlopen():
    """用 AST 而非 monkeypatch 釘住 `_send` 真的把 `self.timeout` 傳給
    `urlopen`——誠實界線第 2 條禁止起假 HTTP server 或 monkeypatch
    `urlopen`，這裡只靜態檢查原始碼裡的呼叫式有沒有這個關鍵字引數，
    不執行任何網路呼叫。
    """
    src_root = Path(__file__).resolve().parents[2] / "src" / "erp_migrai"
    path = src_root / "pipeline" / "odoo_client.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    urlopen_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "urlopen"
    ]
    assert len(urlopen_calls) == 1
    (call,) = urlopen_calls
    timeout_kwargs = [kw for kw in call.keywords if kw.arg == "timeout"]
    assert len(timeout_kwargs) == 1
    (timeout_kw,) = timeout_kwargs
    assert isinstance(timeout_kw.value, ast.Attribute)
    assert timeout_kw.value.attr == "timeout"


# --------------------------------------------------------------------------
# URL scheme 驗證：Mi5——urlopen 對 file:// 一樣會動作
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad_url", ["ftp://x", "file:///etc/passwd", "javascript:alert(1)", ""])
def test_endpoint_rejects_non_http_scheme(bad_url):
    """非 http/https 的 scheme 必須被擋下——`urlopen` 對 `file://` 一樣會
    動作，而 `url` 來自使用者設定。

    🔴 這條測試刻意打在 `_endpoint()`（純函式）而不是 `_send()`。控制者
    複驗時實測過：檢查放在 `_send` 裡時，把整段檢查拿掉，`ftp://`／
    `file://`／`javascript:` 三個案例**仍然通過**——因為那些 URL 連不上，
    `urlopen` 拋的 `URLError` 照樣被轉成 `OdooError`，測試分辨不出「scheme
    被擋下」與「連線失敗」。四個案例裡只有空字串會紅，而且紅的原因是
    urllib 噴出一個不是 `OdooError` 的裸 `ValueError`——也就是說那條守衛
    連自己宣稱守的東西都沒守到。

    打在 `_endpoint()` 上就沒有這個歧義：它完全不碰網路，唯一會拋
    `OdooError` 的原因就是 scheme 不對。
    """
    client = _client(url=bad_url)
    with pytest.raises(OdooError) as exc:
        client._endpoint()
    assert "scheme" in str(exc.value)


# --------------------------------------------------------------------------
# execute_kw 的 result 鍵：Important 1——_extract_result（純函式）
# --------------------------------------------------------------------------


def test_extract_result_missing_result_key_returns_none_not_keyerror():
    """`odoo/http.py` 的 dispatch：`if result is not None:
    response['result'] = result`——方法回傳 `None` 時 body 根本不會有
    `result` 鍵。`account.move.button_draft()` 就是這種方法（最後一行
    `self.state = 'draft'`，沒有 `return`）。這裡釘住「沒有 `result`
    鍵」被當成「回傳 `None`」處理，不會噴裸 `KeyError`。
    """
    client = _client()
    body: dict[str, object] = {"jsonrpc": "2.0", "id": None}
    assert client._extract_result(body) is None


def test_extract_result_returns_result_value_when_present():
    client = _client()
    assert client._extract_result({"result": [1, 2, 3]}) == [1, 2, 3]


def test_extract_result_result_value_of_false_is_returned_as_is():
    """`result` 存在但值是 falsy（例如 `False`、`0`、`[]`）時要原樣回傳，
    不能跟「`result` 鍵缺席」混為一談——這裡故意檢查鍵是否存在，不是
    檢查值的真假。
    """
    client = _client()
    assert client._extract_result({"result": False}) is False


# --------------------------------------------------------------------------
# 密碼不外洩：repr()
# --------------------------------------------------------------------------


def test_client_repr_does_not_leak_password():
    """dataclass 預設產生的 `__repr__` 會印出每個欄位——密碼欄位標了
    `field(repr=False)`，否則任何 `logging.debug("%s", client)` 都會外洩。
    """
    client = _client(password=PASSWORD)
    assert PASSWORD not in repr(client)


# --------------------------------------------------------------------------
# 架構鐵律：只有 odoo_client.py 知道網路模組存在
# --------------------------------------------------------------------------

# Mi3：原本這份清單漏了 "http"（`from http import client` 匯入的模組名稱
# 就是裸的 "http"，不是 "http.client"）、"xmlrpc"（Odoo 官方另一套 RPC
# 用戶端，最可能被誤用來直接連 Odoo 的那一個）、"httpx"、"ssl"、
# "ftplib"——這五種匯入方式先前完全逃過這條鐵律的偵測。
_NETWORK_MODULES = (
    "urllib",
    "http",
    "http.client",
    "socket",
    "requests",
    "xmlrpc",
    "httpx",
    "ssl",
    "ftplib",
)
# Mi3：比對相對路徑而不是 basename——只比 basename 的話，在
# `src/erp_migrai/` 底下任何別的目錄放一個同名 `odoo_client.py` 內含
# `import urllib.request`，一樣能逃過這條鐵律（K1）。
_ALLOWED_FILE = "pipeline/odoo_client.py"


def _imported_module_names(tree: ast.Module) -> set[str]:
    """回傳一個檔案裡所有 `import`／`from ... import` 匯入的模組名稱。

    用 AST 而不是字串 grep：grep 連文件字串／註解裡提到「urllib」這個字
    也會誤判成違規（例如這個檔案自己的 docstring 大量提到 urllib）。
    AST 只看真正的 import 陳述式。
    """
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _imports_network_module(modules: set[str]) -> bool:
    return any(
        module == net or module.startswith(net + ".")
        for module in modules
        for net in _NETWORK_MODULES
    )


def _scan_network_imports(src_root: Path, allowed_relpath: str) -> list[str]:
    """掃描 `src_root` 底下每個 `.py`，回傳違反架構鐵律的檔案相對路徑。

    抽成獨立函式（而不是寫死在 `test_only_odoo_client_imports_network_modules`
    裡）是為了讓 `test_same_basename_in_other_directory_is_still_caught`
    可以用 `tmp_path` 餵一個假的目錄結構，驗證 Mi3（比相對路徑而非
    basename）真的生效，不必碰真正的 `src/erp_migrai/`。
    """
    offenders = []
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relpath = str(path.relative_to(src_root))
        if _imports_network_module(_imported_module_names(tree)) and relpath != allowed_relpath:
            offenders.append(relpath)
    return offenders


def test_only_odoo_client_imports_network_modules():
    """架構鐵律：`odoo_client.py` 是唯一知道 HTTP／socket 存在的檔案。

    這條測試掃描 `src/erp_migrai/` 底下每一個 `.py`，任何檔案（除了
    `pipeline/odoo_client.py` 本身）匯入 `urllib`／`http`／`socket`／
    `requests`／`xmlrpc`／`httpx`／`ssl`／`ftplib`，就代表架構鐵律已經
    被破壞——不再靠人記得手動跑
    `grep -rn "urllib\\|http\\|socket\\|requests\\|xmlrpc\\|httpx\\|ssl\\|ftplib" src/erp_migrai/`。
    """
    src_root = Path(__file__).resolve().parents[2] / "src" / "erp_migrai"
    offenders = _scan_network_imports(src_root, _ALLOWED_FILE)
    assert offenders == [], f"這些檔案不該匯入網路模組：{offenders}"


def test_odoo_client_itself_does_import_network_module():
    """反向釘住：確認 `odoo_client.py` 真的用了 `urllib`——不是白名單放假的。

    若哪天 `odoo_client.py` 改用其他方式送 HTTP（理論上不該發生，因為
    Global Constraints 禁止引入 `requests`），這條測試會提醒白名單本身
    是不是也該檢討。
    """
    src_root = Path(__file__).resolve().parents[2] / "src" / "erp_migrai"
    path = src_root / _ALLOWED_FILE
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert _imports_network_module(_imported_module_names(tree))


def test_same_basename_in_other_directory_is_still_caught(tmp_path):
    """K1：只比 basename 的話，別的目錄放一個同名 `odoo_client.py` 一樣
    能夾帶 import 逃過鐵律——這裡改比對相對路徑，用一個假的目錄結構
    （不碰真正的 `src/erp_migrai/`）釘住這個修正真的生效。
    """
    (tmp_path / "pipeline").mkdir()
    (tmp_path / "pipeline" / "odoo_client.py").write_text("import urllib.request\n")
    (tmp_path / "mapping").mkdir()
    (tmp_path / "mapping" / "odoo_client.py").write_text("import urllib.request\n")
    offenders = _scan_network_imports(tmp_path, "pipeline/odoo_client.py")
    assert offenders == ["mapping/odoo_client.py"]


@pytest.mark.parametrize(
    "source",
    [
        "from http import client\n",
        "import xmlrpc.client\n",
        "import ssl\n",
        "import ftplib\n",
        "import httpx\n",
    ],
)
def test_network_module_detection_catches_previously_missed_modules(source):
    """I7／I10／I11／I13／I15：白名單原本漏了 `http`／`xmlrpc`／`ssl`／
    `ftplib`／`httpx` 這五種匯入方式，都該被判定成違反架構鐵律。
    """
    tree = ast.parse(source)
    assert _imports_network_module(_imported_module_names(tree))
