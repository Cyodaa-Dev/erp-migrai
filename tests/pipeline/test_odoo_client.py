"""`pipeline/odoo_client.py` 的行為契約。

⚠️ **誠實界線**：本檔案只測「不需要 Odoo 存在」的部分——封包組裝、錯誤
映射、密碼不外洩、架構鐵律。「Odoo 會不會接受這個封包」一律留給 Task 12
的 CI 整合測試；本檔案不起任何本機 HTTP server 假扮 Odoo，也不
monkeypatch `urllib.request.urlopen`——那等於自己寫了一個 fake Odoo。
因此 `_send()`／`authenticate()` 整個網路往返、`execute_kw()` 整個網路
往返都沒有對應的單元測試：會被測到的邏輯已經抽成 `_build_payload`／
`_build_login_payload`／`_raise_for_error`／`_extract_uid`／
`_raise_for_http_status` 五個純函式（不碰網路），這裡逐一釘住。

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


def test_raise_for_error_data_present_but_no_message_falls_back():
    """`data` 存在但沒有 `message` 鍵——一樣退回 `error.message`。"""
    client = _client()
    with pytest.raises(OdooError) as exc:
        client._raise_for_error(
            {"error": {"data": {"debug": "traceback..."}, "message": "generic message"}}
        )
    assert "generic message" in str(exc.value)


def test_raise_for_error_data_debug_never_leaks_into_message():
    """`data.debug`（伺服器端完整 traceback）絕不能混進例外訊息。"""
    client = _client()
    with pytest.raises(OdooError) as exc:
        client._raise_for_error(
            {"error": {"data": {"message": "clean message", "debug": "TRACEBACK-MARKER"}}}
        )
    assert "TRACEBACK-MARKER" not in str(exc.value)


def test_raise_for_error_completely_empty_error_does_not_raise_keyerror():
    """`error` 鍵存在但整個是空 dict——仍要拋 OdooError，不能是 KeyError。"""
    client = _client()
    with pytest.raises(OdooError):
        client._raise_for_error({"error": {}})


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

_NETWORK_MODULES = ("urllib", "http.client", "socket", "requests")
_ALLOWED_FILE = "odoo_client.py"


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


def test_only_odoo_client_imports_network_modules():
    """架構鐵律：`odoo_client.py` 是唯一知道 HTTP／socket 存在的檔案。

    這條測試掃描 `src/erp_migrai/` 底下每一個 `.py`，任何檔案（除了
    `odoo_client.py` 本身）匯入 `urllib`／`http.client`／`socket`／
    `requests`，就代表架構鐵律已經被破壞——不再靠人記得手動跑
    `grep -rn "urllib\\|http\\.client\\|socket\\|requests" src/erp_migrai/`。
    """
    src_root = Path(__file__).resolve().parents[2] / "src" / "erp_migrai"
    offenders = []
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _imports_network_module(_imported_module_names(tree)) and path.name != _ALLOWED_FILE:
            offenders.append(str(path.relative_to(src_root)))
    assert offenders == [], f"這些檔案不該匯入網路模組：{offenders}"


def test_odoo_client_itself_does_import_network_module():
    """反向釘住：確認 `odoo_client.py` 真的用了 `urllib`——不是白名單放假的。

    若哪天 `odoo_client.py` 改用其他方式送 HTTP（理論上不該發生，因為
    Global Constraints 禁止引入 `requests`），這條測試會提醒白名單本身
    是不是也該檢討。
    """
    src_root = Path(__file__).resolve().parents[2] / "src" / "erp_migrai"
    path = src_root / "pipeline" / _ALLOWED_FILE
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert _imports_network_module(_imported_module_names(tree))
