-- 0020: 长期质量指标（MRAR(2) 三年评级，ADR-083）。
-- 只存可由月度净值复算的原始结果；质量标签/风险提示在读取时按当前快照组合，
-- 避免把会随持仓、规模、经理变化而变化的标签冻结成历史事实。
CREATE TABLE IF NOT EXISTS fund_quality_metric(
  as_of         TEXT NOT NULL,
  fcode         TEXT NOT NULL,
  method        TEXT NOT NULL,
  window_months INTEGER NOT NULL,
  mrar_pct      REAL,
  n_obs         INTEGER NOT NULL,
  start_date    TEXT,
  end_date      TEXT,
  rf_annual     REAL,
  peer_n        INTEGER,
  peer_pct      REAL,
  computed_at   TEXT NOT NULL,
  PRIMARY KEY(as_of, fcode)
);
CREATE INDEX IF NOT EXISTS idx_quality_fcode ON fund_quality_metric(fcode, as_of DESC);
