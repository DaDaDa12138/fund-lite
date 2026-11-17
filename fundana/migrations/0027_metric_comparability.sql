-- 0027 P0-2：指标可比性标记
-- 目的：把“净值曲线不可比”的基金显式标记出来，不让摊余成本定开债、
-- 零风险/零回撤、稀疏净值、过期净值污染分位分布或伪装成低风险优质基金。
CREATE TABLE IF NOT EXISTS fund_metric_comparability(
  fcode             TEXT NOT NULL,
  period_days       INTEGER NOT NULL,
  formula_contract  TEXT NOT NULL,
  as_of             TEXT NOT NULL,
  flags_json        TEXT NOT NULL,
  is_comparable     INTEGER NOT NULL,
  computed_at       TEXT NOT NULL,
  PRIMARY KEY(fcode, period_days, formula_contract, as_of)
);
CREATE INDEX IF NOT EXISTS idx_metric_comparability_asof
  ON fund_metric_comparability(as_of, period_days, is_comparable);
