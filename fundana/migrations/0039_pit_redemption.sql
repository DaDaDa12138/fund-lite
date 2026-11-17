-- 0039 P1：把“持有满 1 年赎回费”纳入发布时输入快照。
-- 榜单 API 原先 JOIN fund_fee_meta 当前行；发布后费率更新会让历史榜单漂移。
-- 该字段只用于展示/审计，不进入筛选门槛（见 fees/holding.py 注释）。
ALTER TABLE screen_run_input ADD COLUMN redemption_1y_pct REAL;
