-- 0011: Batch A —— 跨源校验 / 发布门禁 / 前瞻 cohort / 研究优先池
CREATE TABLE IF NOT EXISTS nav_cross_check(
  check_id TEXT PRIMARY KEY,
  business_date TEXT NOT NULL,
  fcode TEXT NOT NULL,
  source_a TEXT,
  source_b TEXT,
  field TEXT NOT NULL,
  value_a REAL,
  value_b REAL,
  delta_abs REAL,
  delta_rel REAL,
  tolerance TEXT,
  status TEXT NOT NULL,          -- pass|mismatch|soft_lag|missing_a|missing_b
  detail_json TEXT,
  checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cross_date ON nav_cross_check(business_date, status);
CREATE INDEX IF NOT EXISTS idx_cross_fcode ON nav_cross_check(fcode, business_date);

CREATE TABLE IF NOT EXISTS publication_gate(
  publication_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  status TEXT NOT NULL,          -- pass|warning|blocked
  blocks_json TEXT,
  warnings_json TEXT,
  checks_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forward_cohort(
  cohort_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  publication_id TEXT,
  business_date TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'live',   -- live|retro
  strategy_version TEXT,
  formula_contract TEXT,
  universe_count INTEGER,
  investable_count INTEGER,
  freeze_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forward_member(
  cohort_id TEXT NOT NULL,
  fcode TEXT NOT NULL,
  role TEXT NOT NULL,            -- formal|control_style|control_risk
  matched_to TEXT NOT NULL DEFAULT '',
  style_code TEXT,
  formal_score REAL,
  PRIMARY KEY (cohort_id, fcode, role, matched_to)
);
CREATE INDEX IF NOT EXISTS idx_fmember_role ON forward_member(cohort_id, role);

CREATE TABLE IF NOT EXISTS forward_observation(
  cohort_id TEXT NOT NULL,
  fcode TEXT NOT NULL,
  horizon_days INTEGER NOT NULL,
  status TEXT NOT NULL,          -- matured|not_ready|unverifiable
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

CREATE TABLE IF NOT EXISTS screen_research_pool(
  run_id TEXT NOT NULL,
  fcode TEXT NOT NULL,
  bucket TEXT,
  score REAL,
  rank INTEGER,
  reason TEXT,
  PRIMARY KEY (run_id, fcode)
);

-- 筛选轮次必须记录业务日：前瞻 cohort 与 tracker 都以"业务日"而非"运行时刻"对齐
ALTER TABLE screen_run ADD COLUMN business_date TEXT;
