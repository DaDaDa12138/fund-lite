-- P0-4：逐只记录指标截止日与容忍天数，报告/API 可解释“为什么用了旧 1 天的指标”。
ALTER TABLE screen_decision ADD COLUMN data_flags_json TEXT;
