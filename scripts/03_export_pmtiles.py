#!/usr/bin/env python3
"""
mesh500m_out_*.parquet → GeoJSONL → PMTiles（tippecanoe）

姉妹リポジトリ **japan-transit-desert-analysis-125**（125mメッシュ版。`-125` の付かない
250m版・`-100` 版ではない）の `05_export_geojson.py` に合わせた作り。
違いは GeoJSON の作り方だけで、**geopandas を通さず DuckDB で GeoJSONL を直に書く**
（2,814,449 行を geopandas に載せると重いため。japan-food-store-master の
`scripts/build_pmtiles.sh` と同じ流儀）。

出すもの
--------
125mメッシュのポリゴン。座標は 11桁L6コードから計算した重心 ±半セル
（緯度 1/1920・経度 1/1280）で作る。**人口>0 のメッシュのみ**出力してサイズを抑える
（-125 の `gdf[gdf["pop_total"] > 0]` と同じ方針）。

**「500m圏外」であって「アクセス困難」ではない。** 困難＝圏外 かつ 自動車利用困難で、
後者をメッシュに割り当てる根拠が無い（係数を分解できていない）。プロパティ名も out_* のまま。

プロパティ（判定は道路距離だけ・2026-08-28）
--------------------------------------------
  out_d   全カテゴリ（S4）まで道路距離500m超か  ← 地図の赤
  out_sm  スーパー（S1）まで道路距離500m超か    ← 緑／橙の切り分け
  dmin    最寄り店舗までの徒歩分（S4）
  dsm     最寄りスーパーまでの徒歩分（S1）

かつて載せていた `out_a`（同一500mメッシュの店舗存否）・`out_c`（直線500m）・
`sm`/`cv`/`dg`/`fr`（同一メッシュの業態）は**廃止した**。距離判定は道路距離1本にしたため。

使い方
------
  python scripts/03_export_pmtiles.py                       # 既定 data/mesh500m_out_47県.parquet
  MESH_OUT=data/mesh500m_out_ATP⑪_47県.parquet python scripts/03_export_pmtiles.py
  bash output/make_pmtiles.sh                               # tippecanoe（WSL 等）
"""
import os
import subprocess
import sys

import duckdb

# 02_validate_access_difficulty.py の出力。道路距離の判定も距離もここに入っているので、
# 04 の parquet を別途結合する必要は無い。
SRC = os.environ.get("MESH_OUT", "data/mesh500m_out_47県.parquet")
OUT_DIR = "output"
GEOJSONL = f"{OUT_DIR}/food_access_125m.geojsonl"
PMTILES = f"{OUT_DIR}/food_access_125m.pmtiles"
LAYER = "food_access"

# 125mメッシュの半セル。重心 ± これでポリゴンの四隅になる。
HALF_LAT = 1 / 1920
HALF_LNG = 1 / 1280

# -125 の 05_export_geojson.py と同じ指定。--no-tile-size-limit / --no-feature-limit で
# 間引かずに全メッシュを保持し、超えたぶんだけ --coalesce-densest-as-needed で結合する。
TIPPECANOE = f"""tippecanoe \\
  -Z 4 -z 13 \\
  -l {LAYER} \\
  --no-tile-size-limit \\
  --no-feature-limit \\
  --coalesce-densest-as-needed \\
  --force \\
  -P \\
  -o {PMTILES} \\
  {GEOJSONL}"""


def main():
    if not os.path.exists(SRC):
        sys.exit(f"入力が無い: {SRC}"
                 "（先に 02_validate_access_difficulty.py を実行）")
    os.makedirs(OUT_DIR, exist_ok=True)
    con = duckdb.connect()

    n_all, n_pop = con.execute(
        f"select count(*), count(*) filter (where pop_total > 0) "
        f"from read_parquet('{SRC}')").fetchone()
    print(f"入力 {SRC}: {n_all:,} メッシュ（うち人口>0 は {n_pop:,}）")

    print(f"GeoJSONL 出力中 → {GEOJSONL} …")
    con.execute(f"""
COPY (
  SELECT to_json({{
    'type':'Feature',
    'geometry':{{'type':'Polygon','coordinates':[[
        [s.lng-{HALF_LNG}, s.lat-{HALF_LAT}],
        [s.lng+{HALF_LNG}, s.lat-{HALF_LAT}],
        [s.lng+{HALF_LNG}, s.lat+{HALF_LAT}],
        [s.lng-{HALF_LNG}, s.lat+{HALF_LAT}],
        [s.lng-{HALF_LNG}, s.lat-{HALF_LAT}]
    ]]}},
    'properties':{{
      'mesh': s.mesh_code,
      'city': s.city_code,
      'pop': s.pop_total,
      'p65': s.pop_65over,
      -- 0 = 500m圏内 / 1 = 500m圏外。判定は道路距離だけ。
      -- アクセスリンクが無く道路距離が未定義のメッシュは、-125 と同じく圏外に倒す。
      'out_d':  CASE WHEN coalesce(s.out500m_S4, true) THEN 1 ELSE 0 END,
      -- スーパー（S1）まで500m超か。緑／橙の切り分けに使う。
      'out_sm': CASE WHEN coalesce(s.out500m_S1, true) THEN 1 ELSE 0 END,
      -- 最寄りまでの徒歩分（60m/分）。到達不能・未定義は null
      'dmin': round(s.dist_S4_min, 1),
      'dsm':  round(s.dist_S1_min, 1)
    }}
  }}) AS j
  FROM read_parquet('{SRC}') s
  WHERE s.pop_total > 0
) TO '{GEOJSONL}' (FORMAT csv, HEADER false, QUOTE '', DELIMITER E'\\t');
""")
    size_mb = os.path.getsize(GEOJSONL) / 1024 / 1024
    print(f"  完了: {size_mb:.0f} MB / {n_pop:,} 行")

    sh_path = f"{OUT_DIR}/make_pmtiles.sh"
    with open(sh_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("#!/bin/bash\n" + TIPPECANOE + "\n")
    os.chmod(sh_path, 0o755)
    print(f"\n{sh_path} に保存した。tippecanoe があれば:\n\n{TIPPECANOE}\n")

    if subprocess.run("which tippecanoe", shell=True,
                      capture_output=True).returncode == 0:
        print("tippecanoe が見つかったので続けて実行する…")
        subprocess.run(["bash", sh_path], check=True)
        print(f"完了: {PMTILES}")
    else:
        print("tippecanoe が PATH に無い。WSL 等で bash output/make_pmtiles.sh を実行すること。")


if __name__ == "__main__":
    main()
