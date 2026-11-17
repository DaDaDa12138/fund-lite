-- P0-3：独立风险门（持仓集中度 + acc_nav 压力测试）结果。
-- 只增加一列，不修改已有分数；blocked 的基金会以 layer_reached='L8R' 留在
-- screen_decision 里，便于复盘“为什么没进榜”。
ALTER TABLE screen_decision ADD COLUMN risk_json TEXT;
