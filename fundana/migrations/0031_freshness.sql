-- 2026-11-03 数据新鲜度事故：screen_run/publication 必须记录“有效净值日”
-- 和“指标截止日”，不能再用 MAX(nav_date) 代表市场数据日。
ALTER TABLE screen_run ADD COLUMN nav_as_of TEXT;
ALTER TABLE screen_run ADD COLUMN metric_as_of TEXT;
ALTER TABLE screen_run ADD COLUMN freshness_json TEXT;
ALTER TABLE publication ADD COLUMN nav_as_of TEXT;
ALTER TABLE publication ADD COLUMN metric_as_of TEXT;
