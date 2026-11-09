# G-CHAM SVI and LISA

A QGIS plugin for calculating a Social Vulnerability Index (SVI) and performing local Moran's I (LISA) analysis on polygon layers.

## Installation

Install the ZIP package from **Plugins → Manage and Install Plugins → Install from ZIP**. Select the release ZIP without extracting it.

## Features

- Select numeric source fields or calculate percentages from numerator and denominator fields.
- Reverse the percentile direction for indicators where higher values represent lower vulnerability.
- Convert indicators to average-rank percentiles from 0 to 1, then calculate SVI sums, means, and final percentiles.
- Identify the indicator with the highest percentile for each feature. This is a descriptive indicator, not a causal contribution.
- Run local Moran's I with k-nearest neighbors, row-standardized weights, permutation tests, and optional Benjamini–Hochberg correction.
- Save results as separate SVI and LISA layers in a GeoPackage.
- Export descriptive statistics and LISA category counts to CSV files that can be opened in spreadsheet software.
- Display progress and processing details in a log window.

## Analysis notes and limitations

- SVI values are calculated only for features with valid values for every selected indicator. Missing values are not imputed.
- LISA excludes features with missing numeric values or empty geometries.
- The spatial weights use k-nearest representative points and planar distances. Use a projected coordinate reference system; geographic coordinate systems are rejected.
- The default permutation seed is fixed at `20261106` for reproducibility.
- The current implementation supports up to 20,000 valid LISA features. Runtime can increase substantially with dataset size and permutation count.
- The analysis covers the entire input layer. Stratified calculations, such as separate analyses by municipality, are not implemented.
- Review the results and neighborhood settings for the study data before research or publication use.

## Output fields

SVI outputs include `ind01_raw`, `ind01_pct` and corresponding fields for each indicator, plus `svi_sum`, `svi_mean`, `svi_pct`, `max_ind`, and `max_pct`.

LISA outputs include `local_I`, `p_perm`, `p_fdr`, and `lisa_cluster`. The category summary reports High-High, Low-Low, High-Low, Low-High, Not significant, and No data counts, along with analysis and total feature counts.

## License

MIT License. See [LICENSE](LICENSE).
