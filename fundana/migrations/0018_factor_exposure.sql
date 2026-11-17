-- ADR-078：风格因子归因（久期/信用/转债 beta）。
-- 指数日线来自东财 kline（国债指数/企债指数/中证转债），只存收盘价；
-- 因子暴露按基金最近一年日收益对三因子做 OLS，结果按 as_of 留存，可回看历史。
CREATE TABLE IF NOT EXISTS market_index_daily(
  index_code  TEXT NOT NULL,
  trade_date  TEXT NOT NULL,
  close       REAL NOT NULL,
  pct_chg     REAL,
  source_id   TEXT NOT NULL,
  raw_sha256  TEXT,
  observed_at TEXT NOT NULL,
  PRIMARY KEY(index_code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_market_index_date ON market_index_daily(trade_date);

CREATE TABLE IF NOT EXISTS fund_factor_exposure(
  as_of           TEXT NOT NULL,
  fcode           TEXT NOT NULL,
  beta_duration   REAL,
  beta_credit     REAL,
  beta_convertible REAL,
  alpha           REAL,
  r2              REAL,
  n_obs           INTEGER NOT NULL,
  start_date      TEXT,
  end_date        TEXT,
  method          TEXT NOT NULL,
  computed_at     TEXT NOT NULL,
  PRIMARY KEY(as_of, fcode)
);
CREATE INDEX IF NOT EXISTS idx_factor_fcode ON fund_factor_exposure(fcode, as_of DESC);
