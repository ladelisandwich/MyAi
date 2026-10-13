-- Remote updater: what each computer runs (Hello.build), the last check the owner ran, and one row per
-- remote update. The partial unique index allows at most one active update in the whole mesh.
ALTER TABLE nodes ADD COLUMN build_json TEXT;
ALTER TABLE nodes ADD COLUMN update_check_json TEXT;
CREATE TABLE node_updates (
    id              TEXT PRIMARY KEY,       -- the update.apply job id
    node_id         TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    requested_by    TEXT,
    requested_at    TEXT NOT NULL,
    expected_commit TEXT NOT NULL,
    from_commit     TEXT,
    force           INTEGER NOT NULL DEFAULT 0,
    state           TEXT NOT NULL,          -- requested|running|ok|ok_unconfirmed|already_up_to_date|rolled_back|failed|interrupted|not_started|abandoned
    active          INTEGER,                -- 1 while requested/running, NULL afterwards
    run_id          TEXT,
    phase           TEXT,
    report_json     TEXT,
    last_report_at  TEXT,
    finished_at     TEXT,
    error_code      TEXT,
    error           TEXT,
    hint            TEXT
);
CREATE UNIQUE INDEX node_updates_one_active ON node_updates(active) WHERE active IS NOT NULL;
CREATE INDEX node_updates_by_node ON node_updates(node_id, requested_at);
