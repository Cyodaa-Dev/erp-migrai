.DEFAULT_GOAL := help
.PHONY: help install lint format test test-all update-golden demo demo-full corpus corpus-dirty

OUT ?= out/demo

# demo-full 專用：連線一台真的 Odoo 17 CE（CI 的 integration job 會覆蓋這三個
# 變數；本機貢獻者要跑的話自己起一台 Odoo 後 export，或在指令前面覆蓋，例如
# `MIGRAI_ODOO_URL=http://localhost:8069 make demo-full`）。密碼刻意不在這裡
# 給預設值——P3-2：密碼只從 MIGRAI_ODOO_PASSWORD 環境變數或 --password-stdin
# 讀，Makefile 不該替使用者決定密碼，未設定時 CLI 自己會印出清楚的錯誤訊息。
MIGRAI_ODOO_URL ?= http://localhost:8069
MIGRAI_ODOO_DB ?= migrai
MIGRAI_ODOO_USER ?= admin

help:  ## 顯示可用指令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install:  ## 安裝開發環境
	uv sync

lint:  ## 靜態檢查與格式檢查
	uv run ruff check src tests
	uv run ruff format --check src tests

format:  ## 自動格式化並修正可自動修的問題
	uv run ruff format src tests
	uv run ruff check --fix src tests

test:  ## 單元 + golden 測試（跳過需要 docker 的整合測試）
	uv run pytest -v

test-all:  ## 全部測試，含整合測試
	uv run pytest -v -m ""

# 這個目標的每一行都用 @ 靜音：scan/map 的原始指令回聲會蓋掉下面刻意
# 排版的 ✓ 摘要，讓 demo 看起來像雜訊而非成果。其他目標沒有這個顧慮，
# 所以維持 make 預設會回聲指令的行為。
demo:  ## 對合成語料跑一次 scan → map，五分鐘看到第一個結果
	@uv run migrai scan --src corpus/case01_partner/src --out "$(OUT)"
	@uv run migrai map --inventory "$(OUT)/inventory.json" --out "$(OUT)"
	@echo ""
	@echo "  → $(OUT)/mapping.csv   ← 用試算表打開這份"
	@echo ""
	@echo "這份語料是合成的（synthetic: true）。"
	@echo "想試自己的 .4gl：uv run migrai scan --src <你的目錄> --out <輸出目錄>"

update-golden:  ## 重新產生 corpus 的 golden 檔（diff 必須人工審過才 commit）
	uv run python -m erp_migrai.tools.update_golden

corpus:  ## 由 schema.sql 與 seed.sql 產生各 case 的 data.sqlite（不進 git）
	uv run python -m erp_migrai.tools.build_corpus

# 髒資料版：示範「誠實報錯」用。刻意與 corpus 分成兩個目標而不是讓
# corpus 順手把兩份都建出來——`make demo-full` 依賴 corpus，若那個目標
# 也產出 data_dirty.sqlite，示範前的準備動作就會多出一份「長得很像正式
# 來源、內容卻是故意弄壞的」檔案躺在同一個目錄，按錯 --source 的代價
# 是把髒資料寫進示範用的 Odoo。要用它就得自己明講一次。
corpus-dirty:  ## 由 schema.sql 與 seed_dirty.sql 產生 data_dirty.sqlite（不進 git；示範報錯用）
	uv run python -m erp_migrai.tools.build_corpus --dirty

# 依賴 corpus：demo-full 讀的是 case03_sales_order/data/data.sqlite，這份
# 二進位檔不進 git（見 corpus 目標），沒有先跑過 corpus 就不存在。
#
# 四段管線缺一不可：scan → map 沒有外部依賴，load → verify 才是這個目標
# 存在的理由——證明前面十一個任務的產出真的能匯入一台真 Odoo，而不只是
# 通過單元測試。此目標**不靜音**指令回聲（跟 demo 不同）：demo-full 的
# 使用者已經在跑一台 Odoo，不是五分鐘上手情境，看得到每一步在做什麼更
# 重要。
demo-full: corpus  ## 對合成語料跑完四段（需要一台 Odoo 17 CE；CI 的 integration job 跑這個）
	uv run migrai scan --src corpus/case03_sales_order/src --out "$(OUT)"
	uv run migrai map --inventory "$(OUT)/inventory.json" --out "$(OUT)"
	uv run migrai load \
		--mapping "$(OUT)/mapping.json" \
		--source corpus/case03_sales_order/data/data.sqlite \
		--source-id case03 \
		--odoo-url "$(MIGRAI_ODOO_URL)" \
		--odoo-db "$(MIGRAI_ODOO_DB)" \
		--odoo-user "$(MIGRAI_ODOO_USER)" \
		--out "$(OUT)"
	uv run migrai verify \
		--mapping "$(OUT)/mapping.json" \
		--source corpus/case03_sales_order/data/data.sqlite \
		--log "$(OUT)/migration_log.jsonl" \
		--source-id case03 \
		--odoo-url "$(MIGRAI_ODOO_URL)" \
		--odoo-db "$(MIGRAI_ODOO_DB)" \
		--odoo-user "$(MIGRAI_ODOO_USER)" \
		--out "$(OUT)"
