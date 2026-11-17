-- P2 数据健康与榜单查询的辅助索引（只加索引，不改数据）
CREATE INDEX IF NOT EXISTS idx_fetch_log_started
    ON fetch_log(started_at);
CREATE INDEX IF NOT EXISTS idx_fund_metric_contract_period
    ON fund_metric(formula_contract, period_days);
