// Add one piece of evidence. The choices come from the policy's evidence catalogue for this
// reason code and this party's side, so the form never offers something the server would refuse.
import { useState } from "react";
import type { FormEvent } from "react";
import { errorMessage, postForm } from "../api";
import { humanize } from "../format";
import type { DisputeView, Policy, Role } from "../types";
import { useData } from "../useData";

const CARRIERS = ["BLUEDART", "DELHIVERY", "DTDC", "INDIA_POST"];

interface Props {
  dispute: DisputeView;
  role: Role;
  onAdded: () => void;
}

export default function EvidenceForm({ dispute, role, onAdded }: Props) {
  const policy = useData<Policy>("/api/v1/policy");
  const [choice, setChoice] = useState("");
  const [note, setNote] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [carrier, setCarrier] = useState(CARRIERS[0]);
  const [tracking, setTracking] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (policy.data === null) {
    return null;
  }
  // 1. the evidence types this side may upload or write for this reason code
  const side = role === "merchant" ? "MERCHANT" : "CARDMEMBER";
  const catalogue = policy.data.evidence_catalogue[dispute.reason_code];
  const options: { value: string; label: string }[] = [];
  for (const [type, entry] of Object.entries(catalogue)) {
    if (entry.side === side && entry.produced_by !== "tracking") {
      options.push({ value: `${entry.produced_by}:${type}`, label: humanize(type) });
    }
  }
  // 2. carrier tracking: merchants prove delivery (C08); cardmembers prove a return (C31, C02)
  if (side === "MERCHANT" && dispute.reason_code === "C08") {
    options.push({ value: "shipment_tracking:", label: "Carrier tracking number (checked with the carrier)" });
  }
  if (side === "CARDMEMBER" && (dispute.reason_code === "C31" || dispute.reason_code === "C02")) {
    options.push({ value: "return_tracking:", label: "Return tracking number (checked with the carrier)" });
  }
  const [kind, evidenceType] = (choice || options[0]?.value || ":").split(":");

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    // 3. the API takes multipart form data: kind plus the fields that kind needs
    const form = new FormData();
    form.append("kind", kind);
    if (kind === "file" || kind === "text") {
      form.append("evidence_type", evidenceType);
    }
    if (note.trim()) {
      form.append("note", note);
    }
    if (kind === "file" && file) {
      form.append("file", file);
    }
    if (kind.endsWith("_tracking")) {
      form.append("carrier", carrier);
      form.append("tracking_number", tracking.trim());
    }
    setBusy(true);
    setError(null);
    try {
      await postForm(`/api/v1/disputes/${dispute.dispute_id}/evidence`, form);
      setNote("");
      setFile(null);
      setTracking("");
      onAdded();
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="subform">
      <h4>Add evidence</h4>
      <label>
        Type
        <select aria-label="Evidence type" value={choice || options[0]?.value} onChange={(e) => setChoice(e.target.value)}>
          {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      </label>
      {kind === "file" && (
        <label>
          File (PDF, JPEG or PNG, up to 5 MB)
          <input type="file" accept="application/pdf,image/jpeg,image/png"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </label>
      )}
      {(kind === "text" || kind === "file") && (
        <label>
          {kind === "text" ? "Your statement" : "Note (optional)"} ({note.length}/2000)
          <textarea value={note} maxLength={2000} rows={3} onChange={(e) => setNote(e.target.value)} />
        </label>
      )}
      {kind.endsWith("_tracking") && (
        <div className="row">
          <label>
            Carrier
            <select aria-label="Carrier" value={carrier} onChange={(e) => setCarrier(e.target.value)}>
              {CARRIERS.map((c) => <option key={c} value={c}>{humanize(c)}</option>)}
            </select>
          </label>
          <label>
            Tracking number
            <input aria-label="Tracking number" value={tracking} onChange={(e) => setTracking(e.target.value)} maxLength={30} />
          </label>
        </div>
      )}
      <p className="hint">Do not include card numbers, Aadhaar or PAN numbers, phone numbers or bank details.</p>
      {error && <p className="error" role="alert">{error}</p>}
      <button type="submit" disabled={busy}>Add evidence</button>
    </form>
  );
}
