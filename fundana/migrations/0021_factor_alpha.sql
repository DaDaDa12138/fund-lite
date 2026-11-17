-- 0021: 因子回归的原始收益 alpha + Newey-West HAC t 值（ADR-083）。
-- 既有 alpha 是“基金与因子都标准化”后的日截距，量纲不可解释；这里保留标准化 beta
-- 供页面展示，同时增加原始收益 OLS 的年化 alpha、HAC t 值与原始 beta。
ALTER TABLE fund_factor_exposure ADD COLUMN alpha_annual_pct REAL;
ALTER TABLE fund_factor_exposure ADD COLUMN alpha_t REAL;
ALTER TABLE fund_factor_exposure ADD COLUMN alpha_reliable INTEGER;
ALTER TABLE fund_factor_exposure ADD COLUMN beta_duration_raw REAL;
ALTER TABLE fund_factor_exposure ADD COLUMN beta_credit_raw REAL;
ALTER TABLE fund_factor_exposure ADD COLUMN beta_convertible_raw REAL;
ALTER TABLE fund_factor_exposure ADD COLUMN alpha_method TEXT;
