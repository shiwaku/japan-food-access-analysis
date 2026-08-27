#!/usr/bin/env python3
"""
食料品店まで**道路距離500m以上**の人口を125mメッシュから推計する（＋農水省公表値との突合）。

何を推計するか（★ 条件は距離のみ）
----------------------------------
成果物は **「食料品店まで道路距離が500m以上のところに住む人口」** であって、
農水省の「食料品アクセス困難人口」ではない。農水省の定義は

    店舗まで500m以上  **かつ**  自動車利用が困難  **かつ**  65歳以上

の3条件だが、本スクリプトは**第1条件だけ**を推計する。自動車利用困難率は外部データで
固定できておらず（係数が自動車要因と店舗レイヤの穴のどちらに由来するか分離できない）、
掛けると推計値の意味が不明になるため**掛けない**。年齢条件も外せるように
**65歳以上と総人口の両方**を出す。

★ 判定は道路距離だけ（2026-08-28）
----------------------------------
かつては次の二値近似も併走させていたが、**どちらも消した**。

  変種A    同一500mメッシュ内に店舗があるか   → 隣メッシュ10m先の店に救われない。
                                                実効半径250m程度のマス目判定で距離ではない
  変種C125 メッシュ重心から直線500m以内       → 道路距離があるなら直線を持つ意味が無い

農水省の公表値を再現するために変種Aを残していたが、農水省の推計方法が**確率版**
（当該＋周辺メッシュの店舗状況から、メッシュ内位置kで最近店舗が500m以上になる確率を
算出し人口に乗じる。`docs/検証_アクセス困難人口_メッシュ単位.md` 10.5）と判明した時点で、
**二値近似では原理的に届かない**ことが確定した。保持する理由が消えたので落とした。

→ **距離判定の出所は `04_road_distance.py` 一箇所**になった。本スクリプトは店舗レイヤを
   読まない。店舗レイヤを差し替えたときは `04` を回し直すこと。

カテゴリ感度も道路距離で出す
----------------------------
「どの業態を足すと圏外が何人減るか」は `04` が入れ子集合ごとに Dijkstra を回した結果
（S1〜S4）をそのまま集計する。かつてはメッシュ存否で数えていたが、これも道路距離に移した。

  S1 supermarket のみ / S2 ＋convenience / S3 ＋drugstore / S4 ＋fresh_food（主指標）

農水省公表値（表5）との突合は「参考」
--------------------------------------
公表値は3条件込みなので、距離条件だけの本推計と直接は比べられない。突合は

  - **比 農水省÷本推計 が1を超えないこと**（超えたら論理矛盾＝どちらかが誤り）
  - 相関 r（地域間の順位づけが公表値と整合するか）

という**必要条件のチェック**としてのみ使う。比の値そのものは「暗黙の自動車利用困難率」
であって推計の精度ではない。都市部で比>1 が出るのは上記の確率版との構造差による（issue #4）。

分母のそろえ方
--------------
アクセスリンクが張られていないメッシュは `04` の出力に現れない（全国1,853メッシュ・
65歳以上 8,450人＝0.024%）。**圏外率の分母はこれを除いた「道路距離が定義できるメッシュ」**
に限る。分母をそろえないと率が濁る。

使い方
------
  python scripts/01_fetch_mesh_population.py                       # メッシュ人口を用意
  FOOD_STORES=input/food_store_master_atp_super.parquet \
      python scripts/04_road_distance.py                          # 道路距離（判定の出所）
  OUT_SUFFIX=_ATP⑪ \
      python scripts/02_validate_access_difficulty.py 高知県 島根県 宮城県
出力
----
  output/検証_アクセス困難人口_市区町村別{SUFFIX}.csv
  output/検証_アクセス困難人口_都道府県別{SUFFIX}.csv
  output/検証_アクセス困難人口_カテゴリ感度{SUFFIX}.csv
  data/mesh500m_out{SUFFIX}.parquet   125mメッシュ1行の判定結果（集計前・WRITE_MESH=0 で抑止）

**出しているのは「500m圏外か否か」であって「アクセス困難」ではない。**
列名を out500m_* にしてあるのはそのため。自動車利用も年齢も掛けていない。
"""
import os
import sys
import urllib.request
import zipfile

