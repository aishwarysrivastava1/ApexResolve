// One dispute: facts, decision or offer, the actions this role may take now, evidence and timeline.
// The buttons come from allowed_actions; the server re-checks every action anyway.
import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { downloadFile, errorMessage, postJson } from "../api";
import { EVENT_LABELS, formatMoney, formatScore, formatTime, humanize, parseMoney, REASONS, stateLabel } from "../format";
import type { DisputeView, ListPage, TimelineItem, User } from "../types";
import { useData } from "../useData";
import EvidenceForm from "./EvidenceForm";

const VERDICTS: Record<string, string> = {
  CARDMEMBER_REFUND: "Refund to the cardmember",
  MERCHANT_UPHELD: "Charge stands (merchant upheld)",
  SPLIT_SETTLEMENT: "Split settlement",
  WITHDRAWN: "Withdrawn by the cardmember",
};

// simple actions: one click (after a confirmation) and no body
const SIMPLE_ACTIONS: Record<string, { path: string; label: string; confirm: string }> = {
  contest: { path: "contest", label: "Contest the dispute", confirm: "Contest this dispute with the evidence you added?" },
  accept: { path: "accept", label: "Accept and refund", confirm: "Accept the dispute and refund the full disputed amount?" },
  withdraw: { path: "withdraw", label: "Withdraw my dispute", confirm: "Withdraw this dispute? The charge will stand." },
  rebuttal_done: { path: "rebuttal-done", label: "I have nothing more to add", confirm: "Send the case for a decision now?" },
};

interface Props {
  disputeId: string;
  user: User;
  onBack: () => void;
}

export default function DisputeDetail({ disputeId, user, onBack }: Props) {
  const dispute = useData<DisputeView>(`/api/v1/disputes/${disputeId}`);
  const timeline = useData<ListPage<TimelineItem>>(`/api/v1/disputes/${disputeId}/timeline`);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const reloadDispute = dispute.reload;
  const reloadTimeline = timeline.reload;
  // while the worker is deciding or sending the refund, refresh every 3 seconds (it takes about one)
  const workerBusy = dispute.data?.state === "READY_FOR_DECISION" || dispute.data?.state === "SETTLEMENT_PENDING";
  useEffect(() => {
    if (!workerBusy) {
      return;
    }
    const timer = window.setInterval(() => {
      reloadDispute();
      reloadTimeline();
    }, 3000);
    return () => window.clearInterval(timer);
  }, [workerBusy, reloadDispute, reloadTimeline]);

  // every action ends the same way: show errors, then reload the dispute and its timeline
  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(false);
      dispute.reload();
      timeline.reload();
    }
  };

  if (dispute.error) {
    return <section className="card"><p className="error">{dispute.error}</p><button onClick={onBack}>Back</button></section>;
  }
  const d = dispute.data;
  if (d === null) {
    return <section className="card"><p>Loading…</p></section>;
  }
  const base = `/api/v1/disputes/${d.dispute_id}`;
  const can = (action: string) => d.allowed_actions.includes(action as DisputeView["allowed_actions"][number]);

  return (
    <>
      <section className="card">
        <button className="link" onClick={onBack}>← Back</button>
        <h2>{REASONS[d.reason_code]} · {d.transaction.merchant_name}</h2>
        <p className="status">
          <strong>{stateLabel(d.state)}</strong>
          {d.state_due_at && <> · next step due {formatTime(d.state_due_at)}</>}
        </p>
        {d.status_explanation && <p>{d.status_explanation}</p>}
        {d.appeal_reason && <p className="note">Appeal reason: {d.appeal_reason}</p>}
        <dl className="facts">
          <dt>Disputed amount</dt><dd>{formatMoney(d.disputed_amount_minor, d.currency)}</dd>
          <dt>Charge</dt><dd>{formatMoney(d.transaction.amount_minor, d.currency)} on {formatTime(d.transaction.transaction_at)}</dd>
          {d.transaction.order_ref && <><dt>Order</dt><dd>{d.transaction.order_ref}</dd></>}
          {d.transaction.card_display_mask && <><dt>Card</dt><dd>{d.transaction.card_display_mask}</dd></>}
          <dt>Filed</dt><dd>{formatTime(d.claim_received_at)}</dd>
          <dt>Filing deadline</dt><dd>{formatTime(d.filing_deadline_at)}</dd>
          {d.fact_check_result && <><dt>Network records check</dt><dd>{humanize(d.fact_check_result)}</dd></>}
          {d.risk_flag !== undefined && <><dt>Risk flag</dt><dd>{d.risk_flag ? "Yes" : "No"}</dd></>}
          <dt>Policy</dt><dd>{d.policy_version}</dd>
        </dl>
      </section>

      {d.decision && (
        <section className="card decision">
          <h3>Decision: {VERDICTS[d.decision.verdict]}</h3>
          <p>{d.decision.explanation}</p>
          <p className="hint">
            Refund {formatMoney(d.decision.refund_amount_minor, d.currency)} · rule {d.decision.rule_id} ·
            decided by {d.decision.decided_by === "SYSTEM" ? "the rules engine" : "a reviewer"}
            {d.decision.margin !== null && <> · evidence margin {formatScore(d.decision.margin)}</>}
            {d.decision.appealable && d.decision.appeal_due_at && <> · appeal by {formatTime(d.decision.appeal_due_at)}</>}
          </p>
        </section>
      )}

      {d.offer && (
        <section className="card">
          <h3>Settlement offer: {formatMoney(d.offer.refund_amount_minor, d.currency)}</h3>
          <p>{d.offer.explanation}</p>
          <p className="hint">
            Expires {formatTime(d.offer.expires_at)} · cardmember: {d.offer.cardmember_response ?? "no answer yet"} ·
            merchant: {d.offer.merchant_response ?? "no answer yet"}
          </p>
          {can("respond_offer") && (
            <div className="row">
              <button disabled={busy} onClick={() => run(() => postJson(`${base}/offer-response`, { response: "ACCEPTED" }))}>Accept offer</button>
              <button disabled={busy} className="secondary" onClick={() => run(() => postJson(`${base}/offer-response`, { response: "DECLINED" }))}>Decline offer</button>
            </div>
          )}
        </section>
      )}

      {error && <p className="error card" role="alert">{error}</p>}

      <section className="card">
        <h3>What you can do now</h3>
        {d.allowed_actions.length === 0 && <p className="hint">Nothing is needed from you at this stage.</p>}
        <div className="row">
          {Object.entries(SIMPLE_ACTIONS).filter(([name]) => can(name)).map(([name, action]) => (
            <button key={name} disabled={busy}
              onClick={() => window.confirm(action.confirm) && run(() => postJson(`${base}/${action.path}`))}>
              {action.label}
            </button>
          ))}
        </div>
        {can("add_evidence") && <EvidenceForm dispute={d} role={user.role} onAdded={() => { dispute.reload(); timeline.reload(); }} />}
        {can("appeal") && <AppealForm busy={busy} onSubmit={(reason) => run(() => postJson(`${base}/appeal`, { reason }))} />}
        {can("review_decide") && (
          <ReviewForm busy={busy} maxMinor={d.disputed_amount_minor}
            onSubmit={(body) => run(() => postJson(`${base}/review-decision`, body))} />
        )}
      </section>

      <section className="card">
        <h3>Evidence</h3>
        {d.evidence.length === 0 && <p className="hint">No evidence yet.</p>}
        <ul className="evidence">
          {d.evidence.map((e) => (
            <li key={e.evidence_id}>
              <strong>{humanize(e.evidence_type)}</strong> · {e.side === "MERCHANT" ? "merchant side" : "cardmember side"} ·
              {" "}{humanize(e.source)} · {formatTime(e.created_at)}
              {e.note && <p className="note">{e.note}</p>}
              {e.details && <p className="hint">{Object.entries(e.details).map(([k, v]) => `${humanize(k)}: ${String(v)}`).join(" · ")}</p>}
              {e.file_id && user.role !== "auditor" && (
                <button className="link" onClick={() => run(() => downloadFile(`${base}/files/${e.file_id}`, `evidence-${e.seq}`))}>
                  Download file
                </button>
              )}
            </li>
          ))}
        </ul>
      </section>

      <section className="card">
        <h3>Timeline</h3>
        <ol className="timeline">
          {(timeline.data?.items ?? []).map((t) => (
            <li key={t.seq}>
              {formatTime(t.at)} · {EVENT_LABELS[t.event_type] ?? humanize(t.event_type)}
              {t.summary && <> ({t.summary.split(" → ").map(stateLabel).join(" → ")})</>} · {humanize(t.actor_role)}
            </li>
          ))}
        </ol>
      </section>
    </>
  );
}

