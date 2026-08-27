#!/usr/bin/env python3
"""
食料品店レイヤを「農水省 食料品アクセス困難人口」の指標で検証する（125mメッシュ人口）。

何をするか
----------
125mメッシュ人口（`01_fetch_mesh_population.py`）と食料品店レイヤ（`FOOD_STORES`）から
「500m圏外の65歳以上人口」を市区町村別に出し、農水省公表値（表5・2020年）と突合する。

農水省の操作化について（500mメッシュ人口で実測した結論・`docs/検証_アクセス困難人口_メッシュ単位.md`）
------------------------------------------------------------------------------------
「500m以上」の実装候補を3つ試し、公表値との比（= 暗黙の自動車利用困難率）の分布を比べた:

  変種A 同一500mメッシュ内に店舗があるか   → 比 中央値0.370 / sd0.109  ★整合
  変種B 自メッシュ+8近傍メッシュ            → 比 中央値0.592 / sd2.696
  変種C 500mメッシュ重心から実距離500m以内  → 比 中央値0.446 / sd0.504

農水省の困難人口は「圏外 かつ 自動車利用困難」なので、比は必ず1以下でなければならない。
B と C は比が1を超える市区町村が出る（＝論理的に不整合）。**変種Aのみ全市区町村で比≤1**。
→ 公表値と整合する操作化は「店舗を500mメッシュに集計し、自メッシュに店舗が無ければ圏外」。
   つまり**隣メッシュの店舗には救われない**＝店舗レイヤの網羅性が直に効く。

125mメッシュ人口にした理由と、ここで測り直すこと
------------------------------------------------
変種Cが不整合だった原因の一部は**重心誤差**である。500m格子の重心は実際の居住位置から
最大 ~350m ずれるので、500mの距離判定に対して誤差が同じオーダーになってしまう。
125mメッシュなら重心誤差は最大 ~88m に落ちる。そこで本スクリプトは同じ人口レイヤから

  変種A    : 125mメッシュが属する500mメッシュ（11桁コードの先頭9桁）に店舗があるか  ← 主・農水省再現
  変種C125 : 125mメッシュ重心から実距離500m以内に店舗があるか                       ← 対照

の両方を出す。**変種Aは500mメッシュ単位の集計としては人口を125mにしても数学的に不変**
（子16メッシュの和＝親メッシュ）で、変わるのは市区町村への割当精度だけ。
意味が変わるのは変種C125の側であり、これが比≤1 を満たすかどうかが本リポジトリの論点。

判定の見方
----------
件数を足せば圏外率は必ず下がるので、下がったこと自体は判定材料にならない。
可否は **「比 農水省÷変種A が1を超えないこと」** と
**「農水省公表値との相関 r が上がること」** で見る（本スクリプトが両方出す）。

距離・メッシュの扱い
--------------------
- 500mメッシュコード（9桁）は店舗側で緯度経度から計算する（`mesh500_code`）。
  人口側は11桁L6コードの先頭9桁がそのまま500mメッシュコードになる。
- 変種C125 の近傍探索は等距円筒近似でメートル平面へ投影し 500m グリッドでバケット化する。
  この環境の DuckDB は `ST_Distance_Spheroid` が -nan を返すため。
- DuckDB の `/` は DOUBLE を返し `::int` が四捨五入するため、メッシュ添字は必ず `//` を使う。

使い方
------
  python scripts/01_fetch_mesh_population.py 高知県 島根県 宮城県   # 先にメッシュ人口を用意
  FOOD_STORES=input/food_store_master_atp_permit.parquet \
      python scripts/02_validate_access_difficulty.py 高知県 島根県 宮城県
出力
----
  output/検証_アクセス困難人口_市区町村別{SUFFIX}.csv
  output/検証_アクセス困難人口_都道府県別{SUFFIX}.csv
  output/検証_アクセス困難人口_カテゴリ感度{SUFFIX}.csv
  data/mesh500m_out{SUFFIX}.parquet   125mメッシュ1行の判定結果（集計前・WRITE_MESH=0 で抑止）

**メッシュ単位で出せるのは「500m圏外か否か」であって「アクセス困難」ではない。**
農水省の困難人口は「圏外 **かつ** 自動車利用困難」で、後者を外部データで固定できていない
（係数 0.42〜0.57 が自動車要因と店舗の穴のどちらに由来するか分離できていない）。
列名を out500m_A / out500m_C125 にしてあるのはそのため。
"""
import os
import sys
import urllib.request
import zipfile

