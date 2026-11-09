# G-CHAM SVI and LISA

ポリゴンデータから社会的脆弱性指標（SVI）を算出し、局所 Moran's I（LISA）による空間クラスター分析を行うQGISプラグインです。

## 主な機能

- 指標の元データ、または分子・分母から算出した割合を使用
- 指標ごとの順位方向の反転
- 平均順位法による0〜1のパーセンタイル化とSVIの合算
- k近傍法、行標準化、置換検定、Benjamini–Hochberg法によるFDR補正を用いたLISA
- SVIとLISA結果のGeoPackage保存
- SVI記述統計とLISAカテゴリ別該当数のCSV出力

## リポジトリ構成

- `svi_lisa_qgis/`: QGISにインストールするプラグイン本体
- リリース用ZIPはソースリポジトリに含めず、QGISプラグイン公式サイトに別途登録します。

## 制約

- SVIは選択した全指標に有効値がある地物のみ計算します。欠損値の補完は行いません。
- LISAでは数値欠損または空ジオメトリを分析対象から除外します。
- 空間重みは投影座標系上の代表点によるk近傍法です。
- LISAの有効地物数は20,000件までです。
- 結果の研究利用・公表前に、対象データに応じた妥当性確認を行ってください。

## インストール

QGISの「プラグイン」→「プラグインの管理とインストール」→「ZIPからインストール」から、公式リリースZIPを選択します。

## ライセンス

MIT License。詳細は [`svi_lisa_qgis/LICENSE`](svi_lisa_qgis/LICENSE) を参照してください。

## 引用

研究等で本プラグインを使用した場合は、利用したバージョンを明記して引用してください。以下はバージョン0.1.5の例です。各引用文をコピーして使用できます。

APA第7版：

> GISPHN (Ryo Horiike). (2026). *G-CHAM SVI and LISA* (Version 0.1.5) [Computer software]. GitHub.  
> https://github.com/GISPHN/G-CHAM_SVI_LISA

Vancouver形式：

> GISPHN (Ryo Horiike). G-CHAM SVI and LISA [computer software]. Version 0.1.5. GitHub; 2026 [cited 2026 Nov 10]. Available from: https://github.com/GISPHN/G-CHAM_SVI_LISA

BibTeX：

```bibtex
@software{Horiike2026GCHAMSVILISA,
  author = {{GISPHN (Ryo Horiike)}},
  title = {G-CHAM SVI and LISA},
  version = {0.1.5},
  year = {2026},
  url = {https://github.com/GISPHN/G-CHAM_SVI_LISA},
  note = {QGIS plugin}
}
```

GitHubの引用機能向けメタデータは [`CITATION.cff`](CITATION.cff) に掲載しています。LISAの分析方法を説明する場合は、方法の原著論文も引用してください。原著論文の詳細と追加の案内は [`CITATION.md`](CITATION.md) を参照してください。
