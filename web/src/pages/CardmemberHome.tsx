// Cardmember home: "My charges" (with a Dispute button) and "My disputes".
import { useState } from "react";
import type { FormEvent } from "react";
import { errorMessage, postJson } from "../api";
import { formatMoney, formatTime, parseMoney, REASONS } from "../format";
import type { DisputeView, ListPage, ReasonCode, Transaction } from "../types";
import { useData } from "../useData";
import DisputeList from "./DisputeList";

export default function CardmemberHome({ onOpen }: { onOpen: (disputeId: string) => void }) {
  const charges = useData<ListPage<Transaction>>("/api/v1/me/transactions");
  const [filing, setFiling] = useState<Transaction | null>(null);

  return (
    <>
      <section className="card">
        <h2>My charges (last 180 days)</h2>
        {charges.error && <p className="error">{charges.error}</p>}
        <table>
          <thead>
            <tr><th>Date</th><th>Merchant</th><th>Amount</th><th>Card</th><th></th></tr>
          </thead>
          <tbody>
            {(charges.data?.items ?? []).map((t) => (
              <tr key={t.transaction_id}>
                <td>{formatTime(t.transaction_at)}</td>
                <td>{t.merchant_name}{t.order_ref ? ` (${t.order_ref})` : ""}</td>
                <td>{formatMoney(t.amount_minor, t.currency)}</td>
                <td>{t.card_display_mask}</td>
                <td>
                  {t.dispute_id
                    ? <button className="link" onClick={() => onOpen(t.dispute_id as string)}>View dispute</button>
                    : <button onClick={() => setFiling(t)}>Dispute</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      {filing && (
        <FileDisputeForm key={filing.transaction_id} charge={filing} onCancel={() => setFiling(null)}
          onFiled={(id) => { setFiling(null); onOpen(id); }} />
      )}
      <DisputeList title="My disputes" sortByDue={false} onOpen={onOpen} />
    </>
  );
}

interface FormProps {
  charge: Transaction;
  onCancel: () => void;
  onFiled: (disputeId: string) => void;
}

function FileDisputeForm({ charge, onCancel, onFiled }: FormProps) {
  const [reason, setReason] = useState<ReasonCode>("C08");
  const [amount, setAmount] = useState(String(charge.amount_minor / 100));
  const [statement, setStatement] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // one key per opened form: a double click or a retry can never create two disputes
  const [idempotencyKey] = useState(() => crypto.randomUUID());

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const amountMinor = parseMoney(amount);
    if (amountMinor === null || amountMinor < 1 || amountMinor > charge.amount_minor) {
      setError(`Enter an amount between ₹0.01 and ${formatMoney(charge.amount_minor, charge.currency)}.`);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const body = { transaction_id: charge.transaction_id, reason_code: reason,
                     disputed_amount_minor: amountMinor, statement };
      const view = await postJson<DisputeView>("/api/v1/disputes", body, { "Idempotency-Key": idempotencyKey });
      onFiled(view.dispute_id);
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="card" onSubmit={submit}>
      <h2>Dispute {charge.merchant_name} · {formatMoney(charge.amount_minor, charge.currency)}</h2>
      <fieldset>
        <legend>What went wrong?</legend>
        {Object.entries(REASONS).map(([code, text]) => (
          <label key={code} className="radio">
            <input type="radio" name="reason" value={code} checked={reason === code}
              onChange={() => setReason(code as ReasonCode)} />
            {text}
          </label>
        ))}
      </fieldset>
      <label>
        Amount you dispute (₹)
        <input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" />
      </label>
      <label>
        What happened? ({statement.length}/2000)
        <textarea value={statement} maxLength={2000} rows={5} onChange={(e) => setStatement(e.target.value)} />
      </label>
      <p className="hint">Do not include card numbers, Aadhaar or PAN numbers, phone numbers or bank details.</p>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="row">
        <button type="submit" disabled={busy || statement.trim().length === 0}>File dispute</button>
        <button type="button" className="secondary" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}
