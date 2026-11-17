-- 0019: 冻结每个 screen_batch 的滞回基线，保证同批次重跑使用同一前序榜单。
-- 背景：同日重跑若按 created_at 取“最近一次 done”，第一次与第二次的
-- incumbents 会不同（第一次看上批、第二次看本批第一次），exit 档滞回
-- 因而可能改变名单。把基线冻结进 screen_batch 后，同批次所有 run 共用。
ALTER TABLE screen_batch ADD COLUMN baseline_run_id TEXT;
