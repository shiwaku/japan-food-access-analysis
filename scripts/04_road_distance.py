#!/usr/bin/env python3
"""
食料品店までの**道路距離**を125mメッシュごとに算出する（Multi-source Dijkstra）。

算出方法は姉妹リポジトリ **japan-transit-desert-analysis-125** の
`scripts/02_calc_transit_desert.py` を踏襲する（`-125` の付かない250m版・`-100` 版ではない）。
違いは施設が鉄道駅・バス停ではなく食料品店であることだけ。

  ① 道路リンク（N13・walkモード）から scipy CSR グラフを構築
  ② 店舗を最近傍1道路ノードにスナップ（バス停と同じ扱い。駅の4象限スナップはしない）
  ③ スーパーソースを足した Multi-source Dijkstra で全ノードの最短時間を得る
  ④ アクセスリンク（L6＝125mメッシュ↔道路ノード）経由でメッシュ距離に変換
  ⑤ walk 3.6km/h = 60m/分 で 500m = 8.33分 を閾値にする

なぜ道路距離だけで測るのか
--------------------------
**このリポジトリの距離判定は道路距離1本にした**（2026-08-28）。かつては

  変種A    同一500mメッシュ内に店舗があるか（農水省の公表値を再現できる二値近似）
  変種C125 メッシュ重心から直線500m以内に店舗があるか

も併走させていたが、**どちらも実際に歩く距離を測っていない**。変種Aは隣メッシュ10m先の店に
救われない実効半径250m程度のマス目判定で、距離の代理としては粗すぎる。
農水省の公表値の再現は、その推計方法が**確率版**（メッシュ内位置についての面積比。
`docs/検証_アクセス困難人口_メッシュ単位.md` 10.5）と判明した時点で二値では届かないことが
確定したので、二値近似を保持する理由が無くなった。

→ **この段階（04）が距離判定の唯一の出所**になる。後段の `02` は店舗レイヤを読まず、
   ここが出す parquet だけを見る。

業態の入れ子
------------
「どの業態を足すと圏外が何人減るか」を道路距離のまま出せるように、**入れ子集合ごとに
Dijkstra を回す**（4回・全国で約4分）。農水省の対象業種は生鮮専門店もドラッグストアも含む。

  S1 supermarket のみ
  S2 ＋convenience
  S3 ＋drugstore
  S4 ＋fresh_food（＝全カテゴリ・主指標）

入れ子なので S1 ≥ S2 ≥ S3 ≥ S4 の順に距離は縮む（部分集合の距離が本体より短いことは無い）。
`02` がこの単調性を検算する。

入力
----
  input/food_store_master_atp_super.parquet     店舗（lat / lng / cat）
  <NETWORK_DIR>/KSJ_N13-24_nationwide_walk_道路リンク.parquet
  <NETWORK_DIR>/KSJ_N13-24_nationwide_walk_道路ノード.parquet
  <NETWORK_DIR>/KSJ_N13-24_nationwide_walk_アクセスリンク_L6.parquet
  data/mesh/mesh125_pop.parquet                 有人メッシュのフィルタに使う

出力
----
  data/mesh_road_dist.parquet
    mesh_code
    dist_S1_min / dist_S2_min / dist_S3_min / dist_S4_min   最寄り店舗までの徒歩分
    out500m_S1  / out500m_S2  / out500m_S3  / out500m_S4    500m超か
  主指標は **S4**（全カテゴリ）。地図の緑／橙の区分は S1（スーパー）を使う。
"""
import os
import sys
import time

import numpy as np
import pandas as pd

STORES = os.environ.get("FOOD_STORES", "input/food_store_master_atp_super.parquet")
MESH = "data/mesh/mesh125_pop.parquet"
OUT = os.environ.get("OUT_ROAD", "data/mesh_road_dist.parquet")

# -125 が使っているのと同じ全国 walk ネットワーク（リポジトリ外）。
# 既定はリポジトリの隣に ksj/ が並んでいる前提の相対パス。`NETWORK_DIR` で差し替えられる。
# **`/c/Users/...` のような Git Bash 形式は Windows の Python が解決できない**ので正規化する
# （素の os.path.exists が False を返し「入力が無い」で落ちる）。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_NETWORK_DIR = os.path.join(
    os.path.dirname(_REPO_ROOT),
    "ksj", "ksj-roadcenterline-route-search", "01_MakeNetwork", "nationwide_walk")


def _normalize_dir(p):
    """`/c/Users/...` → `C:/Users/...`（Git Bash 形式の吸収）。それ以外はそのまま返す。"""
    if len(p) > 2 and p[0] == "/" and p[1].isalpha() and p[2] == "/":
        return f"{p[1].upper()}:{p[2:]}"
    return p


