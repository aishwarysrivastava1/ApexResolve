-- Migration 0001 — runs as apex_owner (never as apex_app).
-- PostgreSQL 16. gen_random_uuid() is built in (PostgreSQL 13+).

-- ============================================================
-- Mock Amex core systems (stand-ins; replaced by adapters in production)
-- ============================================================
CREATE TABLE core_accounts (
    account_token      TEXT PRIMARY KEY,                       -- vault token, never a PAN
    cardmember_ref     TEXT NOT NULL,                          -- equals the IdP "sub" of the cardmember
    status             TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
    shipping_postcode  TEXT NOT NULL CHECK (shipping_postcode ~ '^[0-9]{6}$'),
    display_name       TEXT NOT NULL,
    card_display_mask  TEXT NOT NULL CHECK (card_display_mask ~ '^[0-9]{6}\*{5}[0-9]{4}$')  -- BIN + last four only
);
CREATE INDEX core_accounts_cardmember_idx ON core_accounts (cardmember_ref);

CREATE TABLE merchants (
    merchant_id      TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    return_postcode  TEXT NOT NULL CHECK (return_postcode ~ '^[0-9]{6}$')
);

CREATE TABLE merchant_se_numbers (
    se_number    TEXT PRIMARY KEY CHECK (se_number ~ '^[0-9]{10}$'),
    merchant_id  TEXT NOT NULL REFERENCES merchants (merchant_id) ON DELETE RESTRICT
);
CREATE INDEX merchant_se_numbers_merchant_idx ON merchant_se_numbers (merchant_id);

