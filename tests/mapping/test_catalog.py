"""catalog.load_catalog 的測試。

覆蓋兩類案例：內建 catalog 的內容正確性，以及驗證器對各種畸形輸入的
「大聲失敗」行為——後者是重點，見任務說明：讀入畸形 catalog 卻不出聲，
就會悄悄產出全盤皆錯的映射結果。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from erp_migrai.mapping.catalog import (
    ALLOWED_FIELD_TYPES,
    CatalogError,
    OdooField,
    OdooModel,
    load_catalog,
)

EXPECTED_MODELS = {
    "res.partner",
    "product.template",
    "product.product",
    "sale.order",
    "sale.order.line",
    # 20260806 隨 case04_subcontract（委外加工）加入，見 corpus/case04_subcontract/README.md
    "mrp.workcenter",
    "mrp.production",
    "purchase.order",
    "purchase.order.line",
}


def test_builtin_catalog_loads_and_has_expected_models():
    catalog = load_catalog()
    assert set(catalog) == EXPECTED_MODELS
    for name, model in catalog.items():
        assert isinstance(model, OdooModel)
        assert model.name == name


def test_res_partner_name_is_char_and_not_field_level_required():
    # 20260806 對 Odoo 19 真機 `fields_get` 查證：`res.partner.name.required`
    # 是 **False**——Odoo 用 SQL constraint 而不是欄位層 required 來管它。
    # 舊版斷言寫 True，是照著人工抄寫的 odoo17_catalog.yaml 寫的，那筆記載是錯的。
    catalog = load_catalog()
    name_field = catalog["res.partner"].fields["name"]
    assert isinstance(name_field, OdooField)
    assert name_field.type == "char"
    assert name_field.required is False


def test_res_partner_credit_limit_module_is_account_not_base():
    catalog = load_catalog()
    credit_limit = catalog["res.partner"].fields["credit_limit"]
    assert credit_limit.module == "account"
    assert credit_limit.module != "base"


def test_res_partner_rank_fields_module_is_account_not_base():
    # customer_rank／supplier_rank 是 account 模組加到 res.partner 上的
    # （見 odoo/odoo 17.0 分支 addons/account/models/partner.py 第 605～606
    # 行），一個沒裝 account 的乾淨 Odoo 上沒有這兩個欄位。
    catalog = load_catalog()
    customer_rank = catalog["res.partner"].fields["customer_rank"]
    supplier_rank = catalog["res.partner"].fields["supplier_rank"]
    assert customer_rank.module == "account"
    assert supplier_rank.module == "account"


def test_product_product_exists_with_expected_fields():
    """catalog 必須含 product.product，且欄位集合與型別符合預期。

    這條測試守的是 Task 4 的規則檔：sale_order.yaml 的 target 指到
    product.product 的欄位，catalog 少一個欄位規則檔就會在載入時炸掉。
    """
    catalog = load_catalog()
    model = catalog["product.product"]
    assert set(model.fields) == {
        "name",
        "default_code",
        "barcode",
        "list_price",
        "standard_price",
        "active",
    }
    assert model.fields["name"].type == "char"
    assert model.fields["name"].required is True
    assert model.fields["list_price"].type == "float"
    assert model.fields["default_code"].module == "product"


def test_sale_order_line_required_flags_match_odoo_source():
    """釘住兩處與 Odoo 17.0 原始碼對照過的 required 判定。

    這條測試的存在理由是：這兩個值在 2026-07-29 之前是錯的，且錯的方向會
    讓載入階段在寫入時才用看不懂的錯誤炸出來。把它們釘成測試，是為了讓
    「有人憑印象改回去」這件事會紅燈。
    """
    catalog = load_catalog()
    fields = catalog["sale.order.line"].fields
    # sale_order_line.py:115-119 —— required=True
    assert fields["product_uom_qty"].required is True
    # sale_order_line.py:80-84 —— 欄位定義沒有 required=True
    assert fields["product_id"].required is False


def test_model_may_legitimately_have_no_required_field_in_the_curated_subset():
    # catalog 收的是**策展子集**，不是模型的全部欄位。`res.partner` 與
    # `mrp.workcenter` 在 Odoo 19 都有 required 欄位（前者 autopost_bills／
    # group_rfq／group_on，後者 resource_id／sequence／currency_id），但沒有
    # 一個落在我們會映射的子集裡。所以「子集裡沒有必填欄位」是合法狀態，
    # 不是整理疏漏——舊的斷言與 catalog.py 裡對應的檢查已於 20260806 移除。
    catalog = load_catalog()
    without_required = [
        name
        for name, model in catalog.items()
        if not any(f.required for f in model.fields.values())
    ]
    assert "res.partner" in without_required


def test_every_field_has_module_and_nonempty_help():
    catalog = load_catalog()
    for model_name, model in catalog.items():
        for field_name, f in model.fields.items():
            assert f.module, f"{model_name}.{field_name} 缺少 module"
            assert f.help.strip(), f"{model_name}.{field_name} 缺少 help"


def test_custom_path_is_read_instead_of_builtin(tmp_path: Path):
    custom = tmp_path / "custom.yaml"
    custom.write_text(
        """
