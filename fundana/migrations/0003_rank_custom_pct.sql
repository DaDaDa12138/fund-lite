-- 0003：东财排行第 18 段实测为「自定义区间涨幅」，不是规模。
-- 保留 scale_yi 列（历史数据留痕）但不再写入；新增 custom_pct 承载真实语义。
ALTER TABLE src_rank_row ADD COLUMN custom_pct REAL;
