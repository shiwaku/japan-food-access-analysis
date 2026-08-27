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
後者をメッシュに割り当てる根拠が無い（係数を分解できていない）。プロパティ名も out_a のまま。

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

SRC = os.environ.get("MESH_OUT", "data/mesh500m_out_47県.parquet")
# 道路距離（04_road_distance.py の出力）。あれば変種Dの列も載せる。
ROAD = os.environ.get("ROAD_DIST", "data/mesh_road_dist.parquet")
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

    if os.path.exists(ROAD):
        # 変種D: 道路距離500m圏外（japan-transit-desert-analysis-125 と同じ Dijkstra）
        # アクセスリンクが無いメッシュは道路距離が未定義なので、-125 と同じく圏外に倒す
        road_join = f"left join read_parquet('{ROAD}') r using (mesh_code)"
        road_props = (
            ",\n      'out_d': CASE WHEN coalesce(r.out500m_road, true) THEN 1 ELSE 0 END,"
            "\n      'dmin': round(r.dist_store_min, 1)")
        print(f"道路距離を結合: {ROAD}")
    else:
        road_join, road_props = "", ""
        print(f"道路距離が無いので変種Dは載せない（{ROAD}）")

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
      -- 0 = 500m圏内 / 1 = 500m圏外（変種A＝同一500mメッシュに対象4業態が1つも無い）
      'out_a': CASE WHEN s.out500m_A THEN 1 ELSE 0 END,
      -- 対照: 重心から実距離500m以内に1件も無い
      'out_c': CASE WHEN s.out500m_C125 THEN 1 ELSE 0 END,
      -- 同一500mメッシュにどの業態があるか（ポップアップ用）
      'sm': CASE WHEN s.has_sm THEN 1 ELSE 0 END,
      'cv': CASE WHEN s.has_cv THEN 1 ELSE 0 END,
      'dg': CASE WHEN s.has_dg THEN 1 ELSE 0 END,
      'fr': CASE WHEN s.has_fr THEN 1 ELSE 0 END{road_props}
    }}
  }}) AS j
  FROM read_parquet('{SRC}') s
  {road_join}
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
