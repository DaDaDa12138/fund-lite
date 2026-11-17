-- 0006: 交易费用阶梯（A4 jjfl 费率页）。只存事实档位，不在采集层做任何费后收益推断。
-- rate_pct 单位为百分点（0.80 = 0.80%）；固定费单独存 fixed_cny，禁止当成 0%。
CREATE TABLE IF NOT EXISTS fund_fee_tier(
  fcode          TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK(kind IN ('purchase','subscription','redemption')),
  tier_no        INTEGER NOT NULL,
  amount_lo_cny  REAL,
  amount_hi_cny  REAL,
  days_lo        INTEGER,
  days_hi        INTEGER,
  rate_pct       REAL,
  discount_pct   REAL,
  fixed_cny      REAL,
  observed_at    TEXT,
  raw_sha256     TEXT,
  PRIMARY KEY(fcode, kind, tier_no)
);
CREATE INDEX IF NOT EXISTS idx_fee_tier_fcode ON fund_fee_tier(fcode);

CREATE TABLE IF NOT EXISTS fund_fee_meta(
  fcode          TEXT PRIMARY KEY,
  mgr_fee_pct    REAL,
  trust_fee_pct  REAL,
  sales_fee_pct  REAL,
  has_discount   INTEGER NOT NULL DEFAULT 0,
  source         TEXT,
  observed_at    TEXT,
  raw_sha256     TEXT
);
