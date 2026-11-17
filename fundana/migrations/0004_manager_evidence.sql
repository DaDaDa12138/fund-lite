-- 0004: 经理维度（东财 pingzhongdata 现任经理，职业年限与东财综合评分）
-- 注意：manager_power_score 是东财 power.avr（0–100），不是 MGOLD 10 分制，禁止互相映射。
ALTER TABLE fund_profile ADD COLUMN manager_count INTEGER;
ALTER TABLE fund_profile ADD COLUMN manager_career_min_days INTEGER;
ALTER TABLE fund_profile ADD COLUMN manager_career_max_days INTEGER;
ALTER TABLE fund_profile ADD COLUMN manager_power_score REAL;

CREATE TABLE IF NOT EXISTS fund_manager_evidence(
  fcode             TEXT NOT NULL,
  mgr_id            TEXT NOT NULL,
  mgr_name          TEXT,
  is_lead           INTEGER NOT NULL DEFAULT 0,
  career_days       INTEGER,
  tenure_return_pct REAL,
  power_avr         REAL,
  star              INTEGER,
  fund_size_text    TEXT,
  observed_at       TEXT,
  raw_sha256        TEXT,
  PRIMARY KEY(fcode, mgr_id)
);
CREATE INDEX IF NOT EXISTS idx_mgr_evidence_fcode ON fund_manager_evidence(fcode);
