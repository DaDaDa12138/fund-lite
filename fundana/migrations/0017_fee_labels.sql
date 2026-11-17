-- 0017: 费用阶梯可审计字段 + 赎回费快照
-- 背景：jjfl 的赎回档位大量使用“不小于7天/小于30天/T<7日”等写法，旧解析只存 days_lo/days_hi，
-- 无法回查原始文案；同时持有期总成本此前假设“满 1 年免赎回费”，需要把 1/3 年档位落成快照。
ALTER TABLE fund_fee_tier ADD COLUMN raw_label TEXT;
ALTER TABLE fund_fee_schedule ADD COLUMN raw_label TEXT;
ALTER TABLE fund_fee_meta ADD COLUMN redemption_1y_pct REAL;
ALTER TABLE fund_fee_meta ADD COLUMN redemption_3y_pct REAL;
