#!/usr/bin/env python3
"""
国勢調査2020 125mメッシュ（6次メッシュ・L6）人口を e-Stat 統計GIS から取得する。

なぜ125mか
----------
農水省「食料品アクセス困難人口」は 500mメッシュ人口 × 最近隣店舗距離500m で定義されるが、
500mメッシュ人口をそのまま使うと **メッシュ重心が実際の居住位置から最大 ~350m ずれる**
（500m格子の対角の半分）。この誤差が距離判定と同じオーダーになるため、
「重心から実距離500m以内」という素直な操作化が成立しなかった
（`docs/検証_アクセス困難人口_メッシュ単位.md` の変種C＝比>1 の市区町村が6件出て不整合）。

125mメッシュなら重心誤差は最大 ~88m まで落ちる。人口の空間分布を細かく持てるので、
- 農水省の再現（500mメッシュ単位の存否）は **11桁コードの先頭9桁＝500mメッシュコード** で維持でき、
- 同時に「重心から実距離500m」を意味のある精度で測れる。
両方を同じ人口レイヤから出して比較するのが本リポジトリの設計。

データ
------
e-Stat 統計GIS のダウンロードAPI（**appId 不要**）:

  https://www.e-stat.go.jp/gis/statmap-search/data?statsId=T001225&code=<1次メッシュ>&downloadType=2

統計表 T001225（男女別人口・年齢別人口・世帯数, 125mメッシュ, 令和2年国勢調査 その1）。
**列の並びは500mメッシュ版 T001141 と完全に同一**:
  T001225001 = 人口（総数）
  T001225019 = 65歳以上人口 総数   ← 農水省の困難人口（65歳以上）に対応
  T001225022 = 75歳以上人口 総数   ← 農水省 表5 の 75歳以上 と突合する用
「総数・男・女」の3列組が並ぶ構造から数える（001-003 総数, 004-006 0-14歳, 007-009 15歳以上,
010-012 15-64歳, 013-015 18歳以上, 016-018 20歳以上, 019-021 65歳以上, 022-024 75歳以上）。
**022 は75歳以上**なので取り違えないこと（500m版で踏んだ落とし穴）。

秘匿処理: HTKSYORI=2 の行は自メッシュの値が '*' で、HTKSAKI のメッシュに合算されている。
**'*' は 0 として扱う**。125mでは秘匿が500mより桁違いに多い（全国 2,820,831 メッシュ中
1,017,247 メッシュが 65歳以上 '*'）ので、県計・市区町村計での目減りを必ず確認すること。

使い方:
  # 既定: 手元の統合済み parquet（input/2020_pop_census_mesh125.parquet）を畳む。県名は不要
  python scripts/01_fetch_mesh_population.py
  # ファイルが無いとき: e-Stat から1次メッシュ単位で取る（県名で範囲を指定）
  python scripts/01_fetch_mesh_population.py 高知県 島根県 宮城県
出力:
  data/mesh/mesh125_pop.parquet
    (mesh_code[11桁], mesh500_code[9桁], lat, lng, pop_total, pop_65over, pop_75over)
"""
import io
import os
import sys
import urllib.request
import zipfile

import duckdb

PREF_GEOJSON = "data/japan_pref.geojson"
PREF_URL = "https://raw.githubusercontent.com/dataofjapan/land/master/japan.geojson"
CACHE_DIR = "data/mesh/cache"
OUT_PARQUET = "data/mesh/mesh125_pop.parquet"

STATS_ID = "T001225"
DL = ("https://www.e-stat.go.jp/gis/statmap-search/data"
      f"?statsId={STATS_ID}&code={{mesh1}}&downloadType=2")

COL_TOTAL = f"{STATS_ID}001"
COL_65OVER = f"{STATS_ID}019"
COL_75OVER = f"{STATS_ID}022"

# **既定は手元の統合済み parquet を読む**（姉妹リポジトリ japan-transit-desert-analysis-125 と
# 同じ流儀・同じファイル名）。全国 2,820,831 メッシュが1本に入っているので県名の指定は不要で、
# 絞り込みは検証器側が県境界で行う。
# 置き場所: input/2020_pop_census_mesh125.parquet（.gitignore）。別の場所にあるなら
#   MESH125_PARQUET=".../2020_pop_census_mesh125.parquet" python scripts/01_fetch_mesh_population.py
# ファイルが無いときだけ e-Stat から1次メッシュ単位で取りに行く（全国だと151個・数十分かかる）。
DEFAULT_LOCAL = "input/2020_pop_census_mesh125.parquet"
LOCAL_PARQUET = os.environ.get("MESH125_PARQUET") or (
    DEFAULT_LOCAL if os.path.exists(DEFAULT_LOCAL) else "")
