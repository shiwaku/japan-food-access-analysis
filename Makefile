.PHONY: all mesh validate nationwide clean

# 既定は検証3県（加重カバー率の worst / worst / best）
PREFS ?= 高知県 島根県 宮城県
FOOD_STORES ?= input/food_store_master_atp_permit.parquet
OUT_SUFFIX ?= _ATP許可基準

ALL_PREFS = 北海道 青森県 岩手県 宮城県 秋田県 山形県 福島県 茨城県 栃木県 群馬県 \
            埼玉県 千葉県 東京都 神奈川県 新潟県 富山県 石川県 福井県 山梨県 長野県 \
            岐阜県 静岡県 愛知県 三重県 滋賀県 京都府 大阪府 兵庫県 奈良県 和歌山県 \
            鳥取県 島根県 岡山県 広島県 山口県 徳島県 香川県 愛媛県 高知県 福岡県 \
            佐賀県 長崎県 熊本県 大分県 宮崎県 鹿児島県 沖縄県

all: mesh validate

# Step 1: 125mメッシュ人口（e-Stat 統計GIS T001225・appId不要・キャッシュあり）
mesh:
	python scripts/01_fetch_mesh_population.py $(PREFS)

# Step 2: 圏外率算出＋農水省 表5 突合
validate:
	FOOD_STORES=$(FOOD_STORES) OUT_SUFFIX=$(OUT_SUFFIX) \
	    python scripts/02_validate_access_difficulty.py $(PREFS)

# 47都道府県（issue #2）。全国 2,820,831 メッシュ・1,740市区町村。
# メッシュ人口は1次メッシュ151個、境界は47県分をダウンロードしてキャッシュする。
nationwide:
	$(MAKE) mesh     PREFS="$(ALL_PREFS)"
	$(MAKE) validate PREFS="$(ALL_PREFS)" OUT_SUFFIX="$(OUT_SUFFIX)_47県"

clean:
	rm -rf data/mesh data/boundary
