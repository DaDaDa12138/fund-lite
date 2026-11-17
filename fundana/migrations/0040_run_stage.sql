-- 日更阶段落库：过去 stage 只存在于进程内存与 stdout，journal 里看不到，
-- 失败后也无法回答"卡在哪一步"。每追加一条 stage 即提交，进程被杀也留痕。
CREATE TABLE IF NOT EXISTS run_stage(
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    detail_json TEXT,
    PRIMARY KEY(run_id, seq),
    FOREIGN KEY(run_id) REFERENCES run(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_run_stage_run ON run_stage(run_id, seq);
