.DEFAULT_GOAL := help
.PHONY: help install lint format test test-all update-golden demo corpus

OUT ?= out/demo

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