NETWORK_DIR = _normalize_dir(os.environ.get("NETWORK_DIR", _DEFAULT_NETWORK_DIR))
LINKS = os.path.join(NETWORK_DIR, "KSJ_N13-24_nationwide_walk_道路リンク.parquet")
NODES = os.path.join(NETWORK_DIR, "KSJ_N13-24_nationwide_walk_道路ノード.parquet")
ACCESS = os.path.join(NETWORK_DIR, "KSJ_N13-24_nationwide_walk_アクセスリンク_L6.parquet")

# walk 3.6 km/h = 60 m/分。農水省の閾値は 500m。
THRESHOLD_MIN = 500 / 60  # 8.33分

# 連結成分がこの規模未満のノードはスナップ対象から外す（-125 と同じ）
MIN_COMPONENT = 1000

# 業態の入れ子。02 のカテゴリ感度がそのままこの順で「前段からの削減人数」を出す。
# 農水省の対象業種は生鮮専門店・ドラッグストアも含む（access_genjo.html 原文）。
NESTED = [
    ("S1", ["supermarket"]),
    ("S2", ["supermarket", "convenience"]),
    ("S3", ["supermarket", "convenience", "drugstore"]),
    ("S4", ["supermarket", "convenience", "drugstore", "fresh_food"]),
]


def build_graph(links):
    """道路リンクから scipy CSR グラフを構築（-125 の build_graph と同じ）。"""
    from scipy.sparse import csr_matrix
    n1 = links["node1"].astype(np.int64).to_numpy()
    n2 = links["node2"].astype(np.int64).to_numpy()
    ws = links["time_001min"].astype(float).to_numpy() * 0.01  # 分単位

    src = np.concatenate([n1, n2])
    dst = np.concatenate([n2, n1])
    w = np.concatenate([ws, ws])

    unique = np.unique(np.concatenate([src, dst]))
    # 大きいので dict ではなく searchsorted で引く
    rows = np.searchsorted(unique, src).astype(np.int32)
    cols = np.searchsorted(unique, dst).astype(np.int32)
    G = csr_matrix((w, (rows, cols)), shape=(len(unique), len(unique)))
    return unique, G


def multisource_dijkstra(G, source_idxs):
    """スーパーソースを足して全始点を同時投入する（-125 と同じ）。"""
    from scipy.sparse import coo_matrix, csr_matrix
    from scipy.sparse import vstack as sp_vstack
    from scipy.sparse.csgraph import dijkstra as sp_dijkstra

    nv = G.shape[0]
    extra = coo_matrix(
        (np.zeros(len(source_idxs)),
         (np.zeros(len(source_idxs), dtype=np.int32), source_idxs.astype(np.int32))),
        shape=(1, nv + 1)).tocsr()
    G_pad = csr_matrix((G.data, G.indices, G.indptr), shape=(nv, nv + 1))
    G_ext = sp_vstack([G_pad, extra], format="csr")
    return sp_dijkstra(G_ext, directed=True, indices=nv)[:nv]


def snap_to_nodes(coords, node_coords, node_ids, unique):
    """店舗を最近傍1道路ノードにスナップしてグラフ添字を返す。

    -125 のバス停と同じ扱い。駅でやっている NE/NW/SE/SW の4象限スナップはしない
    （線路のような「またぐと遠回りになる線状障害物」が店舗には無いため）。
    """
    from scipy.spatial import KDTree
    tree = KDTree(node_coords)
    _, snap_idx = tree.query(coords)
    nids = node_ids[snap_idx]
    pos = np.searchsorted(unique, nids)
    ok = (pos < len(unique)) & (unique[np.minimum(pos, len(unique) - 1)] == nids)
    return np.unique(pos[ok].astype(np.int32))