import duckdb

# 検証したい店舗レイヤを差し替えられるようにしておく。
# 「許可データを投入したらアクセス困難人口がどう動くか」のような**投入前後の比較**を、
# 店舗マスター本体を書き換えずに測るための入口。出力も同時にずらすこと（既定の CSV を
# 実験結果で上書きしないため）。
#   FOOD_STORES=input/tmp_with_permit.parquet OUT_SUFFIX=_許可投入後 \
#       python scripts/02_validate_access_difficulty.py 高知県 島根県 宮城県
#
# 店舗レイヤに必要な列は lat / lng / cat の3つだけ。
# cat は supermarket / convenience / drugstore / fresh_food。
# 実体は shiwaku/japan-food-store-master が生成する parquet。
STORES = os.environ.get("FOOD_STORES", "input/food_store_master.parquet")
SUFFIX = os.environ.get("OUT_SUFFIX", "")
MESH = "data/mesh/mesh125_pop.parquet"
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

# 農水省の対象業種は生鮮専門店・ドラッグストアも含む（access_genjo.html 原文）。
# 入れ子にして「どのカテゴリの欠落が何人分の誤差になるか」を分離する。
NESTED = [
    ("S1_supermarketのみ", "has_sm"),
    ("S2_+convenience", "(has_sm or has_cv)"),
    ("S3_+drugstore", "(has_sm or has_cv or has_dg)"),
    ("S4_全カテゴリ", "(has_sm or has_cv or has_dg or has_fr)"),
]

THRESHOLD_M = 500


