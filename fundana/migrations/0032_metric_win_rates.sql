-- 0032 P0-1：日胜率三分解。低波动债基大量交易日收益四舍五入为 0.00%，
-- 旧 positive_ratio_pct 把持平日记为“未上涨”，与低波动目标方向相反。
-- 新口径：positive=上涨日；flat=持平(0.00%)日；non_negative=上涨+持平。
ALTER TABLE fund_metric ADD COLUMN flat_ratio_pct REAL;
ALTER TABLE fund_metric ADD COLUMN non_negative_ratio_pct REAL;
