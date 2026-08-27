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

主指標は **変種D（道路距離500m）**
----------------------------------
  変種D    : 道路ネットワーク上の距離が500m超か（`04_road_distance.py` の出力を結合）  ← ★主
  変種A    : 125mメッシュが属する500mメッシュ（11桁コードの先頭9桁）に店舗があるか      ← 対照
  変種C125 : 125mメッシュ重心から直線500m以内に店舗があるか                             ← 対照

変種Dを主にする理由は、**実際に歩く距離であり、地域の厳しさの順位づけで最良だから**
（地方部の相関 rA 0.199 → rD 0.258・`docs/検証_アクセス困難人口_メッシュ単位.md` 9.3）。
変種Aは「同一500mメッシュ内の存否」なので隣メッシュ10m先の店に救われず、実効半径が
250m程度になる二値近似であり、距離そのものを測ってはいない。

農水省公表値（表5）との突合は「参考」に降格した
------------------------------------------------
公表値は上の3条件込みなので、距離条件だけの本推計と直接は比べられない。突合は

  - **比 農水省÷本推計 が1を超えないこと**（超えたら論理矛盾＝どちらかが誤り）
  - 相関 r（地域間の順位づけが公表値と整合するか）

という**必要条件のチェック**としてのみ使う。比の値そのものは「暗黙の自動車利用困難率」
であって推計の精度ではない。なお農水省の推計方法は 2024-03-19 研究成果報告会資料により
**確率版**（当該＋周辺メッシュの店舗状況から、メッシュ内位置kで最近店舗が500m以上に
なる確率を算出し人口に乗じる）かつ**直線距離**と判明している。二値判定の変種A/C/Dとは
構造が違うので、都市部で比>1 が出るのはこの差による（issue #4）。

125mメッシュ人口にした理由
--------------------------
500m格子の重心は実際の居住位置から最大 ~350m ずれ、500mの距離判定に対して誤差が同じ
オーダーになる。125mメッシュなら重心誤差は最大 ~88m に落ちる。
**変種Aは500mメッシュ単位の集計としては人口を125mにしても数学的に不変**
（子16メッシュの和＝親メッシュ）で、変わるのは市区町村への割当精度だけ。

店舗レイヤの投入可否の見方
--------------------------
件数を足せば圏外率は必ず下がるので、下がったこと自体は判定材料にならない。
可否は **「比 農水省÷本推計 が1を超えないこと」** と **「相関 r が上がること」** を
**地方部（高齢者密度 < 1,500人/km²・可住地ベース）に限定して**見る。

距離・メッシュの扱い
--------------------
- 500mメッシュコード（9桁）は店舗側で緯度経度から計算する（`mesh500_code`）。
  人口側は11桁L6コードの先頭9桁がそのまま500mメッシュコードになる。
- 変種C125 の近傍探索は等距円筒近似でメートル平面へ投影し 500m グリッドでバケット化する。
  この環境の DuckDB は `ST_Distance_Spheroid` が -nan を返すため。
- DuckDB の `/` は DOUBLE を返し `::int` が四捨五入するため、メッシュ添字は必ず `//` を使う。
- 変種Dは距離を持たず**真偽値だけを結合する**（`ROAD_DIST` の `out500m_road`）。
  道路距離の算出は `04_road_distance.py` 側の責務で、**同じ店舗レイヤで作ること**。
  アクセスリンクが張られていないメッシュは道路距離側に現れないので、
  **変種Dの分母は「道路距離データがあるメッシュ」に限る**（分母をそろえないと率が濁る）。

