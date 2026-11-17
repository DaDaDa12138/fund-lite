-- 0010: 长期跟踪榜（Tracker）。规格书 §23.2：
--   「最终优质基金累积跟踪」以**已提交正式快照**为单位去重，重复重建同一快照不得重复加次数。
-- 因此这里用 publication_id（= revision_id）作为幂等键：screening_snapshot 主键即去重凭据。
CREATE TABLE IF NOT EXISTS screening_snapshot (
    publication_id TEXT PRIMARY KEY,
    business_date  TEXT NOT NULL,
    run_id         TEXT,
    generation     INTEGER,
    formal_count   INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_screening_snapshot_date ON screening_snapshot(business_date);

CREATE TABLE IF NOT EXISTS screening_tracker (
    fcode               TEXT PRIMARY KEY,
    first_seen_date     TEXT NOT NULL,
    first_publication_id TEXT NOT NULL,
    last_seen_date      TEXT NOT NULL,
    last_publication_id TEXT NOT NULL,
    appearances         INTEGER NOT NULL DEFAULT 0,   -- 出现在多少个正式快照
    snapshots_total     INTEGER NOT NULL DEFAULT 0,   -- 当时已提交快照总数（频率分母）
    streak              INTEGER NOT NULL DEFAULT 0,   -- 连续在榜快照数
    status              TEXT NOT NULL DEFAULT 'active', -- active | exited
    last_board          TEXT,
    last_rank           INTEGER,
    last_formal_score   REAL,
    prev_formal_score   REAL,
    score_delta         REAL,
    best_rank           INTEGER,
    best_board          TEXT,
    best_formal_score   REAL,
    style_code          TEXT,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_screening_tracker_status ON screening_tracker(status);

CREATE TABLE IF NOT EXISTS screening_tracker_event (
    event_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id      TEXT NOT NULL,
    business_date       TEXT NOT NULL,
    run_id              TEXT,
    fcode               TEXT NOT NULL,
    change              TEXT NOT NULL,   -- entered | held | returned | exited
    board               TEXT,
    rank                INTEGER,
    formal_score        REAL,
    confidence          REAL,
    prev_publication_id TEXT,
    score_delta         REAL,
    detail_json         TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE(publication_id, fcode)
);
CREATE INDEX IF NOT EXISTS idx_tracker_event_fcode ON screening_tracker_event(fcode, publication_id);
CREATE INDEX IF NOT EXISTS idx_tracker_event_date ON screening_tracker_event(business_date);
