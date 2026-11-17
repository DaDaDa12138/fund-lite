-- 0025: v9 同池影子排序的前瞻观察
-- 与正式 forward_* 隔离：影子结果不是发布榜，不能混入正式证据等级。
CREATE TABLE IF NOT EXISTS shadow_cohort(
  cohort_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  publication_id TEXT,
  business_date TEXT NOT NULL,
  strategy_version TEXT NOT NULL,
  formula_contract TEXT,
  screen_contract TEXT,
  freeze_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_member(
  cohort_id TEXT NOT NULL,
  board TEXT NOT NULL,              -- core10|core50
  strategy TEXT NOT NULL,           -- v10|v9
  fcode TEXT NOT NULL,
  rank INTEGER,
  style_code TEXT,
  rank_score REAL,
  PRIMARY KEY (cohort_id, board, strategy, fcode)
);
CREATE INDEX IF NOT EXISTS idx_shadow_member_board ON shadow_member(cohort_id, board, strategy);

CREATE TABLE IF NOT EXISTS shadow_observation(
  cohort_id TEXT NOT NULL,
  fcode TEXT NOT NULL,
  horizon_days INTEGER NOT NULL,
  status TEXT NOT NULL,             -- matured|not_ready|unverifiable
  start_date TEXT,
  end_date TEXT,
  n_obs INTEGER,
  max_gap_days INTEGER,
  start_lag_days INTEGER,
  total_return_pct REAL,
  annual_dec REAL,
  mdd_dec REAL,
  reason_code TEXT,
  computed_at TEXT NOT NULL,
  PRIMARY KEY (cohort_id, fcode, horizon_days)
);
CREATE INDEX IF NOT EXISTS idx_shadow_obs_status ON shadow_observation(cohort_id, horizon_days, status);
