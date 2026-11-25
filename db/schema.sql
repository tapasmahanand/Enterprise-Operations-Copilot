-- Kestrel ops copilot schema. One Postgres for knowledge and workflow state.
CREATE EXTENSION IF NOT EXISTS vector;

-- Knowledge ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    document_id     TEXT PRIMARY KEY,            -- stable id from front-matter
    title           TEXT NOT NULL,
    source_path     TEXT NOT NULL UNIQUE,
    document_type   TEXT NOT NULL,               -- policy | sop | script | matrix | directory | runbook | guideline
    department      TEXT NOT NULL,
    sensitivity     TEXT NOT NULL DEFAULT 'internal',  -- public | internal | restricted
    effective_from  DATE,
    status          TEXT NOT NULL DEFAULT 'current',   -- current | superseded
    supersedes      TEXT,
    content_hash    TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id        TEXT PRIMARY KEY,            -- {document_id}:{chunk_index}
    document_id     TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    chunk_index     INT  NOT NULL,
    chunk_text      TEXT NOT NULL,
    token_count     INT  NOT NULL,
    metadata        JSONB NOT NULL,
    embedding       vector(1536) NOT NULL,
    tsv             tsvector GENERATED ALWAYS AS (to_tsvector('english', chunk_text)) STORED
);
CREATE INDEX IF NOT EXISTS document_chunks_embedding_hnsw
    ON document_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS document_chunks_tsv ON document_chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS document_chunks_meta ON document_chunks USING gin (metadata);

-- Routing data (BANKING77) ---------------------------------------------------
CREATE TABLE IF NOT EXISTS banking_queries (
    query_id        TEXT PRIMARY KEY,
    split           TEXT NOT NULL,               -- train | test
    user_query      TEXT NOT NULL,
    intent_label    TEXT NOT NULL,
    in_scope        BOOLEAN NOT NULL,
    priority        TEXT NOT NULL,
    department      TEXT NOT NULL,
    risk_level      TEXT NOT NULL
);

-- Workflow state -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    summary         TEXT NOT NULL DEFAULT '',
    entities        JSONB NOT NULL DEFAULT '{}',
    last_route      TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cases (
    case_id            TEXT PRIMARY KEY,
    session_id         TEXT REFERENCES sessions(session_id),
    status             TEXT NOT NULL,            -- pending_approval | at_risk | breached | approved | rejected | closed
    risk_level         TEXT NOT NULL,
    evidence           JSONB NOT NULL DEFAULT '[]',
    recommended_action JSONB NOT NULL,
    due_at             DATE,                     -- RBI-derived deadline from the calculator
    trace_id           TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id     TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES cases(case_id),
    thread_id       TEXT NOT NULL,
    state           TEXT NOT NULL CHECK (state IN ('pending', 'approved', 'rejected')),
    approver_role   TEXT,
    approver_id     TEXT,
    action_payload  JSONB NOT NULL,
    reason          TEXT,
    decided_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    job_type        TEXT NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}',
    state           TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'done', 'failed')),
    attempts        INT  NOT NULL DEFAULT 0,
    result          JSONB,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_queued ON jobs (created_at) WHERE state = 'queued';

