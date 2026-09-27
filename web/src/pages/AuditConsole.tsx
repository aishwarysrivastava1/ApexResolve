// Auditor console: verify the signed hash chain (optionally against a saved checkpoint),
// create and download checkpoints, and browse ledger events page by page.
import { useState } from "react";
import { errorMessage, getJson, postJson, postText, saveBlob } from "../api";
import { EVENT_LABELS, humanize } from "../format";
import type { AuditEvent, Checkpoint, ListPage } from "../types";
import { useData } from "../useData";

interface VerifyResult {
  ok: boolean;
  message: string;
}

function saveCheckpoint(checkpoint: Checkpoint): void {
  const blob = new Blob([JSON.stringify(checkpoint, null, 2)], { type: "application/json" });
  saveBlob(blob, `checkpoint-seq-${checkpoint.seq}.json`);
}

export default function AuditConsole({ onOpen }: { onOpen: (disputeId: string) => void }) {
  const [afterSeq, setAfterSeq] = useState(0);
  const events = useData<ListPage<AuditEvent>>(`/api/v1/audit/events?after_seq=${afterSeq}&limit=50`);
  const [result, setResult] = useState<VerifyResult | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const act = async (work: () => Promise<void>) => {
    setBusy(true);
    setMessage(null);
    try {
      await work();
    } catch (failure) {
      setMessage(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  };

  // verify without a checkpoint (chain and signatures), or against a checkpoint file the auditor kept
  const verify = (file: File | null) => act(async () => {
    const body = file ? await file.text() : "";
    setResult(await postText<VerifyResult>("/api/v1/audit/verify", body));
  });
  const createCheckpoint = () => act(async () => {
    saveCheckpoint(await postJson<Checkpoint>("/api/v1/audit/checkpoints"));
    setMessage("Checkpoint created and downloaded. Keep the file outside this system.");
  });
  const downloadLatest = () => act(async () => {
    saveCheckpoint(await getJson<Checkpoint>("/api/v1/audit/checkpoints/latest"));
  });

  const items = events.data?.items ?? [];
  return (
    <>
      <section className="card">
        <h2>Ledger integrity</h2>
        <div className="row">
          <button disabled={busy} onClick={() => verify(null)}>Verify chain</button>
          <label className="file-button">
            Verify against a checkpoint file
            <input type="file" accept="application/json" disabled={busy}
              onChange={(e) => verify(e.target.files?.[0] ?? null)} />
          </label>
          <button disabled={busy} className="secondary" onClick={createCheckpoint}>Create checkpoint</button>
          <button disabled={busy} className="secondary" onClick={downloadLatest}>Download latest checkpoint</button>
        </div>
        {result && (
          <p className={result.ok ? "ok" : "error"} role="status">
            {result.ok ? "✔ " : "✘ "}{result.message}
          </p>
        )}
        {message && <p className="hint">{message}</p>}
      </section>
      <section className="card">
        <h2>Ledger events</h2>
        {events.error && <p className="error">{events.error}</p>}
        <table>
          <thead>
            <tr><th>#</th><th>Time (UTC)</th><th>Event</th><th>Actor</th><th>Entry hash</th></tr>
          </thead>
          <tbody>
            {items.map((e) => (
              <tr key={e.seq} className="clickable" onClick={() => onOpen(e.dispute_id)}>
                <td>{e.seq}</td>
                <td>{e.created_at_text}</td>
                <td>{EVENT_LABELS[e.event_type] ?? humanize(e.event_type)}</td>
                <td>{e.actor}</td>
                <td className="mono">{e.entry_hash.slice(0, 16)}…</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="row">
          {afterSeq > 0 && <button className="secondary" onClick={() => setAfterSeq(0)}>First page</button>}
          {items.length === 50 && (
            <button className="secondary" onClick={() => setAfterSeq(items[items.length - 1].seq)}>Next page</button>
          )}
        </div>
      </section>
    </>
  );
}
