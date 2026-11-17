-- ADR-026：记录窗口采样节奏与本次实际使用的最少点数，便于审计"为什么这只基金有/没有指标"。
ALTER TABLE fund_metric ADD COLUMN median_gap_days REAL;
ALTER TABLE fund_metric ADD COLUMN min_points_used INTEGER;
