-- P2-3：季度指标快照，保存每个 as_of 真正 PIT 的 1/3/5 年指标。
-- 与 fund_metric（只存最新一期）分开，避免回填历史时覆盖生产指标。
CREATE TABLE IF NOT EXISTS fund_metric_snapshot (
    fcode TEXT NOT NULL,
    as_of TEXT NOT NULL,
    period_days INTEGER NOT NULL,
    formula_contract TEXT NOT NULL,
    start_date TEXT,
    end_date TEXT,
    n_obs INTEGER,
    years REAL,
    annual_dec REAL,
    vol_dec REAL,
    mdd_dec REAL,
    sharpe REAL,
    sortino REAL,
    calmar_dec REAL,
    positive_ratio_pct REAL,
    non_negative_ratio_pct REAL,
    flat_ratio_pct REAL,
    coverage_ratio REAL,
    nav_sig TEXT,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (fcode, as_of, period_days, formula_contract)
);
CREATE INDEX IF NOT EXISTS idx_metric_snapshot_asof
    ON fund_metric_snapshot(as_of, period_days, formula_contract);