CREATE TABLE core_transactions (
    transaction_id           TEXT PRIMARY KEY,
    account_token            TEXT NOT NULL REFERENCES core_accounts (account_token) ON DELETE RESTRICT,
    se_number                TEXT NOT NULL REFERENCES merchant_se_numbers (se_number) ON DELETE RESTRICT,
    kind                     TEXT NOT NULL CHECK (kind IN ('CHARGE', 'CREDIT')),
    amount_minor             BIGINT NOT NULL CHECK (amount_minor > 0),
    currency                 CHAR(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    transaction_at           TIMESTAMPTZ NOT NULL,
    order_ref                TEXT,
    expected_delivery_date   DATE,
    original_transaction_id  TEXT REFERENCES core_transactions (transaction_id) ON DELETE RESTRICT,
    CHECK ((kind = 'CREDIT') = (original_transaction_id IS NOT NULL))
);
CREATE INDEX core_transactions_dup_idx ON core_transactions (account_token, se_number, transaction_at);
CREATE INDEX core_transactions_original_idx ON core_transactions (original_transaction_id);

CREATE TABLE core_transfers (                                   -- money movements requested by the engine
    transfer_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key  TEXT NOT NULL UNIQUE,
    dispute_id       UUID NOT NULL,
    amount_minor     BIGINT NOT NULL CHECK (amount_minor > 0),
    currency         CHAR(3) NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE core_ledger_lines (                                -- double entry: one DEBIT + one CREDIT per transfer
    line_id       BIGSERIAL PRIMARY KEY,
    transfer_id   UUID NOT NULL REFERENCES core_transfers (transfer_id) ON DELETE RESTRICT,
    account_ref   TEXT NOT NULL CHECK (account_ref ~ '^(SE|CARD):.+$'),
    direction     TEXT NOT NULL CHECK (direction IN ('DEBIT', 'CREDIT')),
    amount_minor  BIGINT NOT NULL CHECK (amount_minor > 0),
    UNIQUE (transfer_id, direction)
);

-- ============================================================
-- Dispute engine
-- ============================================================
CREATE TABLE disputes (
    dispute_id                UUID PRIMARY KEY,
    transaction_id            TEXT NOT NULL UNIQUE REFERENCES core_transactions (transaction_id) ON DELETE RESTRICT,
    cardmember_ref            TEXT NOT NULL,
    account_token             TEXT NOT NULL REFERENCES core_accounts (account_token) ON DELETE RESTRICT,
    merchant_id               TEXT NOT NULL REFERENCES merchants (merchant_id) ON DELETE RESTRICT,
    se_number                 TEXT NOT NULL REFERENCES merchant_se_numbers (se_number) ON DELETE RESTRICT,
    reason_code               TEXT NOT NULL CHECK (reason_code IN ('C08', 'C31', 'C02', 'P08')),
    transaction_amount_minor  BIGINT NOT NULL CHECK (transaction_amount_minor > 0),
    disputed_amount_minor     BIGINT NOT NULL CHECK (disputed_amount_minor > 0),
    currency                  CHAR(3) NOT NULL,
    transaction_at            TIMESTAMPTZ NOT NULL,
    expected_delivery_date    DATE,
    claim_received_at         TIMESTAMPTZ NOT NULL,
    filing_deadline_at        TIMESTAMPTZ NOT NULL,
    fact_check_result         TEXT CHECK (fact_check_result IN ('CONFIRMED', 'INCONCLUSIVE', 'NOT_FOUND',
                                          'CREDIT_ALREADY_POSTED', 'PARTIAL_CREDIT_FOUND', 'NO_CREDIT_FOUND')),
    risk_flag                 TEXT CHECK (risk_flag IN ('HIGH_DISPUTE_VELOCITY')),
    state                     TEXT NOT NULL CHECK (state IN ('REJECTED_INELIGIBLE', 'AWAITING_MERCHANT',
                                          'AWAITING_CARDMEMBER_REBUTTAL', 'READY_FOR_DECISION', 'SETTLEMENT_OFFERED',
                                          'HUMAN_REVIEW', 'DECIDED', 'SETTLEMENT_PENDING', 'CLOSED')),
    state_entered_at          TIMESTAMPTZ NOT NULL,
    state_due_at              TIMESTAMPTZ,
    status_reason             TEXT,
    current_decision_id       UUID,
    appeal_used               BOOLEAN NOT NULL DEFAULT false,
    appeal_reason_redacted    TEXT,
    policy_version            TEXT NOT NULL,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (disputed_amount_minor <= transaction_amount_minor)
);
CREATE INDEX disputes_merchant_state_idx ON disputes (merchant_id, state);
CREATE INDEX disputes_cardmember_idx ON disputes (cardmember_ref, created_at);
CREATE INDEX disputes_state_idx ON disputes (state);

CREATE TABLE evidence_files (
    file_id       UUID PRIMARY KEY,
    dispute_id    UUID NOT NULL REFERENCES disputes (dispute_id) ON DELETE RESTRICT,
    uploaded_by   TEXT NOT NULL CHECK (uploaded_by IN ('MERCHANT', 'CARDMEMBER')),
    content_type  TEXT NOT NULL CHECK (content_type IN ('application/pdf', 'image/jpeg', 'image/png')),
    size_bytes    INTEGER NOT NULL CHECK (size_bytes > 0 AND size_bytes <= 5242880),
    sha256        CHAR(64) NOT NULL,
    data          BYTEA NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE evidence_items (
    evidence_id    UUID PRIMARY KEY,
    dispute_id     UUID NOT NULL REFERENCES disputes (dispute_id) ON DELETE RESTRICT,
    seq            INTEGER NOT NULL CHECK (seq >= 1),
    side           TEXT NOT NULL CHECK (side IN ('MERCHANT', 'CARDMEMBER')),
    submitted_by   TEXT NOT NULL CHECK (submitted_by IN ('MERCHANT', 'CARDMEMBER', 'SYSTEM')),
    evidence_type  TEXT NOT NULL,
    source         TEXT NOT NULL CHECK (source IN ('system_verified', 'document', 'self_attested')),
    note_redacted  TEXT CHECK (char_length(note_redacted) <= 2000),
    details        JSONB NOT NULL DEFAULT '{}'::jsonb,
    file_id        UUID REFERENCES evidence_files (file_id) ON DELETE RESTRICT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (dispute_id, seq)
);

CREATE TABLE decisions (
    decision_id          UUID PRIMARY KEY,
    dispute_id           UUID NOT NULL REFERENCES disputes (dispute_id) ON DELETE RESTRICT,
    verdict              TEXT NOT NULL CHECK (verdict IN ('CARDMEMBER_REFUND', 'MERCHANT_UPHELD',
                                                          'SPLIT_SETTLEMENT', 'WITHDRAWN')),
    rule_id              TEXT NOT NULL,
    refund_amount_minor  BIGINT NOT NULL CHECK (refund_amount_minor >= 0),
    v_m                  TEXT,
    v_cm                 TEXT,
    margin               TEXT,
    policy_version       TEXT NOT NULL,
    decided_by           TEXT NOT NULL,
    appealable           BOOLEAN NOT NULL,
    appeal_due_at        TIMESTAMPTZ,
    explanation          TEXT NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (appealable = (appeal_due_at IS NOT NULL)),
    CHECK ((verdict IN ('MERCHANT_UPHELD', 'WITHDRAWN')) = (refund_amount_minor = 0))
);
CREATE INDEX decisions_dispute_idx ON decisions (dispute_id, created_at);
ALTER TABLE disputes ADD CONSTRAINT disputes_current_decision_fk
    FOREIGN KEY (current_decision_id) REFERENCES decisions (decision_id) ON DELETE RESTRICT;

CREATE TABLE settlement_offers (
    dispute_id           UUID PRIMARY KEY REFERENCES disputes (dispute_id) ON DELETE RESTRICT,
    refund_amount_minor  BIGINT NOT NULL CHECK (refund_amount_minor > 0),
    v_m                  TEXT NOT NULL,
    v_cm                 TEXT NOT NULL,
    margin               TEXT NOT NULL,
    expires_at           TIMESTAMPTZ NOT NULL,
    cardmember_response  TEXT CHECK (cardmember_response IN ('ACCEPTED', 'DECLINED')),
    merchant_response    TEXT CHECK (merchant_response IN ('ACCEPTED', 'DECLINED')),
    explanation          TEXT NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE outbox (
    outbox_id        UUID PRIMARY KEY,
    dispute_id       UUID NOT NULL REFERENCES disputes (dispute_id) ON DELETE RESTRICT,
    kind             TEXT NOT NULL CHECK (kind IN ('DISPUTE_TRANSFER')),
    payload          JSONB NOT NULL,
    idempotency_key  TEXT NOT NULL UNIQUE,
    status           TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'SENT', 'DEAD')),
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at          TIMESTAMPTZ
);
CREATE INDEX outbox_due_idx ON outbox (status, next_attempt_at);

CREATE TABLE jobs (
    job_id       UUID PRIMARY KEY,
    dispute_id   UUID NOT NULL REFERENCES disputes (dispute_id) ON DELETE RESTRICT,
    kind         TEXT NOT NULL CHECK (kind IN ('MERCHANT_DEADLINE', 'REBUTTAL_DEADLINE', 'DECIDE',
                                               'OFFER_DEADLINE', 'FINALIZE')),
    run_at       TIMESTAMPTZ NOT NULL,
    status       TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'DONE', 'FAILED')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ
);
CREATE INDEX jobs_due_idx ON jobs (status, run_at);

CREATE TABLE notifications (
    notification_id  UUID PRIMARY KEY,
    recipient_role   TEXT NOT NULL CHECK (recipient_role IN ('CARDMEMBER', 'MERCHANT', 'REVIEWER')),
    recipient_ref    TEXT NOT NULL,
    dispute_id       UUID REFERENCES disputes (dispute_id) ON DELETE RESTRICT,
    message          TEXT NOT NULL CHECK (char_length(message) <= 500),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at          TIMESTAMPTZ
);
CREATE INDEX notifications_recipient_idx ON notifications (recipient_role, recipient_ref, created_at);

CREATE TABLE idempotency_keys (
    subject          TEXT NOT NULL,
    idem_key         TEXT NOT NULL CHECK (char_length(idem_key) BETWEEN 8 AND 100),
    request_sha256   CHAR(64) NOT NULL,
    response_status  INTEGER NOT NULL,
    response_body    JSONB NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (subject, idem_key)
);

-- ============================================================
-- Tamper-evident audit ledger
-- ============================================================
CREATE TABLE ledger_keys (
    key_id          TEXT PRIMARY KEY,
    algorithm       TEXT NOT NULL CHECK (algorithm = 'Ed25519'),
    public_key_pem  TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE ledger_head (
    id         SMALLINT PRIMARY KEY CHECK (id = 1),
    last_seq   BIGINT NOT NULL CHECK (last_seq >= 0),
    last_hash  CHAR(64) NOT NULL
);
INSERT INTO ledger_head (id, last_seq, last_hash) VALUES (1, 0, repeat('0', 64));

CREATE TABLE audit_events (
    seq                BIGINT PRIMARY KEY CHECK (seq >= 1),
    dispute_id         UUID NOT NULL,
    event_type         TEXT NOT NULL CHECK (event_type IN ('DISPUTE_CREATED', 'EVIDENCE_ADDED', 'STATE_CHANGED',
                                           'DECISION_RECORDED', 'OFFER_CREATED', 'OFFER_RESPONDED', 'APPEAL_FILED',
                                           'TRANSFER_QUEUED', 'TRANSFER_CONFIRMED')),
    actor              TEXT NOT NULL,
    created_at_text    TEXT NOT NULL CHECK (created_at_text ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$'),
    payload_canonical  TEXT NOT NULL,
    prev_hash          CHAR(64) NOT NULL UNIQUE,
    entry_hash         CHAR(64) NOT NULL UNIQUE,
    key_id             TEXT NOT NULL REFERENCES ledger_keys (key_id) ON DELETE RESTRICT,
    signature          TEXT NOT NULL
);
CREATE INDEX audit_events_dispute_idx ON audit_events (dispute_id, seq);

CREATE TABLE ledger_checkpoints (
    checkpoint_id    BIGSERIAL PRIMARY KEY,
    seq              BIGINT NOT NULL REFERENCES audit_events (seq) ON DELETE RESTRICT,
    entry_hash       CHAR(64) NOT NULL,
    created_at_text  TEXT NOT NULL,
    key_id           TEXT NOT NULL REFERENCES ledger_keys (key_id) ON DELETE RESTRICT,
    signature        TEXT NOT NULL
);

-- ============================================================
-- Append-only enforcement (defence in depth; the hash chain detects what this cannot prevent)
-- ============================================================
CREATE FUNCTION forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'table % is append-only', TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER audit_events_append_only BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_change();
CREATE TRIGGER ledger_checkpoints_append_only BEFORE UPDATE OR DELETE ON ledger_checkpoints
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER evidence_items_append_only BEFORE UPDATE OR DELETE ON evidence_items
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER evidence_files_append_only BEFORE UPDATE OR DELETE ON evidence_files
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER decisions_append_only BEFORE UPDATE OR DELETE ON decisions
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER core_ledger_lines_append_only BEFORE UPDATE OR DELETE ON core_ledger_lines
    FOR EACH ROW EXECUTE FUNCTION forbid_change();

-- ============================================================
-- Least-privilege grants for the application role (no DELETE anywhere, no DDL)
-- ============================================================
GRANT SELECT ON core_accounts, core_transactions, merchants, merchant_se_numbers TO apex_app;
GRANT SELECT, INSERT ON core_transfers, core_ledger_lines TO apex_app;
GRANT SELECT, INSERT, UPDATE ON disputes, settlement_offers, outbox, jobs, notifications TO apex_app;
GRANT SELECT, INSERT ON idempotency_keys TO apex_app;
GRANT SELECT, INSERT ON evidence_files, evidence_items, decisions TO apex_app;
GRANT SELECT, INSERT ON audit_events, ledger_checkpoints, ledger_keys TO apex_app;
GRANT SELECT, UPDATE ON ledger_head TO apex_app;
GRANT USAGE ON SEQUENCE core_ledger_lines_line_id_seq, ledger_checkpoints_checkpoint_id_seq TO apex_app;