CREATE TABLE IF NOT EXISTS run_events (
    event_id        BIGSERIAL PRIMARY KEY,
    run_id          TEXT NOT NULL,
    thread_id       TEXT,
    node            TEXT NOT NULL,
    status          TEXT NOT NULL,               -- ok | error | blocked
    latency_ms      INT,
    tokens_in       INT,
    tokens_out      INT,
    cost_inr        NUMERIC(12, 4),
    detail          JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS run_events_run ON run_events (run_id);

-- Mock bank systems (seeded) ---------------------------------------------------
CREATE TABLE IF NOT EXISTS mock_customers (
    customer_id     TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    home_branch     TEXT NOT NULL,
    kyc_verified    BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS mock_accounts (
    account_id      TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL REFERENCES mock_customers(customer_id),
    product         TEXT NOT NULL,               -- savings | bsbd | current_small | current_large | credit_card | ppi
    credit_limit_inr INT
);

CREATE TABLE IF NOT EXISTS mock_cards (
    card_id         TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL REFERENCES mock_accounts(account_id),
    last4           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active'  -- active | blocked
);

CREATE TABLE IF NOT EXISTS mock_transactions (
    txn_id          TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL REFERENCES mock_accounts(account_id),
    card_id         TEXT REFERENCES mock_cards(card_id),
    txn_date        DATE NOT NULL,
    alert_sent_on   DATE,
    channel         TEXT NOT NULL,               -- pos | card_not_present | atm | upi_transfer | upi_merchant | imps | card_to_card
    merchant        TEXT,
    amount_inr      INT  NOT NULL,
    status          TEXT NOT NULL,               -- posted | failed_debited | reversed
    cross_border    BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS mock_tickets (
    ticket_id       TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL REFERENCES mock_accounts(account_id),
    opened_on       DATE NOT NULL,
    category        TEXT NOT NULL,
    description     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    notes           JSONB NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS org_directory (
    person_id       TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    role            TEXT NOT NULL,
    team            TEXT NOT NULL,
    email           TEXT NOT NULL
);

-- Milestone 2: drafts, chat runs, mock write-side systems -----------------------
-- A draft is what the copilot proposes; it becomes a case only when the agent
-- clicks "Send to supervisor" (POST /drafts/{id}/submit).
CREATE TABLE IF NOT EXISTS drafts (
    draft_id        TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    action          JSONB NOT NULL,              -- {action, amount_inr, txn_id, ticket_id, approver_role, ...}
    evidence        JSONB NOT NULL DEFAULT '[]',
    calc            JSONB,                       -- calculator output, incl. rule_ids and due dates
    case_id         TEXT,                        -- set on submit
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_runs (
    run_id          TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    message         TEXT NOT NULL,               -- already PII-masked by compliance_in
    state           TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'done', 'failed')),
    result          JSONB,
    trace_id        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Posted credits. UNIQUE(case_id) makes submit_credit idempotent, so a crash between
-- the write and the checkpoint cannot post the same credit twice on resume.
CREATE TABLE IF NOT EXISTS mock_credits (
    credit_id       TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL UNIQUE,
    account_id      TEXT NOT NULL,
    txn_id          TEXT,
    kind            TEXT NOT NULL,               -- refund | shadow_credit | fee_reversal
    amount_inr      INT  NOT NULL,
    reason          TEXT,
    posted_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One credit per transaction, whichever case asks (D38). Two cases for the same payment
-- can both be approved; only the first posts, the second fails with the existing credit.
CREATE UNIQUE INDEX IF NOT EXISTS mock_credits_one_per_txn ON mock_credits (txn_id) WHERE txn_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL UNIQUE,
    channel         TEXT NOT NULL,               -- sms | email
    body            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'draft',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE cases ADD COLUMN IF NOT EXISTS draft_id TEXT;
ALTER TABLE cases ADD COLUMN IF NOT EXISTS approver_role TEXT;
ALTER TABLE cases ADD COLUMN IF NOT EXISTS result JSONB;
CREATE UNIQUE INDEX IF NOT EXISTS approvals_one_per_case ON approvals (case_id);

-- Milestone 3: job queue details, escalations, ticket notes -----------------------
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS run_after   TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS locked_at   TIMESTAMPTZ;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS max_attempts INT NOT NULL DEFAULT 3;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS dedupe_key  TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS requested_by TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS progress    JSONB NOT NULL DEFAULT '{}';
-- At most one live job per dedupe key (e.g. one scheduled TAT sweep at a time).
CREATE UNIQUE INDEX IF NOT EXISTS jobs_live_dedupe ON jobs (dedupe_key) WHERE state IN ('queued', 'running');
DROP INDEX IF EXISTS jobs_queued;
CREATE INDEX IF NOT EXISTS jobs_claimable ON jobs (run_after) WHERE state = 'queued';

CREATE TABLE IF NOT EXISTS escalations (
    escalation_id   TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL,
    level           TEXT NOT NULL CHECK (level IN ('grievance', 'ombudsman_pack')),
    reason          TEXT NOT NULL,
    created_by      TEXT NOT NULL,              -- agent | tat_sweeper
    rule_id         TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (case_id, level)
);

CREATE TABLE IF NOT EXISTS mock_chargebacks (
    chargeback_id   TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL UNIQUE,         -- idempotent per case (D18)
    txn_id          TEXT NOT NULL,
    reason_code     TEXT NOT NULL,
    amount_inr      INT  NOT NULL,
    evidence        JSONB NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'filed',
    filed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Authentication and audit (D50-D52) ------------------------------------------------
-- Who did something comes only from a server-side session created at login, never from
-- the client. Supervisors map to an org_directory person (no FK: the mock-data reseed
-- truncates org_directory with CASCADE and must not wipe the users).
CREATE TABLE IF NOT EXISTS app_users (
    user_id         TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('agent', 'supervisor', 'compliance')),
    person_id       TEXT,                         -- org_directory person: sets the approval tier
    password_hash   TEXT NOT NULL,                -- scrypt$N$r$p$salt$hash
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (role <> 'supervisor' OR person_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash      TEXT PRIMARY KEY,             -- sha256 of the token; the token itself is never stored
    user_id         TEXT NOT NULL REFERENCES app_users(user_id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ
);

-- Append-only: rows can be inserted, never changed or removed (trigger below). This stops
-- the application and anyone using its code paths; it does not stop the database owner,
-- who can drop the trigger (see README, "What is still unprotected").
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id        BIGSERIAL PRIMARY KEY,
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_user_id   TEXT,                         -- NULL for failed logins and system actions
    actor_role      TEXT,
    actor_person_id TEXT,
    action          TEXT NOT NULL,                -- login | login_failed | chat | draft_submitted | approval_decided | ...
    object_type     TEXT NOT NULL,
    object_id       TEXT,
    detail          JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS audit_log_object ON audit_log (object_type, object_id);

CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only (% blocked)', TG_OP;
END $$;
DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log;
CREATE TRIGGER audit_log_no_update BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();
DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_append_only();

-- Who created, submitted and decided each item (the authenticated user id).
ALTER TABLE chat_runs ADD COLUMN IF NOT EXISTS user_id TEXT;
ALTER TABLE chat_runs ADD COLUMN IF NOT EXISTS idv_level INT;   -- as attested by user_id (D51)
ALTER TABLE drafts    ADD COLUMN IF NOT EXISTS created_by TEXT;
ALTER TABLE cases     ADD COLUMN IF NOT EXISTS submitted_by TEXT;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS decided_by TEXT;