import duckdb

SUFFIX = os.environ.get("OUT_SUFFIX", "")
MESH = "data/mesh/mesh125_pop.parquet"
# 距離判定の唯一の出所。`04_road_distance.py` の出力。店舗レイヤを変えたら作り直すこと。
ROAD = os.environ.get("ROAD_DIST", "data/mesh_road_dist.parquet")
BND_DIR = "data/boundary"
MAFF_XLSX = "data/maff_2020_table05.xlsx"
OUT_DIR = "output"
OUT_CITY = f"{OUT_DIR}/検証_アクセス困難人口_市区町村別{SUFFIX}.csv"
OUT_SENS = f"{OUT_DIR}/検証_アクセス困難人口_カテゴリ感度{SUFFIX}.csv"
OUT_PREF = f"{OUT_DIR}/検証_アクセス困難人口_都道府県別{SUFFIX}.csv"
# メッシュ単位の判定結果（125mメッシュ1行）。集計前の生の判定を残すための出力。
# **「圏外」であって「アクセス困難」ではない**（困難＝圏外 かつ 自動車利用困難で、
# 後者を外部データで固定できていない）。列名も out500m_* にしてある。
OUT_MESH = os.environ.get("OUT_MESH", f"data/mesh500m_out{SUFFIX}.parquet")
WRITE_MESH = os.environ.get("WRITE_MESH", "1") != "0"

MAFF_TABLE05_URL = ("https://www.maff.go.jp/primaff/seika/fsc/faccess/attach/excel/"
                    "2020_table05.xlsx")
BND_URL = ("https://www.e-stat.go.jp/gis/statmap-search/data"
           "?dlserveyId=A002005212020&code={pref_code}"
           "&coordSys=1&format=shape&downloadType=5&datum=2000")

PREF_CODES = {
    "北海道": "01", "青森県": "02", "岩手県": "03", "宮城県": "04", "秋田県": "05",
    "山形県": "06", "福島県": "07", "茨城県": "08", "栃木県": "09", "群馬県": "10",
    "埼玉県": "11", "千葉県": "12", "東京都": "13", "神奈川県": "14", "新潟県": "15",
    "富山県": "16", "石川県": "17", "福井県": "18", "山梨県": "19", "長野県": "20",
    "岐阜県": "21", "静岡県": "22", "愛知県": "23", "三重県": "24", "滋賀県": "25",
    "京都府": "26", "大阪府": "27", "兵庫県": "28", "奈良県": "29", "和歌山県": "30",
    "鳥取県": "31", "島根県": "32", "岡山県": "33", "広島県": "34", "山口県": "35",
    "徳島県": "36", "香川県": "37", "愛媛県": "38", "高知県": "39", "福岡県": "40",
    "佐賀県": "41", "長崎県": "42", "熊本県": "43", "大分県": "44", "宮崎県": "45",
    "鹿児島県": "46", "沖縄県": "47",
}

# `04_road_distance.py` の NESTED と同じ並び。ラベルだけこちらで持つ。
NESTED = [
    ("S1", "S1_supermarketのみ"),
    ("S2", "S2_+convenience"),
    ("S3", "S3_+drugstore"),
    ("S4", "S4_全カテゴリ"),
]
MAIN = "S4"  # 主指標＝全カテゴリ


def fetch_boundary(pref):
    """e-Stat 統計GIS の 令和2年国勢調査 小地域境界（shapefile, appId不要）。"""
    code = PREF_CODES[pref]
    shp = os.path.join(BND_DIR, f"r2ka{code}.shp")
    if os.path.exists(shp):
        return shp
    os.makedirs(BND_DIR, exist_ok=True)
    print(f"取得: 境界データ {pref}({code})")
    with urllib.request.urlopen(BND_URL.format(pref_code=code), timeout=300) as r:
        body = r.read()
    if not body.startswith(b"PK"):
        sys.exit(f"境界データがzipでない: {pref}")
    zpath = os.path.join(BND_DIR, f"r2ka{code}.zip")
    with open(zpath, "wb") as f:
        f.write(body)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(BND_DIR)
    if not os.path.exists(shp):
        sys.exit(f"展開後に {shp} が無い")
    return shp


