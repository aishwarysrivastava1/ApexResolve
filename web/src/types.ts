// Shapes of the API responses (APX-07). Money is always an integer number of minor units (paise).
// Decimal scores (v_m, v_cm, margin) arrive as strings so no precision is lost; the UI only displays them.

export type Role = "cardmember" | "merchant" | "reviewer" | "auditor";

// Who is signed in (read from the token for display only; the server checks everything again).
export interface User {
  token: string;
  sub: string;
  role: Role;
  name: string;
  merchantId: string | null;
}

// RFC 9457 error body returned by every failed request.
export interface Problem {
  type: string;
  title: string;
  status: number;
  detail: string;
  request_id: string | null;
  errors?: { field: string; type: string }[];
}

export interface Transaction {
  transaction_id: string;
  merchant_name: string;
  amount_minor: number;
  currency: string;
  transaction_at: string;
  order_ref: string | null;
  card_display_mask: string;
  dispute_id: string | null;
}

export interface DisputeSummary {
  dispute_id: string;
  reason_code: ReasonCode;
  state: string;
  state_due_at: string | null;
  status_reason: string | null;
  disputed_amount_minor: number;
  currency: string;
  transaction_id: string;
  created_at: string;
  merchant_name: string;
}

export type ReasonCode = "C08" | "C31" | "C02" | "P08";
export type Side = "CARDMEMBER" | "MERCHANT";

export interface EvidenceItem {
  evidence_id: string;
  seq: number;
  side: Side;
  submitted_by: string;
  evidence_type: string;
  source: "system_verified" | "document" | "self_attested";
  details: Record<string, string | boolean> | null;
  created_at: string;
  file_id: string | null;
  note?: string | null;          // absent for auditors
}

export interface Decision {
  verdict: "CARDMEMBER_REFUND" | "MERCHANT_UPHELD" | "SPLIT_SETTLEMENT" | "WITHDRAWN";
  rule_id: string;
  refund_amount_minor: number;
  v_m: string | null;
  v_cm: string | null;
  margin: string | null;
  appealable: boolean;
  appeal_due_at: string | null;
  explanation: string;
  created_at: string;
  decided_by: string;
}

export interface Offer {
  refund_amount_minor: number;
  expires_at: string;
  cardmember_response: "ACCEPTED" | "DECLINED" | null;
  merchant_response: "ACCEPTED" | "DECLINED" | null;
  v_m: string;
  v_cm: string;
  margin: string;
  explanation: string;
}

export type Action =
  | "add_evidence" | "withdraw" | "rebuttal_done" | "respond_offer" | "appeal"
  | "contest" | "accept" | "review_decide";

export interface DisputeView {
  dispute_id: string;
  reason_code: ReasonCode;
  state: string;
  state_due_at: string | null;
  status_reason: string | null;
  status_explanation: string | null;   // the fixed template for the status reason (E1, R1, R3, R7, offers, appeals)
  appeal_reason?: string | null;       // redacted; parties and reviewers only
  claim_received_at: string;
  filing_deadline_at: string;
  disputed_amount_minor: number;
  currency: string;
  transaction: {
    transaction_id: string;
    merchant_name: string;
    amount_minor: number;
    transaction_at: string;
    order_ref: string | null;
    card_display_mask?: string;   // cardmember and reviewer only
  };
  fact_check_result: string | null;
  evidence: EvidenceItem[];
  decision: Decision | null;
  offer: Offer | null;
  allowed_actions: Action[];
  policy_version: string;
  risk_flag?: boolean;          // reviewers and auditors only
}

export interface TimelineItem {
  seq: number;
  event_type: string;
  at: string;
  actor_role: string;
  summary?: string;
  actor?: string;
}

export interface Notification {
  notification_id: string;
  dispute_id: string | null;
  message: string;
  created_at: string;
  read_at: string | null;
}

export interface ReviewItem {
  dispute_id: string;
  reason_code: ReasonCode;
  disputed_amount_minor: number;
  currency: string;
  status_reason: string | null;
  waiting_since: string;
  v_m: string | null;
  v_cm: string | null;
  margin: string | null;
}

export interface AuditEvent {
  seq: number;
  dispute_id: string;
  event_type: string;
  actor: string;
  created_at_text: string;
  payload_canonical: string;
  prev_hash: string;
  entry_hash: string;
  key_id: string;
  signature: string;
}

export interface Checkpoint {
  seq: number;
  entry_hash: string;
  created_at: string;
  key_id: string;
  signature: string;
}

export interface MetricsSummary {
  period: { from: string; to: string };
  counts_by_state: Record<string, number>;
  closed: number;
  automation_rate: string | null;
  fast_path_rate: string | null;
  offer_rate: string | null;
  offer_acceptance_rate: string | null;
  human_review_rate: string | null;
  appeal_count: number;
  time_to_close_days_p50: number | null;
}

// The part of the policy file the UI needs to build the evidence form.
export interface CatalogueEntry {
  side: Side;
  weight_bp: number;
  compelling: boolean;
  produced_by: "file" | "text" | "tracking";
}

export interface Policy {
  policy_version: string;
  evidence_catalogue: Record<ReasonCode, Record<string, CatalogueEntry>>;
}

export interface ListPage<T> {
  items: T[];
  next_cursor?: string | null;
}
