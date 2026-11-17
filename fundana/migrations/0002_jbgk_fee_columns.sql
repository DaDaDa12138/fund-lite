-- 0002: jbgk 档案补充最高申购/赎回费率（parse_jbgk 已解析，但 0001 未建列）
ALTER TABLE src_jbgk_profile ADD COLUMN max_sub_fee_pct REAL;
ALTER TABLE src_jbgk_profile ADD COLUMN max_red_fee_pct REAL;