def load_maff_table05():
    """農水省 表5（市区町村別 食料品アクセス困難人口, 2020年）。

    8行目のキー: code1741=市区町村コード / numi=名称 / ep20_65=65歳以上困難人口(人) / e20=割合(%)
    """
    import openpyxl
    if not os.path.exists(MAFF_XLSX):
        os.makedirs("data", exist_ok=True)
        print(f"取得: {MAFF_TABLE05_URL}")
        urllib.request.urlretrieve(MAFF_TABLE05_URL, MAFF_XLSX)
    ws = openpyxl.load_workbook(MAFF_XLSX, data_only=True)["表５ 市町村別"]
    rows = []
    for r in ws.iter_rows(min_row=9, values_only=True):
        code, name, pop, rate = r[1], r[2], r[3], r[4]
        if not (code and isinstance(code, str) and code.strip().isdigit()):
            continue
        if pop is None or rate is None:
            continue
        rows.append((code.strip(), str(name).strip(), float(pop), float(rate)))
    return rows


def main():
    prefs = sys.argv[1:]
    if not prefs:
        sys.exit("使い方: python scripts/02_validate_access_difficulty.py 高知県 [島根県 ...]")
    for p in prefs:
        if p not in PREF_CODES:
            sys.exit(f"未知の県名: {p}")
    if not os.path.exists(MESH):
        sys.exit(f"メッシュ人口が無い: {MESH}（先に 01_fetch_mesh_population.py を実行）")
    if not os.path.exists(ROAD):
        sys.exit(f"道路距離が無い: {ROAD}（先に 04_road_distance.py を実行）。"
                 "本スクリプトは距離判定を自前で持たず、ここだけを見る")

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")

    # ---- 1. 距離判定（県に依存しないので一度だけ読む）----
    dist_cols = ", ".join(f"dist_{lab}_min" for lab, _ in NESTED)
    out_cols = ", ".join(f"out500m_{lab}" for lab, _ in NESTED)
    con.execute(f"""create table road as
      select mesh_code, {dist_cols}, {out_cols} from read_parquet('{ROAD}')""")
    n_road = con.execute("select count(*) from road").fetchone()[0]
    print(f"道路距離 {ROAD}: {n_road:,} メッシュ")
    for lab, name in NESTED:
        n = con.execute(f"select count(*) filter (where out500m_{lab}) from road").fetchone()[0]
        print(f"  {name:22s} 圏外 {n:>9,} メッシュ ({n/n_road*100:5.1f}%)")

    # ---- 2. 県ごとに境界を載せ替えて cov を積む ----
    # 47県の小地域境界を一度に載せると点in poly が重い。県単位に区切ればメモリが平らになり、
    # メッシュ重心はちょうど1つの小地域にしか入らないので**結果は一括処理と同一**になる。
    cov_cols = ", ".join(f"out_{lab} boolean" for lab, _ in NESTED)
    con.execute(f"""create table cov(mesh_code varchar, city_code varchar,
                   pop_65over bigint, pop_total bigint,
                   -- 道路距離データが無いメッシュ（アクセスリンク欠落）は NULL のままにして
                   -- 分母から外す。全国1,853メッシュ・65歳以上8,450人＝0.024%。
                   {cov_cols},
                   dist_main double, dist_super double)""")
    for pref in prefs:
        shp = fetch_boundary(pref)
        con.execute("drop table if exists area")
        con.execute("create table area as select PREF || CITY as city_code, "
                    # geom::GEOMETRY で SRS 付き型を落とさないと rtree が作れない
                    f"geom::GEOMETRY as geom from ST_Read('{shp}')")
        con.execute("create index area_ix on area using rtree(geom)")
        bx = con.execute("select min(ST_XMin(geom)), min(ST_YMin(geom)), "
                         "max(ST_XMax(geom)), max(ST_YMax(geom)) from area").fetchone()

        # 全国 2,820,831 メッシュを県ごとに全走査しないよう、先に県の bbox で絞る。
        # city_code は境界データのまま（区レベル）で持ち、市への寄せは 表5 を正として後段で行う。
        con.execute("drop table if exists mesh")
        con.execute(f"""create table mesh as
          select m.mesh_code, m.lat, m.lng, m.pop_total, m.pop_65over, a.city_code
          from (select * from read_parquet('{MESH}')
                 where lng between {bx[0]} and {bx[2]}
                   and lat between {bx[1]} and {bx[3]}) m
          join area a on ST_Contains(a.geom, ST_Point(m.lng, m.lat))""")

        sel = ", ".join(f"r.out500m_{lab}" for lab, _ in NESTED)
        con.execute(f"""insert into cov
          select m.mesh_code, m.city_code, m.pop_65over, m.pop_total,
                 {sel}, r.dist_{MAIN}_min, r.dist_S1_min
          from mesh m
          left join road r using (mesh_code)""")
        n, p65 = con.execute(
            "select count(*), coalesce(sum(pop_65over),0) from mesh").fetchone()
        print(f"  {pref}: 125mメッシュ {n:>8,} 件 / 65歳以上 {p65:>9,} 人")

    n_mesh, n_uniq, pop65 = con.execute(
        "select count(*), count(distinct mesh_code), sum(pop_65over) from cov").fetchone()
    if n_mesh != n_uniq:
        # 小地域ポリゴンが重なっていると同じメッシュが二重計上される
        print(f"⚠ メッシュが重複している: {n_mesh - n_uniq:,} 件（小地域ポリゴンの重なりを確認）")
    print(f"対象県内の125mメッシュ {n_mesh:,} 件 / 65歳以上 {pop65:,} 人")

    # 被覆確認。道路距離側に無いメッシュは分母から外れるので、欠落が大きいと圏外率が濁る。
    n_nd, p_nd = con.execute(f"select count(*), coalesce(sum(pop_65over),0) "
                             f"from cov where out_{MAIN} is null").fetchone()
    print(f"  被覆: 道路距離データ無し {n_nd:,} メッシュ / 65歳以上 {p_nd:,} 人"
          f"（{p_nd/pop65*100:.3f}%）→ 分母から除外")

    # ---- 3. カテゴリ感度（道路距離のまま入れ子で出す）----
    print("\n=== カテゴリ感度: 道路距離500m圏外の65歳以上人口（対象県計）===")
    sens = []
    prev = None
    for lab, name in NESTED:
        out65, all65 = con.execute(
            f"select sum(pop_65over) filter (where out_{lab}), sum(pop_65over) "
            f"from cov where out_{lab} is not null").fetchone()
        out65 = out65 or 0
        delta = (prev - out65) if prev is not None else None
        sens.append((name, out65, all65, round(out65 / all65, 4), delta))
        d = f"  （前段からの削減 {delta:,} 人）" if delta is not None else ""
        print(f"  {name:22s} 圏外 {out65:>9,} / {all65:>9,} = {out65/all65*100:5.1f}%{d}")
        if delta is not None and delta < 0:
            # 入れ子なので店舗が増えて圏外が増えることはありえない
            print("    ⚠ 圏外が増えている。04 の入れ子集合がおかしい")
        prev = out65
    con.execute("create table sens(店舗集合 varchar, 圏外65歳以上人口 bigint, "
                "総65歳以上人口 bigint, 圏外率 double, 前段からの削減人数 bigint)")
    con.executemany("insert into sens values (?,?,?,?,?)", sens)
    os.makedirs(OUT_DIR, exist_ok=True)
    con.execute(f"copy sens to '{OUT_SENS}' (header, delimiter ',')")

    # ---- 4. 市区町村別 × 農水省突合 ----
    maff = load_maff_table05()
    con.execute("create table maff(city_code varchar, 市区町村名 varchar, "
                "maff_pop double, maff_rate double)")
    con.executemany("insert into maff values (?,?,?,?)", maff)
    print(f"\n農水省 表5（2020年）読み込み: {len(maff):,} 市区町村")

    # ---- 4a. 区コードを 表5 の市コードへ寄せる（表5 を正とする）----
    # 「XX1YY は XX100 に寄せる」は**間違い**。表5 は東京23区を個別に持つ（13101〜13123）ので
    # 寄せてはいけないし、政令市も横浜(14100)・川崎(14130)・相模原(14150)のように市コードが
    # 100 とは限らない（川崎区14131 を 14100 に寄せると横浜市に合算されてしまう）。
    # 正しくは「表5 に無いコードだけを、同一県内で自分以下・末尾0 の最大の 表5 コードへ寄せる」。
    con.execute("""create table citymap as
      select c.city_code as raw_code,
             coalesce(m.city_code,
               (select max(x.city_code) from maff x
                 where x.city_code <= c.city_code
                   and substr(x.city_code,1,2) = substr(c.city_code,1,2)
                   and right(x.city_code,1) = '0'),
               c.city_code) as city_code
      from (select distinct city_code from cov) c
      left join maff m using (city_code)""")
    moved = con.execute(
        "select count(*) from citymap where raw_code <> city_code").fetchone()[0]
    print(f"区コードを市へ寄せた: {moved} 件")

    # 推計の本体。**条件は「店舗まで道路距離500m以上」だけ**で、自動車利用も年齢も掛けていない。
    # 年齢条件を外せるように 65歳以上と総人口の両方を出す。
    # 分母は道路距離が定義できるメッシュに限る（popD65 / popDall）。
    con.execute(f"""create table city as
      select c.city_code as 市区町村コード, m.市区町村名,
             c.pop_all as 総人口,
             c.pop65 as 総65歳以上人口,
             c.n_inhabited as 居住125mメッシュ数,
             -- 125mメッシュ = 0.015625 km²。可住地ベースの密度なので行政面積より実態に近い。
             -- **距離判定に依存しない**ので、都市／地方の切り分けをこれで固定できる。
             round(c.pop65 / nullif(c.n_inhabited * 0.015625, 0)) as 高齢者密度_人per_km2,
             -- ★推計値: 道路距離500m以上。分母は道路距離が定義できるメッシュ。
             c.popD65 as 道路距離対象_65歳以上人口,
             c.popDall as 道路距離対象_総人口,
             c.out65 as 圏外65歳以上人口,
             round(c.out65 / nullif(c.popD65,0), 4) as 圏外率,
             c.outall as 圏外総人口,
             round(c.outall / nullif(c.popDall,0), 4) as 圏外率_総人口,
             -- スーパーだけで見た場合（S1）。生鮮の入手可否を見るための内訳。
             c.outS1 as スーパー圏外65歳以上人口,
             round(c.outS1 / nullif(c.popD65,0), 4) as スーパー圏外率,
             -- 参考: 農水省公表値。**3条件込みなので本推計と直接は比べられない**。
             -- 比が1を超えたら論理矛盾、という必要条件のチェックにだけ使う。
             round(m.maff_rate/100, 4) as 農水省_困難人口割合,
             round(m.maff_pop) as 農水省_困難人口,
             round((m.maff_rate/100) / nullif(c.out65 / nullif(c.popD65,0), 0), 3)
               as "比_農水省÷本推計"
      from (select k.city_code, sum(pop_65over) pop65, sum(pop_total) pop_all,
                   count(*) filter (where pop_total > 0) n_inhabited,
                   sum(pop_65over) filter (where out_{MAIN} is not null) popD65,
                   sum(pop_total)  filter (where out_{MAIN} is not null) popDall,
                   coalesce(sum(pop_65over) filter (where out_{MAIN}), 0) out65,
                   coalesce(sum(pop_total)  filter (where out_{MAIN}), 0) outall,
                   coalesce(sum(pop_65over) filter (where out_S1), 0) outS1
            from cov join citymap k on cov.city_code = k.raw_code
            group by 1) c
      left join maff m using (city_code)
      order by 圏外率 desc nulls last""")
    con.execute(f"copy city to '{OUT_CITY}' (header, delimiter ',')")

    # ---- 4b. 都道府県別（ばらつきを県単位で見る。47県運用の本体）----
    con.execute("""create table pref as
      select substr(市区町村コード,1,2) as 都道府県コード,
             count(*) as 市区町村数,
             sum(総人口) as 総人口,
             sum(総65歳以上人口) as 総65歳以上人口,
             sum(圏外65歳以上人口) as 圏外65歳以上人口,
             round(sum(圏外65歳以上人口) / nullif(sum(道路距離対象_65歳以上人口),0), 4) as 圏外率,
             sum(圏外総人口) as 圏外総人口,
             round(sum(圏外総人口) / nullif(sum(道路距離対象_総人口),0), 4) as 圏外率_総人口,
             round(sum(スーパー圏外65歳以上人口) / nullif(sum(道路距離対象_65歳以上人口),0), 4)
               as スーパー圏外率,
             -- 参考: 農水省公表値との突合（必要条件チェック）
             round(sum(農水省_困難人口) / nullif(sum(総65歳以上人口),0), 4) as 農水省_困難人口割合,
             round(sum(農水省_困難人口) / nullif(sum(圏外65歳以上人口),0), 3) as "比_農水省÷本推計",
             round(median("比_農水省÷本推計"), 3) as 比の中央値,
             round(stddev("比_農水省÷本推計"), 3) as 比のsd,
             round(corr(圏外率, 農水省_困難人口割合), 3) as 相関r,
             count(*) filter (where "比_農水省÷本推計" > 1) as 比が1超の市区町村
      from city where 農水省_困難人口 is not null
      group by 1 order by 圏外率 desc nulls last""")
    con.execute(f"copy pref to '{OUT_PREF}' (header, delimiter ',')")

    # ---- 4c. メッシュ単位の判定結果（集計前）----
    if WRITE_MESH:
        os.makedirs(os.path.dirname(OUT_MESH) or ".", exist_ok=True)
        mesh_out = ", ".join(f"c.out_{lab} as out500m_{lab}" for lab, _ in NESTED)
        con.execute(f"""copy (
          select c.mesh_code, k.city_code, m.lat, m.lng, c.pop_total, c.pop_65over,
                 -- 最寄り店舗までの徒歩分（60m/分）。S1=スーパーのみ / S4=全カテゴリ
                 c.dist_main as dist_S4_min, c.dist_super as dist_S1_min,
                 {mesh_out}
          from cov c
          join citymap k on c.city_code = k.raw_code
          join read_parquet('{MESH}') m using (mesh_code)
        ) to '{OUT_MESH}' (FORMAT parquet, COMPRESSION zstd)""")
        n_out, p_out = con.execute(
            f"select count(*) filter (where out_{MAIN}), "
            f"coalesce(sum(pop_65over) filter (where out_{MAIN}),0) from cov").fetchone()
        print(f"出力: {OUT_MESH}"
              f"（125mメッシュ {n_mesh:,} 行 / うち圏外 {n_out:,} メッシュ・"
              f"65歳以上 {p_out:,} 人）")
    print(f"出力: {OUT_SENS}")
    print(f"出力: {OUT_CITY}")
    print(f"出力: {OUT_PREF}")

    matched, total = con.execute(
        "select count(農水省_困難人口), count(*) from city").fetchone()
    print(f"コード突合: {matched}/{total} 市区町村")
    unmatched = con.execute("""select 市区町村コード, 総65歳以上人口 from city
                               where 農水省_困難人口 is null and 総65歳以上人口 > 0
                               order by 総65歳以上人口 desc limit 10""").fetchall()
    if unmatched:
        print("  突合できなかったコード（65歳以上人口の多い順）:")
        for c, p in unmatched:
            print(f"    {c}  65+={p:,}")

    # ---- 5. 推計値（★これが成果物。条件は「店舗まで道路距離500m以上」だけ）----
    print("\n=== 推計: 食料品店まで道路距離500m以上の人口（対象県計）===")
    a = con.execute("""select sum(総人口), sum(総65歳以上人口),
                              sum(道路距離対象_総人口), sum(道路距離対象_65歳以上人口),
                              sum(圏外総人口), sum(圏外65歳以上人口),
                              sum(スーパー圏外65歳以上人口), sum(農水省_困難人口)
                       from city where 農水省_困難人口 is not null""").fetchone()
    pop_all, pop65, dnA, dn65, dA, d65, s65, maff_pop = a
    print(f"  総人口                       {pop_all:>12,}")
    print(f"  65歳以上人口                 {pop65:>12,}")
    print(f"  ★500m以上（総人口）          {dA:>12,}  ({dA/dnA*100:.1f}%)")
    print(f"  ★500m以上（65歳以上）        {d65:>12,}  ({d65/dn65*100:.1f}%)")
    print(f"     ↑ 分母は道路距離が定義できるメッシュ "
          f"（総人口 {dnA:,} / 65歳以上 {dn65:,}・"
          f"欠落 {pop65-dn65:,}人 = {(pop65-dn65)/pop65*100:.2f}%）")
    print(f"  （内訳）スーパーが500m以上   {s65:>12,}  ({s65/dn65*100:.1f}%)")

    # ---- 6. 参考: 農水省公表値との突合（必要条件チェック。精度の指標ではない）----
    print("\n=== 参考: 農水省 公表値との突合（3条件込みなので直接比較はできない）===")
    print(f"  農水省 困難人口（公表）      {int(maff_pop):>12,}  ({maff_pop/pop65*100:.1f}%)")
    print(f"  比 農水省÷本推計             {maff_pop/d65:>12.3f}  ← 暗黙の自動車利用困難率")
    q = con.execute("""select count(*), min("比_農水省÷本推計"),
                              quantile_cont("比_農水省÷本推計",0.1),
                              median("比_農水省÷本推計"),
                              quantile_cont("比_農水省÷本推計",0.9),
                              max("比_農水省÷本推計"), stddev("比_農水省÷本推計"),
                              corr(圏外率, 農水省_困難人口割合),
                              count(*) filter (where "比_農水省÷本推計" > 1)
                       from city where "比_農水省÷本推計" is not null""").fetchone()
    print(f"  n={q[0]}  min={q[1]:.3f}  p10={q[2]:.3f}  median={q[3]:.3f}  "
          f"p90={q[4]:.3f}  max={q[5]:.3f}  sd={q[6]:.3f}")
    print(f"  相関 r={q[7]:.3f}   比>1 の市区町村={q[8]} 件"
          f"{'  ← 都市部で破れる（issue #4）' if q[8] else '  ← 整合'}")

    # 都市／地方は**距離判定に依存しない**高齢者密度で切る。圏外率で切ると測る対象そのもので
    # 区分することになり、店舗レイヤを差し替えると区分も動いて比較にならない。
    print("\n=== 領域別（高齢者密度 1,500人/km²・可住地ベースで分ける）===")
    for lab, cond in (("都市部（≥1,500）", "高齢者密度_人per_km2 >= 1500"),
                      ("地方部（<1,500）", "高齢者密度_人per_km2 < 1500")):
        r = con.execute(f"""select count(*), sum(総65歳以上人口),
                                  sum(圏外65歳以上人口), sum(道路距離対象_65歳以上人口),
                                  corr(圏外率, 農水省_困難人口割合),
                                  count(*) filter (where "比_農水省÷本推計" > 1)
                           from city where 農水省_困難人口 is not null and {cond}""").fetchone()
        n, p, o, dn, r_, gt = r
        if not n or not dn:
            # 3県だけで回すと都市部が1件も無い、のように片側が空になる
            print(f"  {lab}: 該当なし")
            continue
        r_txt = f"{r_:.3f}" if r_ is not None else "  n/a"
        print(f"  {lab}: {n:>5}市区町村  65+={p:>11,}  圏外率={o/dn*100:5.1f}%  "
              f"r={r_txt}  比>1={gt}件 ({gt/n*100:.1f}%)")

    for title, order in (
            ("比が小さい＝圏外率が過大（店舗レイヤの穴の疑い）", ""),
            ("比が大きい＝圏外率が過小（店舗の偽陽性・過剰計上の疑い）", " desc")):
        print(f"\n=== {title} top10 ===")
        for r in con.execute(f"""
                select 市区町村コード, 市区町村名, 総65歳以上人口, 圏外率,
                       農水省_困難人口割合, "比_農水省÷本推計"
                from city
                where "比_農水省÷本推計" is not null and 総65歳以上人口 >= 3000
                order by "比_農水省÷本推計"{order} limit 10""").fetchall():
            print(f"  {r[0]} {str(r[1]):12s} 65+={r[2]:>7,} "
                  f"圏外={r[3]*100:5.1f}% 農水省={r[4]*100:5.1f}% 比={r[5]}")


if __name__ == "__main__":
    main()
