-- 0005: V2 尾部风险指标（架构书 §10.5）。首期只计算与展示，不参与 L1–L10 硬门槛。
-- 口径：同一冻结有效总回报日序列；历史不足一律 NULL，禁止用 1 年回撤代替 3 年回撤。
CREATE TABLE IF NOT EXISTS fund_tail_metric(
  fcode                  TEXT NOT NULL,
  window_days            INTEGER NOT NULL,
  formula_contract       TEXT NOT NULL,
  nav_sig                TEXT,
  start_date             TEXT,
  end_date               TEXT,
  n_obs                  INTEGER,
  mdd_dec                REAL,
  worst_rolling90d_pct   REAL,
  worst_rolling90d_start TEXT,
  worst_rolling90d_end   TEXT,
  worst_rolling180d_pct  REAL,
  worst_rolling180d_start TEXT,
  worst_rolling180d_end  TEXT,
  cvar5_dec              REAL,
  worst_day_pct          REAL,
  worst_day_date         TEXT,
  worst_week_pct         REAL,
  worst_week_end_date    TEXT,
  positive_month_ratio_pct REAL,
  months_total           INTEGER,
  months_positive        INTEGER,
  max_underwater_obs     INTEGER,
  ongoing_drawdown       INTEGER NOT NULL DEFAULT 0,
  completeness           REAL,
  undefined_reasons_json TEXT,
  computed_at            TEXT,
  PRIMARY KEY(fcode, window_days, formula_contract)
);
CREATE INDEX IF NOT EXISTS idx_tail_window ON fund_tail_metric(window_days, formula_contract);
