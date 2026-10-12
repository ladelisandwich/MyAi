-- HomeMesh AI initial schema (version 1).
-- Conventions: TEXT primary keys (prefixed sortable ids), timestamps are UTC ISO-8601 TEXT,
-- JSON stored as TEXT, booleans as INTEGER 0/1. Every table holding personal data carries
-- user_id so that additional users can be added later without mixing their data.

CREATE TABLE users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'owner' CHECK (role IN ('owner', 'member')),
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    password_changed_at TEXT
);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT,
    user_agent TEXT,
    ip TEXT,
    revoked_at TEXT
);
CREATE INDEX idx_sessions_user ON sessions(user_id);

CREATE TABLE api_tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    scopes TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    expires_at TEXT,
    last_used_at TEXT,
    revoked_at TEXT
);

CREATE TABLE nodes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    embedded INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'balanced',
    weight INTEGER NOT NULL DEFAULT 0,
    os TEXT,
    arch TEXT,
    hostname TEXT,
    software_version TEXT,
    protocol_version INTEGER,
    hardware_json TEXT,
    telemetry_json TEXT,
    limits_json TEXT,
    task_types TEXT NOT NULL DEFAULT '[]',
    allowed_roots TEXT NOT NULL DEFAULT '[]',
    allowed_tools TEXT NOT NULL DEFAULT '[]',
    backends_json TEXT NOT NULL DEFAULT '[]',
    connected INTEGER NOT NULL DEFAULT 0,
    last_connected_at TEXT,
    last_seen_at TEXT,
    last_disconnect_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE node_credentials (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    secret_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    last_used_at TEXT
);
CREATE INDEX idx_node_credentials_node ON node_credentials(node_id);

CREATE TABLE pairing_tokens (
    id TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    created_by TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    used_by_node_id TEXT,
    revoked_at TEXT
);

CREATE TABLE hardware_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    report_json TEXT NOT NULL
);
CREATE INDEX idx_hardware_reports_node ON hardware_reports(node_id, id);

-- Model instances: one row per (node, backend, model name). Re-written whenever a worker
-- advertises its models, so this is a registry of what is actually installed where.
CREATE TABLE models (
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    backend TEXT NOT NULL,
    name TEXT NOT NULL,
    backend_type TEXT NOT NULL,
    info_json TEXT NOT NULL,
    capabilities TEXT NOT NULL DEFAULT '[]',
    loaded INTEGER NOT NULL DEFAULT 0,
    remote INTEGER NOT NULL DEFAULT 0,
    mock INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (node_id, backend, name)
);
CREATE INDEX idx_models_name ON models(name);

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'general',
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    title TEXT NOT NULL DEFAULT 'New conversation',
    title_source TEXT NOT NULL DEFAULT 'default',
    settings_json TEXT NOT NULL DEFAULT '{}',
    pinned INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_conversations_user ON conversations(user_id, updated_at);

CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content TEXT NOT NULL DEFAULT '',
    thinking TEXT,
    status TEXT NOT NULL DEFAULT 'complete'
        CHECK (status IN ('complete', 'streaming', 'cancelled', 'failed', 'interrupted')),
    parent_id TEXT,
    model TEXT,
    backend TEXT,
    backend_type TEXT,
    node_id TEXT,
    job_id TEXT,
    mock INTEGER NOT NULL DEFAULT 0,
    metrics_json TEXT,
    sources_json TEXT,
    context_json TEXT,
    tool_calls_json TEXT,
    tool_name TEXT,
    attachments_json TEXT,
    error_code TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_messages_conversation ON messages(conversation_id, created_at);

CREATE VIRTUAL TABLE messages_fts USING fts5(content, content='messages', content_rowid='rowid',
                                             tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
END;
CREATE TRIGGER messages_au AFTER UPDATE OF content ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'fact' CHECK (kind IN ('fact', 'preference', 'project', 'note')),
    content TEXT NOT NULL,
    normalized_hash TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('user_explicit', 'user_edited', 'model_inferred', 'imported')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'candidate', 'rejected', 'archived')),
    confidence REAL,
    conversation_id TEXT,
    message_id TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    use_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_memories_user ON memories(user_id, status);
