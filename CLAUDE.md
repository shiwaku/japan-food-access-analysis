# CLAUDE.md — japan-food-access-analysis

このリポジトリで作業するときの前提・手順・落とし穴をまとめる。次回はまずこれを読むこと。

## このリポジトリは何か

農水省「[食料品アクセス](https://www.maff.go.jp/j/shokusan/eat/access_genjo.html)」の
**食料品アクセス困難人口**を、国勢調査 **125mメッシュ人口** × 食料品店 POI で再現・検証する。

- **人口レイヤ・距離判定・公表値突合がここ**。**店舗レイヤは持たない**（外から受け取る）。
- 店舗レイヤの構築は [shiwaku/japan-food-store-master](https://github.com/shiwaku/japan-food-store-master)。
  2026-08-27 にそちらから検証器（`validate_access_difficulty.py` / `fetch_mesh_population.py`）を
  分離して発足。分離元の該当コミットは #7 / #16 / #20 / #27。
- 姉妹プロジェクト [japan-transit-desert-analysis](https://github.com/shiwaku/japan-transit-desert-analysis)
  （公共交通空白地域・国交省定義）と同じ構造の農水省版。あちらの `01_prepare_facilities.py` が
  担う施設供給を、こちらでは japan-food-store-master が担う。

## リポジトリ構成

```
scripts/
  01_fetch_mesh_population.py     e-Stat T001225 → data/mesh/mesh125_pop.parquet
  02_validate_access_difficulty.py 圏外率算出＋農水省 表5 突合 → output/*.csv
docs/
  検証_アクセス困難人口_メッシュ単位.md  一次記録（操作化の判定・カテゴリ感度・誤差）
input/     店舗レイヤの置き場（.gitignore。japan-food-store-master の parquet）
data/      メッシュ人口・境界・農水省表5（.gitignore。全部スクリプトが自動取得）
output/    市区町村別・カテゴリ感度の CSV（追跡する）
```

## 作業ワークフロー（厳守）

- **main へ直接コミット・push しない**。必ずブランチを切って PR を作成しマージする。
  ```
  git checkout -b <type>/<topic>      # feature/ fix/ docs/ 等
  git push -u origin <branch>
  gh pr create --base main --head <branch> --title "..." --body "..."
  gh pr merge <n> --squash --delete-branch
  git checkout main && git pull --ff-only origin main
  ```
- コミットメッセージ末尾に `Claude-Session: <url>` を付ける。

## ★ 判定の原則（これを外すと全部無意味になる）

**件数を足せば圏外率は必ず下がるので、下がったこと自体は判定材料にならない。**
店舗ソースの投入可否は次の2つで見る（`02_validate_access_difficulty.py` が両方出す）:

1. **比 農水省÷変種A が1を超えないこと**（超えたら操作化が論理的に不整合）
2. **農水省公表値との相関 r が上がること**

## 農水省の操作化（実測で確定済み・蒸し返さないこと）

「500m以上」の実装候補を比べた結果、**同一500mメッシュ内の店舗存否（変種A）だけが整合する**。

| 変種 | 操作化 | 圏外率 | 比 中央値 | 比>1 |
|---|---|---:|---:|---:|
| **A** | **同一500mメッシュ内に店舗があるか** | 62.4% | 0.365 | **0件** ✅ |
| B | 自メッシュ+8近傍メッシュ | 26.6% | 0.592 | 多数 ❌ |
| C125 | 125mメッシュ重心から実距離500m以内 | 40.5% | 0.501 | 5件 ❌ |

- **125mメッシュにしても変種Cの不整合は消えない**（Phase1で5件・ATP基準＋許可で10件・max 4.237）。
  当初は500m重心の粗さ（最大 ~350m ずれ）を疑ったが、~88m まで落としても比>1 が残る。
  → **操作化そのものが違う**。直線500m円ではない。この論点は決着済み。
- 相関 r だけ見ると変種C125 の方が高い（0.499 vs 0.451）が、**論理整合を満たさないので採らない**。
- **隣メッシュの店舗には救われない**＝1店の欠落が1メッシュ全員を圏外に振り替える。
  だから**店舗レイヤの網羅性が距離指標に直に効く**。

## 現状（実測値・3県88市区町村・65歳以上 1,107,739人）

| 人口レイヤ | 店舗レイヤ | 圏外率A | 比 | r | 比>1 |
|---|---|---:|---:|---:|---:|
| 500mメッシュ | Phase1（102,984店） | 62.2% | 0.420 | 0.448 | 0件 |
| 500mメッシュ | ATP基準＋許可（139,492店） | 57.3% | 0.456 | 0.462 | 0件 |
| **125mメッシュ** | Phase1 | 62.4% | 0.416 | 0.451 | 0件 |
| **125mメッシュ** | ATP基準＋許可 | 57.4% | 0.452 | 0.454 | 0件 |

**変種Aは人口を125mにしてもほぼ動かない**。500m単位の判定は同じで、子16メッシュの和が親に一致するため。
変わるのは市区町村境での割当精度だけ（65歳以上人口 1,101,938 → 1,107,739 = +5,801人）。

**判定**: 相対比較・スクリーニングには耐える（比≤1 を全市区町村で維持）。
**絶対人数の推計には耐えない**（係数が自動車利用困難率と店舗の穴のどちらに由来するか分離できていない。
市区町村単位の店舗由来誤差は概ね ±20〜30%）。

### 次の一手（優先順）

1. **自動車利用困難率を外部データで固定して係数を分解する**（絶対推計への唯一の道）。
   候補: 全国家計構造調査の高齢者世帯 自動車保有率、農水省の推計手法論文。
2. **検証県を47都道府県に拡張**（現状 高知・島根・宮城の88市区町村）。
3. 店舗レイヤの穴埋めは japan-food-store-master 側の課題。効果は必ずここの検証器で測る。

## 再現の要点

```bash
python scripts/01_fetch_mesh_population.py 高知県 島根県 宮城県
FOOD_STORES=input/food_store_master_atp_permit.parquet OUT_SUFFIX=_ATP許可基準 \
    python scripts/02_validate_access_difficulty.py 高知県 島根県 宮城県
```

- 店舗レイヤに必要な列は **`lat` / `lng` / `cat`** の3つだけ。
  `cat` は `supermarket` / `convenience` / `drugstore` / `fresh_food`。
- `FOOD_STORES` / `OUT_SUFFIX` で入出力を差し替えられる。**既存 CSV を実験で上書きしないこと**。
- 外部データは全部スクリプトが自動取得・キャッシュ（`data/mesh/` `data/boundary/`
  `data/maff_2020_table05.xlsx`、いずれも .gitignore）。

## 落とし穴・環境メモ

- **e-Stat メッシュ統計の列番号**: `…019` が65歳以上、`…022` は**75歳以上**。取り違えると分母が半分になる
  （仙台市 65+ = 255,567 が 126,553 になる）。**125m（T001225）と 500m（T001141）で並びは同一**。
- **e-Stat 統計GIS は appId 不要**でダウンロードできる。125mメッシュ人口は
  `statmap-search/data?statsId=T001225&code=<1次メッシュ>&downloadType=2`。境界も同様。
  e-Stat **API** の appId はユーザー保有で、リポジトリには無い。
- **125mメッシュは秘匿が桁違いに多い**。全国 2,820,831 メッシュ中 **1,017,247 メッシュ**が
  65歳以上 `*`（秘匿）。`HTKSYORI=2` の行は `HTKSAKI` メッシュに合算済みなので `*` は0扱いで
  総計は保たれるが、人口が合算先メッシュの位置に寄る。県計・市区町村計の目減りは必ず確認すること。
- **DuckDB の `/` は DOUBLE を返し `::int` が四捨五入する**。メッシュ添字は必ず `//`（整数除算）を使う
  （`(m-1)/2` で m=2 が 0.5→1 に丸められ別メッシュと衝突した）。
- **DuckDB spheroid バグ**: この環境では `ST_Distance_Spheroid` / `ST_DWithin_Spheroid` が `-nan` を返す。
  距離は等距円筒近似（緯度補正した平面距離）で代替する。
- **`ST_Read` の戻り型は `GEOMETRY('EPSG:4612')`** で、そのままでは rtree インデックスが作れない。
  `geom::GEOMETRY` で素の型に落とす。
- **e-Stat 境界 shapefile の DBF は CP932**。`CITY_NAME`/`PREF_NAME` を読むと DuckDB が unicode エラーを
  出す。ASCII のコード列（`PREF`,`CITY`）のみ読み、名称は農水省の表5から引く。
- **政令指定都市**: 農水省 表5 は市単位（仙台市=04100）、境界データは区単位（04101…）。
  3桁目が `1` のコードを `XX100` に寄せる。
- **11桁L6コードの先頭9桁がそのまま500mメッシュコード**になる。変種Aの判定キーはこれ。
- Windows の Git Bash でコンソールが文字化けするときは `PYTHONIOENCODING=utf-8` を付ける
  （ファイル出力は影響を受けない）。