使い方
------
  python scripts/01_fetch_mesh_population.py                        # 先にメッシュ人口を用意
  FOOD_STORES=input/food_store_master_atp_super.parquet \
      python scripts/04_road_distance.py                           # 変種Dの道路距離を作る
  FOOD_STORES=input/food_store_master_atp_super.parquet OUT_SUFFIX=_ATP⑪ \
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
# 変種D（主指標）の道路距離。`04_road_distance.py` の出力。**同じ店舗レイヤで作ること**。
# 無ければ変種Dの列を落として変種A/C125 だけで走る（道路ネットワークはリポジトリ外なので、
# それが無い環境でも検証だけは回せるようにしておく）。
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

    # 変種D（主指標）: 道路距離の判定結果。全国1本なので県ループの外で一度だけ読む。
    has_road = os.path.exists(ROAD)
    if has_road:
        con.execute(f"""create table road as
          select mesh_code, out500m_road from read_parquet('{ROAD}')""")
        n_road, n_road_out = con.execute(
            "select count(*), count(*) filter (where out500m_road) from road").fetchone()
        print(f"道路距離 {ROAD}: {n_road:,} メッシュ / うち500m超 {n_road_out:,}")
    else:
        con.execute("create table road(mesh_code varchar, out500m_road boolean)")
        print(f"⚠ 道路距離が無い: {ROAD} → 主指標の変種Dを飛ばし、変種A/C125 だけで走る"
              "（先に scripts/04_road_distance.py を同じ店舗レイヤで実行すること）")

    # ---- 2. 県ごとに境界を載せ替えて cov を積む ----
    # 47県の小地域境界を一度に載せると点in poly が重い。県単位に区切ればメモリが平らになり、
    # メッシュ重心はちょうど1つの小地域にしか入らないので**結果は一括処理と同一**になる。
    con.execute("""create table cov(mesh_code varchar, city_code varchar,
                   pop_65over bigint, pop_total bigint,
                   has_sm boolean, has_cv boolean, has_dg boolean, has_fr boolean,
                   inC boolean,
                   -- 変種D。道路距離データが無いメッシュは NULL のままにして分母から外す
                   -- （アクセスリンクが張られていないメッシュ・全国417件）。
                   outD boolean)""")
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
                 (c.mesh_code is not null),
                 r.out500m_road
          from mesh m
          left join hitA a using (mesh500_code)
          left join hitC c using (mesh_code)
          left join road r using (mesh_code)""")
        n, p65 = con.execute(
            "select count(*), coalesce(sum(pop_65over),0) from mesh").fetchone()
        print(f"  {pref}: 125mメッシュ {n:>8,} 件 / 65歳以上 {p65:>9,} 人")

    n_mesh, n500, pop65 = con.execute(
        "select count(*), count(distinct mesh_code), sum(pop_65over) from cov").fetchone()
    if n_mesh != n500:
        # 小地域ポリゴンが重なっていると同じメッシュが二重計上される
        print(f"⚠ メッシュが重複している: {n_mesh - n500:,} 件（小地域ポリゴンの重なりを確認）")
    print(f"対象県内の125mメッシュ {n_mesh:,} 件 / 65歳以上 {pop65:,} 人")

    # 変種Dの被覆確認。道路距離側に無いメッシュ（アクセスリンク欠落）は分母から外れるので、
    # 欠落が大きいと圏外率が濁る。全国では417件・0.01%程度に収まっているはず。
    if has_road:
        n_nd, p_nd = con.execute("select count(*), coalesce(sum(pop_65over),0) "
                                 "from cov where outD is null").fetchone()
        print(f"  変種D 被覆: 道路距離データ無し {n_nd:,} メッシュ / 65歳以上 {p_nd:,} 人"
              f"（{p_nd/pop65*100:.3f}%）→ 変種Dの分母から除外")

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
    if has_road:
        # 変種Dは道路距離側で店舗集合が固定されているのでカテゴリ入れ子には分解できない。
        # 全カテゴリ相当の1行だけを、分母をそろえて出す。
        outD, allD = con.execute(
            "select sum(pop_65over) filter (where outD), sum(pop_65over) "
            "from cov where outD is not null").fetchone()
        label_d = "★主 変種D 道路距離500m"
        print(f"  {label_d:22s} 圏外 {outD:>9,} / {allD:>9,} = {outD/allD*100:5.1f}%")

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

    # 推計の本体。**条件は「店舗まで500m以上」だけ**で、自動車利用も年齢も掛けていない。
    # 年齢条件を外せるように 65歳以上と総人口の両方を出す。
    # 変種D（主）の分母は道路距離データがあるメッシュに限る（popD65 / popDall）。
    con.execute("""create table city as
      select c.city_code as 市区町村コード, m.市区町村名,
             c.pop_all as 総人口,
             c.pop65 as 総65歳以上人口,
             c.n_inhabited as 居住125mメッシュ数,
             -- 125mメッシュ = 0.015625 km²。可住地ベースの密度なので行政面積より実態に近い。
             -- **店舗レイヤに依存しない**ので、都市／地方の切り分けをこれで固定できる。
             round(c.pop65 / nullif(c.n_inhabited * 0.015625, 0)) as 高齢者密度_人per_km2,
             -- ★主指標: 変種D（道路距離500m以上）。分母は道路距離データがあるメッシュ。
             c.popD65 as 道路距離対象_65歳以上人口,
             c.popDall as 道路距離対象_総人口,
             c.outD as 圏外65歳以上人口_変種D,
             round(c.outD / nullif(c.popD65,0), 4) as 圏外率_変種D,
             c.outD_all as 圏外総人口_変種D,
             round(c.outD_all / nullif(c.popDall,0), 4) as 圏外率_変種D_総人口,
             -- 対照: 変種A（同一500mメッシュ内の存否）/ 変種C125（直線500m）
             c.outA as 圏外65歳以上人口_変種A,
             round(c.outA / nullif(c.pop65,0), 4) as 圏外率_変種A,
             c.outA_all as 圏外総人口_変種A,
             round(c.outA_all / nullif(c.pop_all,0), 4) as 圏外率_変種A_総人口,
             c.outA_sm as 圏外65歳以上人口_supermarketのみ,
             c.outC as 圏外65歳以上人口_変種C125,
             round(c.outC / nullif(c.pop65,0), 4) as 圏外率_変種C125,
             -- 参考: 農水省公表値。**3条件込みなので本推計と直接は比べられない**。
             -- 比が1を超えたら論理矛盾、という必要条件のチェックにだけ使う。
             round(m.maff_rate/100, 4) as 農水省_困難人口割合,
             round(m.maff_pop) as 農水省_困難人口,
             round((m.maff_rate/100) / nullif(c.outD / nullif(c.popD65,0), 0), 3) as 比_農水省÷変種D,
             round((m.maff_rate/100) / nullif(c.outA / nullif(c.pop65,0), 0), 3) as 比_農水省÷変種A,
             round((m.maff_rate/100) / nullif(c.outC / nullif(c.pop65,0), 0), 3) as 比_農水省÷変種C125
      from (select k.city_code, sum(pop_65over) pop65, sum(pop_total) pop_all,
                   count(*) filter (where pop_total > 0) n_inhabited,
                   sum(pop_65over) filter (where outD is not null) popD65,
                   sum(pop_total)  filter (where outD is not null) popDall,
                   sum(case when not (has_sm or has_cv or has_dg or has_fr) then pop_65over else 0 end) outA,
                   sum(case when not (has_sm or has_cv or has_dg or has_fr) then pop_total  else 0 end) outA_all,
                   sum(case when not has_sm then pop_65over else 0 end) outA_sm,
                   sum(case when not inC then pop_65over else 0 end) outC,
                   coalesce(sum(pop_65over) filter (where outD), 0) outD,
                   coalesce(sum(pop_total)  filter (where outD), 0) outD_all
            from cov join citymap k on cov.city_code = k.raw_code
            group by 1) c
      left join maff m using (city_code)
      order by 圏外率_変種D desc nulls last""")
    con.execute(f"copy city to '{OUT_CITY}' (header, delimiter ',')")

    # ---- 6b. 都道府県別（主指標は変種D。ばらつきを県単位で見る。47県運用の本体）----
    con.execute("""create table pref as
      select substr(市区町村コード,1,2) as 都道府県コード,
             count(*) as 市区町村数,
             sum(総人口) as 総人口,
             sum(総65歳以上人口) as 総65歳以上人口,
             -- ★主指標: 変種D
             sum(圏外65歳以上人口_変種D) as 圏外65歳以上人口_変種D,
             round(sum(圏外65歳以上人口_変種D) / nullif(sum(道路距離対象_65歳以上人口),0), 4) as 圏外率_変種D,
             sum(圏外総人口_変種D) as 圏外総人口_変種D,
             round(sum(圏外総人口_変種D) / nullif(sum(道路距離対象_総人口),0), 4) as 圏外率_変種D_総人口,
             -- 対照
             sum(圏外65歳以上人口_変種A) as 圏外65歳以上人口_変種A,
             round(sum(圏外65歳以上人口_変種A) / nullif(sum(総65歳以上人口),0), 4) as 圏外率_変種A,
             round(sum(圏外総人口_変種A) / nullif(sum(総人口),0), 4) as 圏外率_変種A_総人口,
             round(sum(圏外65歳以上人口_変種C125) / nullif(sum(総65歳以上人口),0), 4) as 圏外率_変種C125,
             -- 参考: 農水省公表値との突合（必要条件チェック）
             round(sum(農水省_困難人口) / nullif(sum(総65歳以上人口),0), 4) as 農水省_困難人口割合,
             round(sum(農水省_困難人口) / nullif(sum(圏外65歳以上人口_変種D),0), 3) as 比_農水省÷変種D,
             round(sum(農水省_困難人口) / nullif(sum(圏外65歳以上人口_変種A),0), 3) as 比_農水省÷変種A,
             round(median("比_農水省÷変種D"), 3) as 比Dの中央値,
             round(stddev("比_農水省÷変種D"), 3) as 比Dのsd,
             round(corr(圏外率_変種D, 農水省_困難人口割合), 3) as 相関rD,
             round(corr(圏外率_変種A, 農水省_困難人口割合), 3) as 相関rA,
             count(*) filter (where "比_農水省÷変種D" > 1) as 比Dが1超の市区町村,
             count(*) filter (where "比_農水省÷変種A" > 1) as 比が1超の市区町村,
             count(*) filter (where "比_農水省÷変種C125" > 1) as 比C125が1超の市区町村
      from city where 農水省_困難人口 is not null
      group by 1 order by 圏外率_変種D desc nulls last""")
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
                 not c.inC as out500m_C125,
                 -- ★主指標 変種D: 道路距離500m超。道路距離データが無いメッシュは NULL
                 c.outD as out500m_road
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

    # ---- 7. 推計値（★これが成果物。条件は「店舗まで500m以上」だけ）----
    print("\n=== 推計: 食料品店まで500m以上の人口（対象県計・自動車利用は掛けていない）===")
    a = con.execute("""select sum(総人口), sum(総65歳以上人口),
                              sum(道路距離対象_総人口), sum(道路距離対象_65歳以上人口),
                              sum(圏外総人口_変種D), sum(圏外65歳以上人口_変種D),
                              sum(圏外総人口_変種A), sum(圏外65歳以上人口_変種A),
                              sum(圏外65歳以上人口_変種C125), sum(農水省_困難人口)
                       from city where 農水省_困難人口 is not null""").fetchone()
    pop_all, pop65, dnA, dn65, dA, d65, aA, a65, c65, maff = a
    print(f"  総人口                       {pop_all:>10,}")
    print(f"  65歳以上人口                 {pop65:>10,}")
    if dn65:
        print(f"  ★変種D 道路距離500m以上（総人口）    {dA:>10,}  ({dA/dnA*100:.1f}%)")
        print(f"  ★変種D 道路距離500m以上（65歳以上）  {d65:>10,}  ({d65/dn65*100:.1f}%)")
        print(f"     ↑ 分母は道路距離データがあるメッシュ "
              f"（総人口 {dnA:,} / 65歳以上 {dn65:,}・"
              f"欠落 {pop65-dn65:,}人 = {(pop65-dn65)/pop65*100:.2f}%）")
    print(f"  （対照）変種A メッシュ存否（総人口）  {aA:>10,}  ({aA/pop_all*100:.1f}%)")
    print(f"  （対照）変種A メッシュ存否（65歳以上）{a65:>10,}  ({a65/pop65*100:.1f}%)")
    print(f"  （対照）変種C125 直線500m（65歳以上） {c65:>10,}  ({c65/pop65*100:.1f}%)")

    # ---- 8. 参考: 農水省公表値との突合（必要条件チェック。精度の指標ではない）----
    print("\n=== 参考: 農水省 公表値との突合（3条件込みなので直接比較はできない）===")
    print(f"  農水省 困難人口（公表）        {int(maff):>10,}  ({maff/pop65*100:.1f}%)")
    if d65:
        print(f"  比 農水省÷変種D               {maff/d65:>10.3f}  ← 暗黙の自動車利用困難率")
    print(f"  比 農水省÷変種A               {maff/a65:>10.3f}")
    print(f"  比 農水省÷変種C125            {maff/c65:>10.3f}")

    for col, rate in (("比_農水省÷変種D", "圏外率_変種D"),
                      ("比_農水省÷変種A", "圏外率_変種A"),
                      ("比_農水省÷変種C125", "圏外率_変種C125")):
        print(f"\n=== {col} の分布（1を超えたら操作化が不整合）===")
        q = con.execute(f"""select count(*), min({col}), quantile_cont({col},0.1),
                                  median({col}), quantile_cont({col},0.9),
                                  max({col}), stddev({col}),
                                  corr({rate}, 農水省_困難人口割合),
                                  count(*) filter (where {col} > 1)
                           from city where {col} is not null""").fetchone()
        if not q[0]:
            print("  （データなし。この変種は算出されていない）")
            continue
        print(f"  n={q[0]}  min={q[1]:.3f}  p10={q[2]:.3f}  median={q[3]:.3f}  "
              f"p90={q[4]:.3f}  max={q[5]:.3f}  sd={q[6]:.3f}")
        print(f"  相関 r={q[7]:.3f}   比>1 の市区町村={q[8]} 件"
              f"{'  ← 不整合' if q[8] else '  ← 整合'}")

    # 主指標で外れを見る。変種Dが無ければ変種Aに落とす。
    main_v = "変種D" if has_road else "変種A"
    for title, order in (
            ("比が小さい＝圏外率が過大（店舗レイヤの穴の疑い）", ""),
            ("比が大きい＝圏外率が過小（店舗の偽陽性・過剰計上の疑い）", " desc")):
        print(f"\n=== {title} top10（主指標 {main_v}）===")
        for r in con.execute(f"""
                select 市区町村コード, 市区町村名, 総65歳以上人口, 圏外率_{main_v},
                       農水省_困難人口割合, 比_農水省÷{main_v}
                from city
                where 比_農水省÷{main_v} is not null and 総65歳以上人口 >= 3000
                order by 比_農水省÷{main_v}{order} limit 10""").fetchall():
            print(f"  {r[0]} {str(r[1]):12s} 65+={r[2]:>7,} "
                  f"{main_v}圏外={r[3]*100:5.1f}% 農水省={r[4]*100:5.1f}% 比={r[5]}")


if __name__ == "__main__":
    main()