function AppealForm({ busy, onSubmit }: { busy: boolean; onSubmit: (reason: string) => void }) {
  const [reason, setReason] = useState("");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onSubmit(reason);
  };
  return (
    <form onSubmit={submit}>
      <label>
        Why should a reviewer look again? (20–1000 characters, one appeal per case)
        <textarea value={reason} maxLength={1000} rows={3} onChange={(e) => setReason(e.target.value)} />
      </label>
      <button type="submit" disabled={busy || reason.trim().length < 20}>Appeal</button>
    </form>
  );
}

interface ReviewBody {
  verdict: string;
  refund_amount_minor?: number;
  rationale: string;
}

function ReviewForm({ busy, maxMinor, onSubmit }: { busy: boolean; maxMinor: number; onSubmit: (body: ReviewBody) => void }) {
  const [verdict, setVerdict] = useState("CARDMEMBER_REFUND");
  const [amount, setAmount] = useState("");
  const [rationale, setRationale] = useState("");
  const [error, setError] = useState<string | null>(null);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const body: ReviewBody = { verdict, rationale };
    if (verdict === "SPLIT_SETTLEMENT") {
      // a split must be strictly between nothing and everything
      const minor = parseMoney(amount);
      if (minor === null || minor < 1 || minor >= maxMinor) {
        setError(`A split refund must be more than ₹0 and less than ${formatMoney(maxMinor, "INR")}.`);
        return;
      }
      body.refund_amount_minor = minor;
    }
    setError(null);
    onSubmit(body);
  };
  return (
    <form onSubmit={submit}>
      <label>
        Verdict
        <select aria-label="Verdict" value={verdict} onChange={(e) => setVerdict(e.target.value)}>
          <option value="CARDMEMBER_REFUND">{VERDICTS.CARDMEMBER_REFUND}</option>
          <option value="MERCHANT_UPHELD">{VERDICTS.MERCHANT_UPHELD}</option>
          <option value="SPLIT_SETTLEMENT">{VERDICTS.SPLIT_SETTLEMENT}</option>
        </select>
      </label>
      {verdict === "SPLIT_SETTLEMENT" && (
        <label>
          Refund to the cardmember (₹)
          <input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" />
        </label>
      )}
      <label>
        Rationale (shown to both parties, 20–2000 characters)
        <textarea value={rationale} maxLength={2000} rows={4} onChange={(e) => setRationale(e.target.value)} />
      </label>
      {error && <p className="error">{error}</p>}
      <button type="submit" disabled={busy || rationale.trim().length < 20}>Record decision</button>
    </form>
  );
}
