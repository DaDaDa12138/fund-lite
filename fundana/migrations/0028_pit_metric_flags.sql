-- 0028 P0-2：把可比性旗标冻结进 P0-1 输入快照。
-- 否则重放时 base_gate 看不到当时的旗标，5xx 只基金会全部出现 gate mismatch；
-- 旗标本身也可能随净值更新变化，必须按 run 冻结而不是重算。
ALTER TABLE screen_run_input ADD COLUMN metric_flags_json TEXT;
