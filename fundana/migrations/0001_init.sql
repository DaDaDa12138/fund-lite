-- Fund-ANA V2 首期 schema：三层（L0 raw / L1 src / L2 canonical / L3 决策与发布）
-- 单位纪律：*_pct=百分点，*_dec=小数比率，*_billion=亿元，*_obs/*_days/*_count=计数
-- 缺失纪律：attempted(0/1) + status(not_requested|provider_null|parse_failed|stale|real_zero|ok)，禁止把缺失写成 0

CREATE TABLE IF NOT EXISTS schema_migration(
  version     TEXT PRIMARY KEY,
  applied_at  TEXT NOT NULL,
  sha256      TEXT NOT NULL
);

-- ── L0 原始层 ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS run(
  run_id       TEXT PRIMARY KEY,
  mode         TEXT NOT NULL,          -- initial|daily|research|shadow|probe
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  status       TEXT NOT NULL,          -- running|success|failed|partial
  config_hash  TEXT NOT NULL,
  code_version TEXT NOT NULL,
  notes        TEXT
);

CREATE TABLE IF NOT EXISTS raw_object(
  sha256       TEXT PRIMARY KEY,
  bytes_gz     BLOB NOT NULL,
  raw_size     INTEGER NOT NULL,
  content_type TEXT,
  encoding     TEXT,
  url          TEXT NOT NULL,
  http_status  INTEGER,
  fetched_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fetch_log(
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id       TEXT NOT NULL,
  source_id    TEXT NOT NULL,
  key          TEXT,
  url          TEXT NOT NULL,
  started_at   TEXT NOT NULL,
  duration_ms  INTEGER NOT NULL,
  http_status  INTEGER,
  business_ok  INTEGER NOT NULL,       -- 0/1：HTTP 200 但业务失败必须为 0
  status       TEXT NOT NULL,          -- ok|provider_null|parse_failed|http_error|timeout|soft_fail|skipped_fresh
  error_msg    TEXT,
  raw_sha256   TEXT,
  attempt      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_source ON fetch_log(source_id, started_at);
CREATE INDEX IF NOT EXISTS idx_fetch_log_run ON fetch_log(run_id);

CREATE TABLE IF NOT EXISTS source_state(
  source_id             TEXT NOT NULL,
  key                   TEXT NOT NULL,
  last_attempt_at       TEXT,
  last_success_at       TEXT,
  last_status           TEXT,
  consecutive_failures  INTEGER NOT NULL DEFAULT 0,
  cooldown_until        TEXT,
  last_raw_sha256       TEXT,
  last_error            TEXT,
  PRIMARY KEY(source_id, key)
);

-- ── L1 源事实层（逐源原样解析，不做裁决）─────────────────────
CREATE TABLE IF NOT EXISTS src_directory(
  business_date TEXT NOT NULL,
  observed_at   TEXT NOT NULL,
  code          TEXT NOT NULL,
  name          TEXT,
  pinyin_abbr   TEXT,
  type_text     TEXT,                 -- 东财二级类型，如 债券型-混合二级
  pinyin_full   TEXT,
  raw_sha256    TEXT NOT NULL,
  PRIMARY KEY(business_date, code)
);

CREATE TABLE IF NOT EXISTS src_snapshot_status(
  business_date     TEXT NOT NULL,
  observed_at       TEXT NOT NULL,
  code              TEXT NOT NULL,
  name              TEXT,
  type_text         TEXT,
  unit_nav          TEXT,             -- 原样文本，解析失败保留 NULL
  nav_date_md       TEXT,             -- MM-DD（无年份）
  purchase_status   TEXT,
  redemption_status TEXT,
  next_open_date    TEXT,
  min_purchase      TEXT,
  fee_pct           TEXT,
  flag_code         TEXT,
  raw_sha256        TEXT NOT NULL,
  PRIMARY KEY(business_date, code)
);

CREATE TABLE IF NOT EXISTS src_rank_row(
  business_date TEXT NOT NULL,
  observed_at   TEXT NOT NULL,
  code          TEXT NOT NULL,
  name          TEXT,
  pinyin        TEXT,
  nav_date      TEXT,
  unit_nav      TEXT,
  acc_nav       TEXT,
  day_pct       TEXT,
  w1_pct        TEXT,
  m1_pct        TEXT,
  m3_pct        TEXT,
  m6_pct        TEXT,
  y1_pct        TEXT,
  y2_pct        TEXT,
  y3_pct        TEXT,
  ytd_pct       TEXT,
  since_pct     TEXT,
  estab_date    TEXT,
  scale_yi      TEXT,
  fee_orig_pct  TEXT,
  fee_now_pct   TEXT,
  page          INTEGER,
  row_ord       INTEGER,
  raw_sha256    TEXT NOT NULL,
  PRIMARY KEY(business_date, code)
);

CREATE TABLE IF NOT EXISTS src_pzd_profile(
  code            TEXT NOT NULL,
  business_date   TEXT NOT NULL,
  observed_at     TEXT NOT NULL,
  nav_points      INTEGER,
  acc_points      INTEGER,
  syl_1n_pct      REAL,
  rate_pct        REAL,
  source_rate_pct REAL,
  minsg           TEXT,
  manager_json    TEXT,
  scale_json      TEXT,
  asset_alloc_json TEXT,
  holder_json     TEXT,
  performance_json TEXT,
  raw_sha256      TEXT NOT NULL,
  PRIMARY KEY(code, business_date)
);

CREATE TABLE IF NOT EXISTS src_jbgk_profile(
  code           TEXT NOT NULL,
  business_date  TEXT NOT NULL,
  observed_at    TEXT NOT NULL,
  full_name      TEXT,
  estab_date     TEXT,
  company_name   TEXT,
  custodian      TEXT,
  asset_scale    TEXT,
  share_scale    TEXT,
  mgr_fee_pct    REAL,
  trust_fee_pct  REAL,
  sales_fee_pct  REAL,
  benchmark_text TEXT,
  tracking_text  TEXT,
  raw_sha256     TEXT NOT NULL,
  PRIMARY KEY(code, business_date)
);

CREATE TABLE IF NOT EXISTS src_zqcc_holding(
  code            TEXT NOT NULL,
  report_period   TEXT NOT NULL,
  observed_at     TEXT NOT NULL,
  bond_code       TEXT NOT NULL,
  bond_name       TEXT,
  pct_nav         REAL,
  market_value_wan REAL,
  raw_sha256      TEXT NOT NULL,
  PRIMARY KEY(code, report_period, bond_code)
);

CREATE TABLE IF NOT EXISTS src_manager_history(
  code        TEXT NOT NULL,
  mgr_id      TEXT NOT NULL,
  start_date  TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  mgr_name    TEXT,
  end_date    TEXT,                   -- NULL = 在任
  days        INTEGER,
  raw_sha256  TEXT NOT NULL,
  PRIMARY KEY(code, mgr_id, start_date)
);

-- ── L2 规范化层（唯一可被指标与筛选消费的层）─────────────────
CREATE TABLE IF NOT EXISTS fund(
  fcode           TEXT PRIMARY KEY,
  name            TEXT,
  type_text       TEXT,
  type_group      TEXT,               -- bond|money|equity|hybrid|index|fof|qdii|other
  estab_date      TEXT,
  company_name    TEXT,
  fund_status     TEXT,
  identity_status TEXT NOT NULL,      -- ok|quarantine|conflict
  quarantine_reason TEXT,
  first_seen_date TEXT,
  last_seen_date  TEXT,
  updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_fund_group ON fund(type_group, identity_status);

CREATE TABLE IF NOT EXISTS fund_share_class(
  fcode        TEXT PRIMARY KEY,
  family_id    TEXT,
  class_letter TEXT,
  is_primary   INTEGER
);

CREATE TABLE IF NOT EXISTS fund_family(
  family_id      TEXT PRIMARY KEY,
  canonical_name TEXT,
  company_name   TEXT,
  member_count   INTEGER,
  evidence_json  TEXT,
  created_at     TEXT
);

CREATE TABLE IF NOT EXISTS fund_profile(
  fcode                TEXT PRIMARY KEY,
  full_name            TEXT,
  asset_scale_billion  REAL,
  asset_scale_src      TEXT,
  asset_scale_obs      TEXT,
  share_scale_billion  REAL,
  share_scale_src      TEXT,
  mgr_fee_pct          REAL,
  trust_fee_pct        REAL,
  sales_fee_pct        REAL,
  recurring_fee_pct    REAL,          -- 三项持续计提费合计（百分点）
  max_sub_fee_pct      REAL,
  max_red_fee_pct      REAL,
  benchmark_text       TEXT,
  tracking_text        TEXT,
  field_src_json       TEXT,
  conflict_json        TEXT,
  updated_at           TEXT
);

CREATE TABLE IF NOT EXISTS fund_fee_schedule(
  fcode     TEXT NOT NULL,
  kind      TEXT NOT NULL,            -- sub|red
  tier_no   INTEGER NOT NULL,
  amount_lo REAL,
  amount_hi REAL,
  days_lo   INTEGER,
  days_hi   INTEGER,
  rate_pct  REAL,
  src       TEXT,
  PRIMARY KEY(fcode, kind, tier_no)
);

CREATE TABLE IF NOT EXISTS fund_holding_policy(
  fcode                TEXT PRIMARY KEY,
  structured_days      INTEGER,
  name_inferred_days   INTEGER,
  effective_days       INTEGER,
  lockup_type          TEXT,          -- none|holding|periodic_open|closed
  evidence_json        TEXT,
  updated_at           TEXT
);

CREATE TABLE IF NOT EXISTS fund_style(
  fcode             TEXT PRIMARY KEY,
  style_code        TEXT NOT NULL,    -- bond_secondary_enhanced|bond_primary_enhanced|bond_convertible_enhanced|bond_short|bond_index|bond_rate|bond_credit|bond_cash_enhanced|bond_pure
  confidence        TEXT NOT NULL,    -- structured_confirmed|multi_field_consistent|text_heuristic|conflict|unknown
  evidence_json     TEXT,
  classifier_version TEXT,
  updated_at        TEXT
);

CREATE TABLE IF NOT EXISTS fund_buyable_daily(
  fcode              TEXT NOT NULL,
  business_date      TEXT NOT NULL,
  purchase_status    TEXT,
  redemption_status  TEXT,
  buyable_state      TEXT NOT NULL,   -- confirmed_yes|confirmed_no|unknown|conflicting
  next_open_date     TEXT,
  min_purchase       TEXT,
  fee_pct            TEXT,
  raw_sha256         TEXT,
  PRIMARY KEY(fcode, business_date)
);

CREATE TABLE IF NOT EXISTS fund_nav_daily(
  fcode        TEXT NOT NULL,
  nav_date     TEXT NOT NULL,
  unit_nav     REAL,
  acc_nav      REAL,
  growth_pct   REAL,                  -- JZZZL 百分点，链式复利唯一依据
  nav_source   TEXT NOT NULL,         -- em_pzd|dj_nav|em_lsjz
  raw_sha256   TEXT,
  revision_no  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(fcode, nav_date)
);
CREATE INDEX IF NOT EXISTS idx_nav_date ON fund_nav_daily(nav_date);

CREATE TABLE IF NOT EXISTS fund_nav_revision(
  fcode       TEXT NOT NULL,
  nav_date    TEXT NOT NULL,
  detected_at TEXT NOT NULL,
  old_unit    REAL,
  new_unit    REAL,
  old_growth  REAL,
  new_growth  REAL,
  source      TEXT,
  PRIMARY KEY(fcode, nav_date, detected_at)
);

CREATE TABLE IF NOT EXISTS fund_scale_history(
  fcode               TEXT NOT NULL,
  report_period       TEXT NOT NULL,
  asset_scale_billion REAL,
  share_scale_billion REAL,
  src                 TEXT,
  PRIMARY KEY(fcode, report_period)
);

CREATE TABLE IF NOT EXISTS fund_holding_bond(
  fcode            TEXT NOT NULL,
  report_period    TEXT NOT NULL,
  bond_code        TEXT NOT NULL,
  bond_name        TEXT,
  pct_nav          REAL,
  market_value_wan REAL,
  bond_class       TEXT,              -- convertible|rate|credit|other
  raw_sha256       TEXT,
  PRIMARY KEY(fcode, report_period, bond_code)
);

CREATE TABLE IF NOT EXISTS fund_manager_tenure(
  fcode      TEXT NOT NULL,
  mgr_id     TEXT NOT NULL,
  start_date TEXT NOT NULL,
  mgr_name   TEXT,
  end_date   TEXT,
  days       INTEGER,
  is_current INTEGER,
  src        TEXT,
  PRIMARY KEY(fcode, mgr_id, start_date)
);

CREATE TABLE IF NOT EXISTS fund_coverage(
  fcode          TEXT NOT NULL,
  run_id         TEXT NOT NULL,
  nav_first_date TEXT,
  nav_last_date  TEXT,
  nav_points     INTEGER,
  gap_max_days   INTEGER,
  freshness_days INTEGER,
  flags_json     TEXT,
  PRIMARY KEY(fcode, run_id)
);

-- ── L3 指标 / 决策 / 发布 ────────────────────────────────────
CREATE TABLE IF NOT EXISTS fund_metric(
  fcode              TEXT NOT NULL,
  period_days        INTEGER NOT NULL,
  formula_contract   TEXT NOT NULL,
  nav_sig            TEXT,
  start_date         TEXT,
  end_date           TEXT,
  n_obs              INTEGER,
  years              REAL,
  total_return_pct   REAL,
  annual_dec         REAL,
  vol_dec            REAL,
  positive_ratio_pct REAL,
  mdd_dec            REAL,
  dd_duration_obs    INTEGER,
  sharpe             REAL,
  sharpe_t           REAL,
  sortino            REAL,
  calmar_dec         REAL,
  var95_dec          REAL,
  cvar5_dec          REAL,
  raw_sharpe         REAL,
  raw_sortino        REAL,
  raw_calmar         REAL,
  sharpe_cap_hit     INTEGER,
  sortino_cap_hit    INTEGER,
  calmar_cap_hit     INTEGER,
  undefined_reasons_json TEXT,
  computed_at        TEXT,
  PRIMARY KEY(fcode, period_days, formula_contract)
);

CREATE TABLE IF NOT EXISTS screen_run(
  run_id           TEXT PRIMARY KEY,
  profile          TEXT NOT NULL,
  config_hash      TEXT NOT NULL,
  code_version     TEXT NOT NULL,
  formula_contract TEXT NOT NULL,
  universe_count   INTEGER,
  base_pass_count  INTEGER,
  formal_count     INTEGER,
  locked_count     INTEGER,
  status           TEXT,
  created_at       TEXT
);

CREATE TABLE IF NOT EXISTS screen_decision(
  run_id           TEXT NOT NULL,
  fcode            TEXT NOT NULL,
  layer_reached    TEXT NOT NULL,
  passed           INTEGER NOT NULL,
  reason_codes_json TEXT NOT NULL,
  style_code       TEXT,
  thresholds_json  TEXT,
  scores_json      TEXT,
  confidence       REAL,
  PRIMARY KEY(run_id, fcode)
);

CREATE TABLE IF NOT EXISTS screen_pick(
  run_id         TEXT NOT NULL,
  board          TEXT NOT NULL,       -- bond_stable|bond_enhanced|bond_locked
  rank           INTEGER NOT NULL,
  fcode          TEXT NOT NULL,
  formal_score   REAL,
  stability_rank REAL,
  confidence     REAL,
  PRIMARY KEY(run_id, board, fcode)
);

CREATE TABLE IF NOT EXISTS publication(
  publication_id TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL,
  generation     INTEGER NOT NULL,
  created_at     TEXT NOT NULL,
  revision_path  TEXT NOT NULL,
  manifest_sha256 TEXT,
  status         TEXT NOT NULL
);