models:
  x.custom:
    description: "測試用自訂模型"
    fields:
      name:
        type: char
        required: true
        module: base
        help: "測試欄位"
""",
        encoding="utf-8",
    )
    catalog = load_catalog(custom)
    assert set(catalog) == {"x.custom"}
    assert catalog["x.custom"].fields["name"].type == "char"


# ---------------------------------------------------------------------------
# 失敗路徑：每一種畸形輸入都必須是 CatalogError，且訊息點名是哪個模型／欄位。
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "bad.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_missing_type_raises_catalog_error_naming_model_and_field(tmp_path: Path):
    bad = _write(
        tmp_path,
        """
models:
  res.partner:
    description: "測試"
    fields:
      name:
        required: true
        module: base
        help: "姓名"
""",
    )
    with pytest.raises(CatalogError) as exc_info:
        load_catalog(bad)
    msg = str(exc_info.value)
    assert "res.partner" in msg
    assert "name" in msg


def test_model_name_without_dot_raises_catalog_error(tmp_path: Path):
    bad = _write(
        tmp_path,
        """
models:
  respartner:
    description: "測試"
    fields:
      name:
        type: char
        required: true
        module: base
        help: "姓名"
""",
    )
    with pytest.raises(CatalogError) as exc_info:
        load_catalog(bad)
    assert "respartner" in str(exc_info.value)


def test_unknown_type_raises_catalog_error(tmp_path: Path):
    bad = _write(
        tmp_path,
        """
models:
  res.partner:
    description: "測試"
    fields:
      name:
        type: varchar
        required: true
        module: base
        help: "姓名"
""",
    )
    with pytest.raises(CatalogError) as exc_info:
        load_catalog(bad)
    msg = str(exc_info.value)
    assert "res.partner" in msg
    assert "name" in msg
    assert "varchar" in msg


def test_fields_as_list_raises_catalog_error_not_attribute_error(tmp_path: Path):
    bad = _write(
        tmp_path,
        """
models:
  res.partner:
    description: "測試"
    fields:
      - name
      - ref
""",
    )
    with pytest.raises(CatalogError) as exc_info:
        load_catalog(bad)
    assert "res.partner" in str(exc_info.value)


def test_field_value_as_string_raises_catalog_error(tmp_path: Path):
    bad = _write(
        tmp_path,
        """
models:
  res.partner:
    description: "測試"
    fields:
      name: "char"
""",
    )
    with pytest.raises(CatalogError) as exc_info:
        load_catalog(bad)
    msg = str(exc_info.value)
    assert "res.partner" in msg
    assert "name" in msg


def test_empty_file_raises_catalog_error(tmp_path: Path):
    bad = _write(tmp_path, "")
    with pytest.raises(CatalogError) as exc_info:
        load_catalog(bad)
    assert str(bad) in str(exc_info.value)


def test_valid_yaml_but_not_a_mapping_raises_catalog_error(tmp_path: Path):
    bad = _write(tmp_path, "- a\n- b\n- c\n")
    with pytest.raises(CatalogError) as exc_info:
        load_catalog(bad)
    assert str(bad) in str(exc_info.value)


def test_models_key_not_a_mapping_raises_catalog_error(tmp_path: Path):
    bad = _write(tmp_path, "models: not-a-mapping\n")
    with pytest.raises(CatalogError) as exc_info:
        load_catalog(bad)
    assert str(bad) in str(exc_info.value)


def test_model_with_no_required_field_is_accepted(tmp_path: Path):
    # 20260806 起不再視為錯誤：catalog 是策展子集，子集裡沒有必填欄位很正常
    # （見 catalog._parse_model 的說明）。這個測試從「必須拋錯」翻轉成
    # 「必須順利載入」，翻轉本身就是這項行為變更的紀錄。
    ok = _write(
        tmp_path,
        """
