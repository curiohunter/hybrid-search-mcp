# Confidence calibration report

- rows: 25 (25 answerable)
- ECE: 0.154  |  Brier: 0.2353
- strong coverage (answerable): 0.04

| label | n | precision | nominal |
|---|---:|---:|---:|
| strong | 1 | 1.000 | 0.95 |
| mixed | 21 | 0.714 | 0.6 |
| weak | 3 | 0.667 | 0.2 |

| answer at | coverage | risk |
|---|---:|---:|
| ≥strong | 0.040 | 0.000 |
| ≥mixed | 0.880 | 0.273 |
| ≥weak | 1.000 | 0.280 |

## Gates

- strong precision ≥ 0.95: PASS
- strong coverage ≥ 0.2: FAIL
- **'calibrated' claim allowed: NO**

## Slices

| slice | n | strong precision | strong coverage | ece |
|---|---:|---:|---:|---:|
| corpus=valuein | 25 | 1.000 | 0.04 | 0.154 |
| language=ko | 25 | 1.000 | 0.04 | 0.154 |
| corpus=valuein|language=ko | 25 | 1.000 | 0.04 | 0.154 |
