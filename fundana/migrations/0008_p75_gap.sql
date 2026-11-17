-- ADR-027：采样节奏改用观测间隔 p75（混合披露的中位数会落在 1 天）
ALTER TABLE fund_metric ADD COLUMN p75_gap_days REAL;
