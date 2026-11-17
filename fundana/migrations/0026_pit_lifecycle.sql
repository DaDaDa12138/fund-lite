-- 0026 P0-1：筛选输入的 point-in-time 快照 + 基金生命周期
-- 目标：同一 run 的输入可逐字段重放；历史回放不读未来数据；清盘样本可进入回测宇宙。
CREATE TABLE IF NOT EXISTS screen_run_input(
  run_id                 TEXT NOT NULL,
  fcode                  TEXT NOT NULL,
  as_of                  TEXT NOT NULL,
  layer_reached          TEXT,
  passed                 INTEGER,
  reason_codes_json      TEXT,
  style_code             TEXT,
  style_source           TEXT,
  identity_status        TEXT,
  estab_date             TEXT,
  company_name           TEXT,
  family_id              TEXT,
  asset_scale_billion    REAL,
  share_scale_billion    REAL,
  scale_source           TEXT,
  recurring_fee_pct      REAL,
  max_sub_fee_pct        REAL,
  max_red_fee_pct        REAL,
  has_discount           INTEGER,
  fee_source             TEXT,
  manager_tenure_min_days INTEGER,
  tenure_source          TEXT,
  effective_days         INTEGER,
  lockup_type            TEXT,
  buyable_state          TEXT,
  purchase_status        TEXT,
  nav_points             INTEGER,
  nav_first_date         TEXT,
  nav_last_date          TEXT,
  freshness_days         INTEGER,
  gap_max_days           INTEGER,
  metric_json            TEXT,
  percentile_pool        TEXT,
  pcts_json              TEXT,
  scores_json            TEXT,
  coverage_json          TEXT,
  created_at             TEXT NOT NULL,
  PRIMARY KEY(run_id, fcode)
);
CREATE INDEX IF NOT EXISTS idx_screen_run_input_fcode ON screen_run_input(fcode);

CREATE TABLE IF NOT EXISTS fund_lifecycle(
  fcode          TEXT NOT NULL,
  status         TEXT NOT NULL,          -- liquidated|merged|suspended|active
  effective_date TEXT NOT NULL,          -- 状态生效日（清盘/合并日）
  last_nav_date  TEXT,
  reason         TEXT,
  source         TEXT,
  raw_json       TEXT,
  created_at     TEXT NOT NULL,
  PRIMARY KEY(fcode, status, effective_date)
);
CREATE INDEX IF NOT EXISTS idx_fund_lifecycle_date ON fund_lifecycle(effective_date, status);
