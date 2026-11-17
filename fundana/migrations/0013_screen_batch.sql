-- 阈值冻结与批次（ADR-050）：同一 batch 内分位阈值只算一次，跨日重跑复用，
-- 保证"同一份输入 → 同一份榜单"，也保证"阈值变了"与"数据变了"可区分。
CREATE TABLE IF NOT EXISTS screen_batch(
  batch_id TEXT PRIMARY KEY,
  profile TEXT NOT NULL,
  anchor_business_date TEXT NOT NULL,
  config_hash TEXT NOT NULL,
  formula_contract TEXT NOT NULL,
  frozen_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'frozen',
  note TEXT
);

-- 每条分位线都留 enter（新进入）与 exit（已上榜者保留）两档，供双侧带滞回使用。
CREATE TABLE IF NOT EXISTS screen_threshold(
  batch_id TEXT NOT NULL,
  scope TEXT NOT NULL,            -- 'global' 或风格码
  metric TEXT NOT NULL,
  enter_value REAL,
  exit_value REAL,
  n INTEGER,
  quantile REAL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(batch_id, scope, metric)
);

ALTER TABLE screen_run ADD COLUMN batch_id TEXT;
ALTER TABLE screen_run ADD COLUMN board_diff_path TEXT;
