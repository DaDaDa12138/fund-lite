-- P1-5：季度/月度维护采集状态。daily 内按 due 触发，避免每次全量重拉。
CREATE TABLE IF NOT EXISTS maintenance_state(
  task          TEXT PRIMARY KEY,
  last_run_at   TEXT,
  last_status   TEXT,
  last_run_id   TEXT,
  detail_json   TEXT
);