LOCAL_COLS = {
    "pop_total": "人口（総数）",
    "pop_65over": "６５歳以上人口　総数",
    "pop_75over": "７５歳以上人口　総数",
}


def ensure_pref_geojson():
    if not os.path.exists(PREF_GEOJSON):
        os.makedirs(os.path.dirname(PREF_GEOJSON), exist_ok=True)
        print(f"取得: {PREF_URL} -> {PREF_GEOJSON}")
        urllib.request.urlretrieve(PREF_URL, PREF_GEOJSON)


def mesh1_codes_for(con, prefs):
    """対象県を覆う1次メッシュコードを列挙する（1次メッシュ = 緯度1/1.5度 x 経度1度）。

    県ごとの bbox から候補を作る。県をまたぐ bbox の合成は無関係な内陸まで拾うので、
    必ず**県単位**で列挙して和集合を取る。
    """
    codes = set()
    for pref in prefs:
        q = con.execute(
            "select min(ST_XMin(geom)), min(ST_YMin(geom)), max(ST_XMax(geom)), max(ST_YMax(geom)) "
            f"from ST_Read('{PREF_GEOJSON}') where nam_ja = '{pref}'"
        ).fetchone()
        if q[0] is None:
            sys.exit(f"県名が見つからない: {pref}")
        xmin, ymin, xmax, ymax = q
        for p in range(int(ymin * 1.5), int(ymax * 1.5) + 1):
            for u in range(int(xmin) - 100, int(xmax) - 100 + 1):
                codes.add(f"{p:02d}{u:02d}")
    return sorted(codes)


def fetch_mesh1(code):
    path = os.path.join(CACHE_DIR, f"{STATS_ID}_{code}.zip")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    os.makedirs(CACHE_DIR, exist_ok=True)
    url = DL.format(mesh1=code)
    try:
        with urllib.request.urlopen(url, timeout=300) as r:
            body = r.read()
    except Exception as e:  # 海上のみ等でデータが無い1次メッシュは404になる
        print(f"  {code}: 取得不可 ({e})")
        return None
    if not body.startswith(b"PK"):
        print(f"  {code}: zipでない（データ無しと判断）")
        return None
    with open(path, "wb") as f:
        f.write(body)
    return path


def parse_zip(path):
    """zip内のCSVから (mesh_code, pop_total, pop_65over, pop_75over) を返す。"""
    rows = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            text = z.read(name).decode("cp932", errors="replace")
            rdr = io.StringIO(text)
            header = next(rdr).rstrip("\r\n").split(",")
            next(rdr)  # 2行目 = 日本語見出し
            try:
                i_key = header.index("KEY_CODE")
                i_tot = header.index(COL_TOTAL)
                i_65 = header.index(COL_65OVER)
                i_75 = header.index(COL_75OVER)
            except ValueError:
                print(f"  {name}: 想定列が無い（スキップ）")
                continue
            for line in rdr:
                c = line.rstrip("\r\n").split(",")
                if len(c) <= i_75 or not c[i_key].strip():
                    continue
                rows.append((c[i_key].strip(), to_int(c[i_tot]),
                             to_int(c[i_65]), to_int(c[i_75])))
    return rows


def to_int(v):
    """'*'（秘匿）・'-'・空 は 0。"""
    v = v.strip()
    if v in ("", "*", "-", "X"):
        return 0
    try:
        return int(float(v))
    except ValueError:
        return 0