def mesh500_code(lat, lng):
    """緯度経度 -> 9桁の500m（4次）メッシュコード。"""
    a = lat * 1.5
    p = int(a); a -= p
    q = int(a * 8); a = a * 8 - q
    r = int(a * 10); a = a * 10 - r
    m_lat = int(a * 2)
    f = lng - 100
    u = int(f); f -= u
    v = int(f * 8); f = f * 8 - v
    w = int(f * 10); f = f * 10 - w
    m_lng = int(f * 2)
    return f"{p:02d}{u:02d}{q}{v}{r}{w}{m_lat * 2 + m_lng + 1}"


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
    if not os.path.exists(STORES):
        sys.exit(f"店舗レイヤが無い: {STORES}"
                 "（FOOD_STORES で指定するか input/ に置く。"
                 "実体は shiwaku/japan-food-store-master の parquet）")
    if not os.path.exists(MESH):
        sys.exit(f"メッシュ人口が無い: {MESH}（先に 01_fetch_mesh_population.py を実行）")

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.create_function("mesh500", mesh500_code, ["DOUBLE", "DOUBLE"], "VARCHAR")

    # ---- 1. 店舗側は県に依存しないので一度だけ作る ----
    # 変種A: 店舗を500mメッシュへ割当（農水省再現）
    con.execute(f"""create table smesh as
      select mesh500(lat, lng) as mesh500_code, cat from read_parquet('{STORES}')""")
    con.execute("""create table hitA as
      select mesh500_code,
             bool_or(cat='supermarket') has_sm, bool_or(cat='convenience') has_cv,
             bool_or(cat='drugstore')   has_dg, bool_or(cat='fresh_food')  has_fr
      from smesh group by 1""")
    # 変種C125: 店舗を500mグリッドにバケット化（等距円筒近似。spheroid 関数は -nan を返す）
    con.execute(f"""create table sb as
      select floor(lng*111320*cos(radians(lat))/{THRESHOLD_M})::bigint cx,
             floor(lat*111320/{THRESHOLD_M})::bigint cy,
             lng*111320*cos(radians(lat)) x, lat*111320 y
      from read_parquet('{STORES}')""")
    n_store = con.execute("select count(*) from sb").fetchone()[0]
    print(f"店舗レイヤ {STORES}: {n_store:,} 件")

    # ---- 2. 県ごとに境界を載せ替えて cov を積む ----
    # 47県の小地域境界を一度に載せると点in poly が重い。県単位に区切ればメモリが平らになり、
    # メッシュ重心はちょうど1つの小地域にしか入らないので**結果は一括処理と同一**になる。
    con.execute("""create table cov(mesh_code varchar, city_code varchar,
                   pop_65over bigint, pop_total bigint,
                   has_sm boolean, has_cv boolean, has_dg boolean, has_fr boolean,
                   inC boolean)""")
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
          select m.mesh_code, m.mesh500_code, m.lat, m.lng, m.pop_total, m.pop_65over,
                 a.city_code
          from (select * from read_parquet('{MESH}')
                 where lng between {bx[0]} and {bx[2]}
                   and lat between {bx[1]} and {bx[3]}) m
          join area a on ST_Contains(a.geom, ST_Point(m.lng, m.lat))""")

        con.execute("drop table if exists hitC")
        con.execute(f"""create table hitC as
          select distinct k.mesh_code from
            (select mesh_code, lng*111320*cos(radians(lat)) x, lat*111320 y,
                    floor(lng*111320*cos(radians(lat))/{THRESHOLD_M})::bigint + dx cx,
                    floor(lat*111320/{THRESHOLD_M})::bigint + dy cy
             from mesh, (select unnest([-1,0,1]) dx) a, (select unnest([-1,0,1]) dy) b) k
          join sb s on k.cx=s.cx and k.cy=s.cy
          where sqrt(power(k.x-s.x,2)+power(k.y-s.y,2)) <= {THRESHOLD_M}""")

        con.execute("""insert into cov
          select m.mesh_code, m.city_code, m.pop_65over, m.pop_total,
                 coalesce(a.has_sm,false), coalesce(a.has_cv,false),
                 coalesce(a.has_dg,false), coalesce(a.has_fr,false),
                 (c.mesh_code is not null)
          from mesh m
          left join hitA a using (mesh500_code)
          left join hitC c using (mesh_code)""")
        n, p65 = con.execute(
            "select count(*), coalesce(sum(pop_65over),0) from mesh").fetchone()
        print(f"  {pref}: 125mメッシュ {n:>8,} 件 / 65歳以上 {p65:>9,} 人")

    n_mesh, n500, pop65 = con.execute(
        "select count(*), count(distinct mesh_code), sum(pop_65over) from cov").fetchone()
    if n_mesh != n500:
        # 小地域ポリゴンが重なっていると同じメッシュが二重計上される
        print(f"⚠ メッシュが重複している: {n_mesh - n500:,} 件（小地域ポリゴンの重なりを確認）")
    print(f"対象県内の125mメッシュ {n_mesh:,} 件 / 65歳以上 {pop65:,} 人")

    # ---- 5. カテゴリ感度（主指標 = 変種A）----
    print("\n=== カテゴリ感度: 500m圏外の65歳以上人口（変種A・対象県計）===")
    sens = []
    prev = None
    for label, expr in NESTED:
        out65, all65 = con.execute(
            f"select sum(pop_65over) filter (where not {expr}), sum(pop_65over) from cov"
        ).fetchone()
        out65 = out65 or 0
        delta = (prev - out65) if prev is not None else None
        sens.append((label, out65, all65, round(out65 / all65, 4), delta))
        d = f"  （前段からの削減 {delta:,} 人）" if delta is not None else ""
        print(f"  {label:22s} 圏外 {out65:>9,} / {all65:>9,} = {out65/all65*100:5.1f}%{d}")
        prev = out65
    outC, allC = con.execute(
        "select sum(pop_65over) filter (where not inC), sum(pop_65over) from cov").fetchone()
    label_c = "(対照) 変種C125 実距離500m"
    print(f"  {label_c:22s} 圏外 {outC:>9,} / {allC:>9,} = {outC/allC*100:5.1f}%")

    con.execute("create table sens(店舗集合 varchar, 圏外65歳以上人口 bigint, "
                "総65歳以上人口 bigint, 圏外率 double, 前段からの削減人数 bigint)")
    con.executemany("insert into sens values (?,?,?,?,?)", sens)
    os.makedirs(OUT_DIR, exist_ok=True)
    con.execute(f"copy sens to '{OUT_SENS}' (header, delimiter ',')")

    # ---- 6. 市区町村別 × 農水省突合 ----
    maff = load_maff_table05()
    con.execute("create table maff(city_code varchar, 市区町村名 varchar, "
                "maff_pop double, maff_rate double)")
    con.executemany("insert into maff values (?,?,?,?)", maff)
    print(f"\n農水省 表5（2020年）読み込み: {len(maff):,} 市区町村")

    # ---- 6a. 区コードを 表5 の市コードへ寄せる（表5 を正とする）----
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

    con.execute("""create table city as
      select c.city_code as 市区町村コード, m.市区町村名,
             c.pop65 as 総65歳以上人口,
             c.n_inhabited as 居住125mメッシュ数,
             -- 125mメッシュ = 0.015625 km²。可住地ベースの密度なので行政面積より実態に近い。
             -- **店舗レイヤに依存しない**ので、都市／地方の切り分けをこれで固定できる。
             round(c.pop65 / nullif(c.n_inhabited * 0.015625, 0)) as 高齢者密度_人per_km2,
             c.outA as 圏外65歳以上人口_変種A,
             round(c.outA / nullif(c.pop65,0), 4) as 圏外率_変種A,
             c.outA_sm as 圏外65歳以上人口_supermarketのみ,
             c.outC as 圏外65歳以上人口_変種C125,
             round(c.outC / nullif(c.pop65,0), 4) as 圏外率_変種C125,
             round(m.maff_rate/100, 4) as 農水省_困難人口割合,
             round(m.maff_pop) as 農水省_困難人口,
             round((m.maff_rate/100) / nullif(c.outA / nullif(c.pop65,0), 0), 3) as 比_農水省÷変種A,
             round((m.maff_rate/100) / nullif(c.outC / nullif(c.pop65,0), 0), 3) as 比_農水省÷変種C125
      from (select k.city_code, sum(pop_65over) pop65,
                   count(*) filter (where pop_total > 0) n_inhabited,
                   sum(case when not (has_sm or has_cv or has_dg or has_fr) then pop_65over else 0 end) outA,
                   sum(case when not has_sm then pop_65over else 0 end) outA_sm,
                   sum(case when not inC then pop_65over else 0 end) outC
            from cov join citymap k on cov.city_code = k.raw_code
            group by 1) c
      left join maff m using (city_code)
      order by 圏外率_変種A desc""")
    con.execute(f"copy city to '{OUT_CITY}' (header, delimiter ',')")

    # ---- 6b. 都道府県別（係数のばらつきを県単位で見る。47県運用の本体）----
    con.execute("""create table pref as
      select substr(市区町村コード,1,2) as 都道府県コード,
             count(*) as 市区町村数,
             sum(総65歳以上人口) as 総65歳以上人口,
             sum(圏外65歳以上人口_変種A) as 圏外65歳以上人口_変種A,
             round(sum(圏外65歳以上人口_変種A) / nullif(sum(総65歳以上人口),0), 4) as 圏外率_変種A,
             round(sum(圏外65歳以上人口_変種C125) / nullif(sum(総65歳以上人口),0), 4) as 圏外率_変種C125,
             round(sum(農水省_困難人口) / nullif(sum(総65歳以上人口),0), 4) as 農水省_困難人口割合,
             round(sum(農水省_困難人口) / nullif(sum(圏外65歳以上人口_変種A),0), 3) as 比_農水省÷変種A,
             round(median("比_農水省÷変種A"), 3) as 比の中央値,
             round(stddev("比_農水省÷変種A"), 3) as 比のsd,
             round(stddev("比_農水省÷変種A") / nullif(avg("比_農水省÷変種A"),0), 3) as 比の変動係数,
             round(corr(圏外率_変種A, 農水省_困難人口割合), 3) as 相関r,
             count(*) filter (where "比_農水省÷変種A" > 1) as 比が1超の市区町村,
             count(*) filter (where "比_農水省÷変種C125" > 1) as 比C125が1超の市区町村
      from city where 農水省_困難人口 is not null
      group by 1 order by 圏外率_変種A desc""")
    con.execute(f"copy pref to '{OUT_PREF}' (header, delimiter ',')")

    # ---- 6c. メッシュ単位の判定結果（集計前）----
    if WRITE_MESH:
        os.makedirs(os.path.dirname(OUT_MESH) or ".", exist_ok=True)
        con.execute(f"""copy (
          select c.mesh_code, substr(c.mesh_code,1,9) as mesh500_code,
                 k.city_code, m.lat, m.lng, c.pop_total, c.pop_65over,
                 c.has_sm, c.has_cv, c.has_dg, c.has_fr,
                 -- 変種A: 同一500mメッシュに対象4業態のいずれも無ければ 500m圏外
                 not (c.has_sm or c.has_cv or c.has_dg or c.has_fr) as out500m_A,
                 -- 変種C125: 重心から実距離500m以内に1件も無ければ圏外（対照）
                 not c.inC as out500m_C125
          from cov c
          join citymap k on c.city_code = k.raw_code
          join read_parquet('{MESH}') m using (mesh_code)
        ) to '{OUT_MESH}' (FORMAT parquet, COMPRESSION zstd)""")
        n_out, p_out = con.execute(
            "select count(*) filter (where not (has_sm or has_cv or has_dg or has_fr)), "
            "sum(pop_65over) filter (where not (has_sm or has_cv or has_dg or has_fr)) "
            "from cov").fetchone()
        print(f"出力: {OUT_MESH}"
              f"（125mメッシュ {n_mesh:,} 行 / うち変種A圏外 {n_out:,} メッシュ・"
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

    print("\n=== 対象県 全体 ===")
    a = con.execute("""select sum(総65歳以上人口), sum(圏外65歳以上人口_変種A),
                              sum(圏外65歳以上人口_変種C125), sum(農水省_困難人口)
                       from city where 農水省_困難人口 is not null""").fetchone()
    print(f"  65歳以上人口                 {a[0]:>10,}")
    print(f"  変種A    500m圏外人口         {a[1]:>10,}  ({a[1]/a[0]*100:.1f}%)")
    print(f"  変種C125 500m圏外人口         {a[2]:>10,}  ({a[2]/a[0]*100:.1f}%)")
    print(f"  農水省 困難人口（公表）        {int(a[3]):>10,}  ({a[3]/a[0]*100:.1f}%)")
    print(f"  比 農水省÷変種A               {a[3]/a[1]:>10.3f}  ← 自動車利用困難率に相当")
    print(f"  比 農水省÷変種C125            {a[3]/a[2]:>10.3f}")

    for col, rate in (("比_農水省÷変種A", "圏外率_変種A"),
                      ("比_農水省÷変種C125", "圏外率_変種C125")):
        print(f"\n=== {col} の分布（1を超えたら操作化が不整合）===")
        q = con.execute(f"""select count(*), min({col}), quantile_cont({col},0.1),
                                  median({col}), quantile_cont({col},0.9),
                                  max({col}), stddev({col}),
                                  corr({rate}, 農水省_困難人口割合),
                                  count(*) filter (where {col} > 1)
                           from city where {col} is not null""").fetchone()
        print(f"  n={q[0]}  min={q[1]:.3f}  p10={q[2]:.3f}  median={q[3]:.3f}  "
              f"p90={q[4]:.3f}  max={q[5]:.3f}  sd={q[6]:.3f}")
        print(f"  相関 r={q[7]:.3f}   比>1 の市区町村={q[8]} 件"
              f"{'  ← 不整合' if q[8] else '  ← 整合'}")

    print("\n=== 比が小さい＝圏外率が過大（店舗レイヤの穴の疑い）top10 ===")
    for r in con.execute("""select 市区町村コード, 市区町村名, 総65歳以上人口, 圏外率_変種A,
                                   農水省_困難人口割合, 比_農水省÷変種A
                            from city where 比_農水省÷変種A is not null and 総65歳以上人口 >= 3000
                            order by 比_農水省÷変種A limit 10""").fetchall():
        print(f"  {r[0]} {str(r[1]):12s} 65+={r[2]:>7,} "
              f"変種A圏外={r[3]*100:5.1f}% 農水省={r[4]*100:5.1f}% 比={r[5]}")

    print("\n=== 比が大きい＝圏外率が過小（店舗の偽陽性・過剰計上の疑い）top10 ===")
    for r in con.execute("""select 市区町村コード, 市区町村名, 総65歳以上人口, 圏外率_変種A,
                                   農水省_困難人口割合, 比_農水省÷変種A
                            from city where 比_農水省÷変種A is not null and 総65歳以上人口 >= 3000
                            order by 比_農水省÷変種A desc limit 10""").fetchall():
        print(f"  {r[0]} {str(r[1]):12s} 65+={r[2]:>7,} "
              f"変種A圏外={r[3]*100:5.1f}% 農水省={r[4]*100:5.1f}% 比={r[5]}")


if __name__ == "__main__":
    main()
