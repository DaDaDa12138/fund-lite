-- ADR-062：排序分 = 0.85×综合分 + 0.15×6 个月收益分位（0–100 量纲）。
-- 综合分衡量"质量"（波动/回撤/夏普），6 个月收益分位衡量"动量"，两者合并后作为榜单排序主键。
-- 与 formal_score 并列存储，便于事后核对"这只为什么排在前面"。
ALTER TABLE screen_pick ADD COLUMN rank_score REAL;
