.PHONY: all mesh validate clean

PREFS ?= 高知県 島根県 宮城県
FOOD_STORES ?= input/food_store_master_atp_permit.parquet
OUT_SUFFIX ?= _ATP許可基準

all: mesh validate

# Step 1: 125mメッシュ人口（e-Stat 統計GIS T001225・appId不要・キャッシュあり）
mesh:
	python scripts/01_fetch_mesh_population.py $(PREFS)

# Step 2: 圏外率算出＋農水省 表5 突合
validate:
	FOOD_STORES=$(FOOD_STORES) OUT_SUFFIX=$(OUT_SUFFIX) \
	    python scripts/02_validate_access_difficulty.py $(PREFS)

clean:
	rm -rf data/mesh data/boundary
