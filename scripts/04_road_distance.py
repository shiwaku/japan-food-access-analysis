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

なぜ道路距離を測るのか
----------------------
`docs/検証_アクセス困難人口_メッシュ単位.md` の通り、農水省の公表値は
**同一500mメッシュ内の存否（変種A）**でしか再現できず、**都市部では比>1 が出て破れている**
（119市区町村の73.9%）。破れを解消するには**都市部でより多くの圏外を生む**操作化が要るが、
直線500m（変種C125）は圏外が減る方向で逆に悪化した（比>1 が465件）。

**道路距離は直線距離の1.2〜1.5倍**になるため、圏外が増える方向に効く。
-125 が国交省の直線バッファ版（735万人）に対し道路距離版で1,847万人＝2.5倍を出しているのと
同じ構造で、**未検証のまま残っていた唯一の候補**。これを変種Dとして測る。

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
    mesh_code / dist_store_min / dist_super_min / out500m_road / out500m_road_super
"""
import os
import sys
import time

import numpy as np
import pandas as pd

STORES = os.environ.get("FOOD_STORES", "input/food_store_master_atp_super.parquet")
MESH = "data/mesh/mesh125_pop.parquet"
OUT = os.environ.get("OUT_ROAD", "data/mesh_road_dist.parquet")

# -125 が使っているのと同じ全国 walk ネットワーク（リポジトリ外）
NETWORK_DIR = os.environ.get(
    "NETWORK_DIR",
    "/c/Users/yshiw/Documents/GIS/ksj/ksj-roadcenterline-route-search/01_MakeNetwork/nationwide_walk")
LINKS = os.path.join(NETWORK_DIR, "KSJ_N13-24_nationwide_walk_道路リンク.parquet")
NODES = os.path.join(NETWORK_DIR, "KSJ_N13-24_nationwide_walk_道路ノード.parquet")
ACCESS = os.path.join(NETWORK_DIR, "KSJ_N13-24_nationwide_walk_アクセスリンク_L6.parquet")

# walk 3.6 km/h = 60 m/分。農水省の閾値は 500m。
THRESHOLD_MIN = 500 / 60  # 8.33分

# 連結成分がこの規模未満のノードはスナップ対象から外す（-125 と同じ）
MIN_COMPONENT = 1000


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
    for label, sel in (("store", slice(None)), ("super", st["cat"] == "supermarket")):
        sub = st[sel] if label == "super" else st
        coords = np.column_stack([sub["lat"].to_numpy(), sub["lng"].to_numpy()])
        print(f"{label}: スナップ…")
        idxs = snap_to_nodes(coords, node_coords, node_ids, unique)
        print(f"  スナップ先ノード {len(idxs):,}  ({time.time()-t0:.0f}s)")
        print(f"{label}: Multi-source Dijkstra…")
        dist_node = multisource_dijkstra(G, idxs)
        print(f"  完了  ({time.time()-t0:.0f}s)")
        d = np.where(valid, dist_node[safe] + acc_time, np.inf)
        result[f"dist_{label}_min"] = np.where(np.isfinite(d), np.round(d, 2), np.nan)
        result[f"out500m_road{'' if label=='store' else '_super'}"] = d > THRESHOLD_MIN

    df = pd.DataFrame(result)
    # 1メッシュに複数のアクセスリンクが張られることがあるので最小距離に畳む
    df = df.groupby("mesh_code", as_index=False).agg(
        dist_store_min=("dist_store_min", "min"),
        dist_super_min=("dist_super_min", "min"),
        out500m_road=("out500m_road", "min"),
        out500m_road_super=("out500m_road_super", "min"))
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    df.to_parquet(OUT, index=False, compression="zstd")
    n_out = int(df["out500m_road"].sum())
    print(f"\n出力: {OUT}（{len(df):,} メッシュ / うち道路距離500m圏外 {n_out:,}"
          f" = {n_out/len(df)*100:.1f}%）  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