models:
  res.partner:
    description: "測試"
    fields:
      ref:
        type: char
        required: false
        module: base
        help: "參照"
""",
    )
    catalog = load_catalog(ok)
    assert set(catalog) == {"res.partner"}
    assert catalog["res.partner"].fields["ref"].required is False


def test_missing_module_raises_catalog_error(tmp_path: Path):
    bad = _write(
        tmp_path,
        """
models:
  res.partner:
    description: "測試"
    fields:
      name:
        type: char
        required: true
        help: "姓名"
""",
    )
    with pytest.raises(CatalogError) as exc_info:
        load_catalog(bad)
    msg = str(exc_info.value)
    assert "res.partner" in msg
    assert "name" in msg


# ---------------------------------------------------------------------------
# 重複 key：`yaml.safe_load` 預設會悄悄保留最後一次出現、蓋掉前面的定義，
# 完全不出聲。這正是本任務要堵住的洞——複製貼上一個模型或欄位區塊時打錯
# 名稱，前面整段定義會人間蒸發卻沒有任何錯誤。
# ---------------------------------------------------------------------------


def test_duplicate_model_key_raises_catalog_error_naming_the_key(tmp_path: Path):
    bad = _write(
        tmp_path,
        """
models:
  res.partner:
    description: "第一次出現"
    fields:
      name:
        type: char
        required: true
        module: base
        help: "姓名"
  res.partner:
    description: "第二次出現，重複的模型 key"
    fields:
      ref:
        type: char
        required: true
        module: base
        help: "參照"
""",
    )
    with pytest.raises(CatalogError) as exc_info:
        load_catalog(bad)
    msg = str(exc_info.value)
    assert "res.partner" in msg


def test_duplicate_field_key_raises_catalog_error_naming_the_key(tmp_path: Path):
    bad = _write(
        tmp_path,
        """
models:
  res.partner:
    description: "測試"
    fields:
      name:
        type: char
        required: true
        module: base
        help: "第一次出現"
      name:
        type: char
        required: true
        module: base
        help: "第二次出現，重複的欄位 key"
""",
    )
    with pytest.raises(CatalogError) as exc_info:
        load_catalog(bad)
    msg = str(exc_info.value)
    assert "name" in msg


def test_valid_catalog_with_no_duplicates_still_loads(tmp_path: Path):
    # 防止過度嚴格：沒有任何重複 key 的合法 catalog 必須正常載入，不能被
    # 重複 key 檢查誤傷。
    good = _write(
        tmp_path,
        """
models:
  res.partner:
    description: "測試用模型"
    fields:
      name:
        type: char
        required: true
        module: base
        help: "姓名"
      ref:
        type: char
        required: false
        module: base
        help: "參照"
  product.template:
    description: "另一個測試用模型"
    fields:
      name:
        type: char
        required: true
        module: product
        help: "產品名稱"
""",
    )
    catalog = load_catalog(good)
    assert set(catalog) == {"res.partner", "product.template"}
    assert set(catalog["res.partner"].fields) == {"name", "ref"}


def test_allowed_field_types_is_exactly_the_closed_set():
    # 這個集合是刻意釘死的：新增一個型別必須同時在 typing_rules._COMPATIBILITY
    # 的每一個來源家族補上對應的一格，改這裡等於宣告那件事已經做完。
    # 20260806 新增 `html`——Odoo 19 把 res.partner.comment /
    # product.template.description / sale.order.note 從 text 改成 html。
    expected = frozenset(
        {
            "char",
            "text",
            "html",
            "integer",
            "float",
            "monetary",
            "boolean",
            "date",
            "datetime",
            "selection",
            "many2one",
            "one2many",
            "many2many",
        }
    )
    assert expected == ALLOWED_FIELD_TYPES