CREATE INDEX idx_memories_hash ON memories(user_id, normalized_hash);

CREATE VIRTUAL TABLE memories_fts USING fts5(content, content='memories', content_rowid='rowid',
                                             tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
END;
CREATE TRIGGER memories_au AFTER UPDATE OF content ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TABLE collections (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    root_path TEXT NOT NULL,
    include_globs TEXT NOT NULL DEFAULT '[]',
    exclude_dirs TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    doc_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    embedded_count INTEGER NOT NULL DEFAULT 0,
    last_indexed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (node_id, root_path)
);

CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    rel_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    content_hash TEXT,
    mime TEXT,
    status TEXT NOT NULL,
    error TEXT,
    page_count INTEGER,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    indexed_at TEXT NOT NULL,
    UNIQUE (collection_id, rel_path)
);

CREATE TABLE chunks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    collection_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    content TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    page INTEGER,
    char_start INTEGER,
    char_end INTEGER
);
CREATE INDEX idx_chunks_document ON chunks(document_id, ordinal);
CREATE INDEX idx_chunks_collection ON chunks(collection_id);

CREATE VIRTUAL TABLE chunks_fts USING fts5(content, content='chunks', content_rowid='rowid',
                                           tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
END;

-- Embedding vectors (float32 little-endian blobs) for chunks and memories, per model.
CREATE TABLE embeddings (
    owner_type TEXT NOT NULL CHECK (owner_type IN ('chunk', 'memory')),
    owner_id TEXT NOT NULL,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (owner_type, owner_id, model)
);
CREATE INDEX idx_embeddings_model ON embeddings(owner_type, model);

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'dispatched', 'running', 'completed', 'failed',
                                           'cancelled', 'interrupted')),
    priority TEXT NOT NULL DEFAULT 'normal',
    user_id TEXT,
    conversation_id TEXT,
    message_id TEXT,
    collection_id TEXT,
    node_id TEXT,
    requested_node_id TEXT,
    model TEXT,
    backend TEXT,
    mock INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    result_json TEXT,
    error_code TEXT,
    error TEXT,
    decision_json TEXT,
    metrics_json TEXT,
    progress REAL,
    progress_message TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 1,
    idempotent INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    parent_job_id TEXT,
    created_at TEXT NOT NULL,
    dispatched_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_jobs_status ON jobs(status, priority, created_at);
CREATE INDEX idx_jobs_node ON jobs(node_id, status);
CREATE INDEX idx_jobs_created ON jobs(created_at);

CREATE TABLE job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    kind TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    data_json TEXT
);
CREATE INDEX idx_job_events_job ON job_events(job_id, id);

CREATE TABLE tool_policies (
    tool_name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    approval TEXT NOT NULL DEFAULT 'default' CHECK (approval IN ('default', 'always', 'never_for_reads')),
    updated_at TEXT NOT NULL
);

CREATE TABLE tool_approvals (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    node_id TEXT,
    conversation_id TEXT,
    message_id TEXT,
    side_effect TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'denied', 'expired')),
    reason TEXT,
    requested_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    expires_at TEXT NOT NULL
);
CREATE INDEX idx_tool_approvals_status ON tool_approvals(status);

CREATE TABLE tool_executions (
    id TEXT PRIMARY KEY,
    approval_id TEXT,
    user_id TEXT,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    node_id TEXT,
    conversation_id TEXT,
    message_id TEXT,
    side_effect TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'denied')),
    result_json TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX idx_tool_executions_started ON tool_executions(started_at);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('user', 'node', 'system', 'model', 'cli')),
    actor_id TEXT,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    outcome TEXT NOT NULL DEFAULT 'success',
    ip TEXT,
    details_json TEXT
);
CREATE INDEX idx_audit_ts ON audit_events(ts);

CREATE TABLE attachments (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
    message_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('image', 'document')),
    filename TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