def mesh125_centroid(code):
    """11桁 125mメッシュ（L6）コード -> 中心座標 (lat, lng)。Noneなら対象外。

    11桁の構成: 3次メッシュ8桁 + 500m区分(1-4) + 250m区分(1-4) + 125m区分(1-4)。
    区分は南西を1として Z 順（1=SW, 2=SE, 3=NW, 4=NE）。
    先頭9桁がそのまま 500mメッシュコードになる（＝農水省再現の判定キー）。
    """
    if len(code) != 11 or not code.isdigit():
        return None
    p, u = int(code[0:2]), int(code[2:4])
    q, v = int(code[4]), int(code[5])
    r, w = int(code[6]), int(code[7])
    m4, m5, m6 = int(code[8]), int(code[9]), int(code[10])
    if not (1 <= m4 <= 4 and 1 <= m5 <= 4 and 1 <= m6 <= 4):
        return None
    # 南西端。1次=1/1.5度x1度, 3次=1/120x1/80, 以降は2分割ごとに半分
    lat = p / 1.5 + q / 12 + r / 120
    lng = 100 + u + v / 8 + w / 80
    lat += ((m4 - 1) // 2) / 240 + ((m5 - 1) // 2) / 480 + ((m6 - 1) // 2) / 960
    lng += ((m4 - 1) % 2) / 160 + ((m5 - 1) % 2) / 320 + ((m6 - 1) % 2) / 640
    # 南西端 -> 中心（125mメッシュ = 緯度1/960度 x 経度1/640度）
    return lat + 1 / 1920, lng + 1 / 1280


def from_local(con):
    """手元の統合済み parquet（全国）を畳んで OUT_PARQUET を作る。

    e-Stat から取り直す代わりの経路。秘匿 '*' は VARCHAR で入っているので try_cast で 0 に落とす
    （ダウンロード経路の `to_int` と同じ扱い）。重心はダウンロード経路と同じ
    `mesh125_centroid` で 11桁コードから計算し、geometry 列には触らない。
    """
    print(f"手元の統合済みデータを使う: {LOCAL_PARQUET}")
    con.create_function("c_lat", lambda c: (mesh125_centroid(c) or (None, None))[0],
                        ["VARCHAR"], "DOUBLE")
    con.create_function("c_lng", lambda c: (mesh125_centroid(c) or (None, None))[1],
                        ["VARCHAR"], "DOUBLE")
    q = f"""select KEY_CODE as mesh_code, substr(KEY_CODE,1,9) as mesh500_code,
                   c_lat(KEY_CODE) as lat, c_lng(KEY_CODE) as lng,
                   coalesce(try_cast("{LOCAL_COLS['pop_total']}" as bigint),0) as pop_total,
                   coalesce(try_cast("{LOCAL_COLS['pop_65over']}" as bigint),0) as pop_65over,
                   coalesce(try_cast("{LOCAL_COLS['pop_75over']}" as bigint),0) as pop_75over
            from read_parquet('{LOCAL_PARQUET}')
            where length(KEY_CODE) = 11"""
    con.execute(f"create table mesh as {q}")
    bad = con.execute("select count(*) from mesh where lat is null or lng is null").fetchone()[0]
    if bad:
        print(f"  重心を計算できないコードを除外: {bad:,} 件")
        con.execute("delete from mesh where lat is null or lng is null")


def report_and_write(con):
    """`mesh` テーブルを検算して OUT_PARQUET に書く。両経路で共通。"""
    dup = con.execute("select count(*) - count(distinct mesh_code) from mesh").fetchone()[0]
    if dup:
        sys.exit(f"メッシュコードが重複している: {dup:,} 件（入力を確認）")
    n, n500, t, o, o75 = con.execute(
        "select count(*), count(distinct mesh500_code), sum(pop_total), "
        "sum(pop_65over), sum(pop_75over) from mesh").fetchone()
    print(f"合計: {n:,} メッシュ（500mメッシュ換算 {n500:,}） / "
          f"総人口 {t:,} / 65歳以上 {o:,} / 75歳以上 {o75:,}")
    con.execute(f"copy mesh to '{OUT_PARQUET}' (FORMAT parquet)")
    print(f"出力: {OUT_PARQUET}")


def main():
    prefs = sys.argv[1:]
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")

    if LOCAL_PARQUET:
        if not os.path.exists(LOCAL_PARQUET):
            sys.exit(f"MESH125_PARQUET が見つからない: {LOCAL_PARQUET}")
        os.makedirs(os.path.dirname(OUT_PARQUET), exist_ok=True)
        from_local(con)
        report_and_write(con)
        return

    if not prefs:
        sys.exit("使い方: python scripts/01_fetch_mesh_population.py 高知県 [島根県 ...]\n"
                 "        （手元に全国の統合済み parquet があるなら MESH125_PARQUET=... で渡す）")

    ensure_pref_geojson()

    codes = mesh1_codes_for(con, prefs)
    print(f"対象県 {prefs} を覆う1次メッシュ候補 {len(codes)} 個")

    os.makedirs(os.path.dirname(OUT_PARQUET), exist_ok=True)
    con.execute("create table mesh(mesh_code varchar, mesh500_code varchar, "
                "lat double, lng double, "
                "pop_total bigint, pop_65over bigint, pop_75over bigint)")

    # 47県だと全国 2,820,831 メッシュになる。Python 側に全件溜めると数GB 積み上がるので、
    # 1次メッシュごとに DuckDB へ流し込んでメモリを平らにする。
    skipped = 0
    for code in codes:
        path = fetch_mesh1(code)
        if not path:
            continue
        recs = []
        for mesh, tot, o65, o75 in parse_zip(path):
            c = mesh125_centroid(mesh)
            if c is None:
                skipped += 1
                continue
            recs.append((mesh, mesh[:9], c[0], c[1], tot, o65, o75))
        con.executemany("insert into mesh values (?,?,?,?,?,?,?)", recs)
        print(f"  {code}: {len(recs):,} メッシュ")
    if skipped:
        print(f"桁数不一致で除外 {skipped:,} 件")

    report_and_write(con)


if __name__ == "__main__":
    main()
