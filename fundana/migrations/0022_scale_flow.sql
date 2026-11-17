-- 0022: 规模变动表补期间申购/赎回/净流入（ADR-083）。
-- gmbd 原始表本来就返回这两个字段，之前只落了期末净资产/份额，资金流信息被丢掉。
ALTER TABLE fund_scale_history ADD COLUMN period_buy_billion REAL;
ALTER TABLE fund_scale_history ADD COLUMN period_redeem_billion REAL;
ALTER TABLE fund_scale_history ADD COLUMN net_flow_billion REAL;
ALTER TABLE fund_scale_history ADD COLUMN flow_unit TEXT;