def main():
    t0 = time.time()
    import geopandas as gpd

    for p in (STORES, MESH, LINKS, NODES, ACCESS):
        if not os.path.exists(p):
            sys.exit(f"入力が無い: {p}")

    print(f"店舗読み込み: {STORES}")
    st = pd.read_parquet(STORES, columns=["lat", "lng", "cat"])
    st = st.dropna(subset=["lat", "lng"])
    print(f"  店舗 {len(st):,} 件（うち supermarket {int((st['cat']=='supermarket').sum()):,}）")

    print("道路ネットワーク読み込み…")
    links = pd.read_parquet(LINKS, columns=["node1", "node2", "time_001min"])
    nodes = gpd.read_parquet(NODES)
    access = pd.read_parquet(ACCESS, columns=["mesh_code", "road_node", "time_001min"])
    print(f"  リンク {len(links):,} / ノード {len(nodes):,} / アクセス {len(access):,}"
          f"  ({time.time()-t0:.0f}s)")

    # 有人メッシュに絞る（-125 と同じ。人口0メッシュを Dijkstra 後の変換から外す）
    pop = pd.read_parquet(MESH, columns=["mesh_code"])
    populated = set(pop["mesh_code"].astype(str))
    n_before = len(access)
    access = access[access["mesh_code"].astype(str).isin(populated)].reset_index(drop=True)
    print(f"  有人メッシュフィルタ: {n_before:,} → {len(access):,}  ({time.time()-t0:.0f}s)")

    print("グラフ構築…")
    unique, G = build_graph(links)
    del links
    print(f"  ノード {G.shape[0]:,} / エッジ {G.nnz:,}  ({time.time()-t0:.0f}s)")

    print("連結成分解析…")
    from scipy.sparse.csgraph import connected_components as sp_cc
    n_comp, labels = sp_cc(G, directed=False)
    sizes = np.bincount(labels)
    big = np.where(sizes >= MIN_COMPONENT)[0]
    keep = np.isin(labels, big)
    connected_ids = unique[keep]
    print(f"  成分 {n_comp:,} / 有効成分 {len(big):,} / 有効ノード {keep.sum():,}"
          f"（孤立除外 {(~keep).sum():,}）  ({time.time()-t0:.0f}s)")

    node_ids_all = nodes["node_id"].astype(np.int64).to_numpy()
    in_conn = np.isin(node_ids_all, connected_ids)
    node_ids = node_ids_all[in_conn]
    node_coords = np.column_stack([nodes.geometry.y.to_numpy()[in_conn],
                                   nodes.geometry.x.to_numpy()[in_conn]])
    del nodes

    mesh_codes = access["mesh_code"].astype(str).to_numpy()
    road_nids = access["road_node"].astype(np.int64).to_numpy()
    acc_time = access["time_001min"].astype(float).to_numpy() * 0.01
    pos = np.searchsorted(unique, road_nids)
    pos_clipped = np.minimum(pos, len(unique) - 1)
    valid = unique[pos_clipped] == road_nids
    safe = np.where(valid, pos_clipped, 0)

    result = {"mesh_code": mesh_codes}
    for label, cats in NESTED:
        sub = st[st["cat"].isin(cats)]
        coords = np.column_stack([sub["lat"].to_numpy(), sub["lng"].to_numpy()])
        print(f"{label}（{'+'.join(cats)}・{len(sub):,}店）: スナップ…")
        idxs = snap_to_nodes(coords, node_coords, node_ids, unique)
        print(f"  スナップ先ノード {len(idxs):,}  ({time.time()-t0:.0f}s)")
        print(f"{label}: Multi-source Dijkstra…")
        dist_node = multisource_dijkstra(G, idxs)
        print(f"  完了  ({time.time()-t0:.0f}s)")
        d = np.where(valid, dist_node[safe] + acc_time, np.inf)
        result[f"dist_{label}_min"] = np.where(np.isfinite(d), np.round(d, 2), np.nan)
        result[f"out500m_{label}"] = d > THRESHOLD_MIN

    df = pd.DataFrame(result)
    # 1メッシュに複数のアクセスリンクが張られることがあるので最小距離に畳む。
    # 到達不能は dist が NaN・out500m が True なので、min でどちらも正しく畳める
    # （NaN は min で無視され、True/False の min は False = 1つでも圏内なら圏内）。
    agg = {f"dist_{lab}_min": (f"dist_{lab}_min", "min") for lab, _ in NESTED}
    agg.update({f"out500m_{lab}": (f"out500m_{lab}", "min") for lab, _ in NESTED})
    df = df.groupby("mesh_code", as_index=False).agg(**agg)

    # 入れ子なので S1 ⊇ S2 ⊇ S3 ⊇ S4 の順に距離は縮む。違反したら実装がおかしい。
    for (a, _), (b, _) in zip(NESTED, NESTED[1:]):
        bad = int((df[f"dist_{b}_min"] > df[f"dist_{a}_min"] + 1e-9).sum())
        print(f"  検算 {a} ≥ {b}: 違反 {bad} 件{'' if bad == 0 else '  ⚠'}")

    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    df.to_parquet(OUT, index=False, compression="zstd")
    print(f"\n出力: {OUT}（{len(df):,} メッシュ）  ({time.time()-t0:.0f}s)")
    for lab, cats in NESTED:
        n_out = int(df[f"out500m_{lab}"].sum())
        print(f"  {lab} {'+'.join(cats):48s} 圏外 {n_out:>9,} = {n_out/len(df)*100:5.1f}%")


if __name__ == "__main__":
    main()
