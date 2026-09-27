// A list of disputes the caller may see (the server scopes it by role), with a state filter and paging.
import { useState } from "react";
import { formatMoney, formatTime, STATE_LABELS, stateLabel } from "../format";
import type { DisputeSummary, ListPage } from "../types";
import { useData } from "../useData";

interface Props {
  title: string;
  sortByDue: boolean;
  onOpen: (disputeId: string) => void;
}

export default function DisputeList({ title, sortByDue, onOpen }: Props) {
  const [state, setState] = useState("");
  const [cursor, setCursor] = useState<string | null>(null);
  // build the query string from the filter and the page cursor
  const params = new URLSearchParams({ limit: "50" });
  if (state) {
    params.append("state", state);
  }
  if (cursor) {
    params.append("cursor", cursor);
  }
  const page = useData<ListPage<DisputeSummary>>(`/api/v1/disputes?${params.toString()}`);
  const rows = [...(page.data?.items ?? [])];
  if (sortByDue) {
    // merchants see the most urgent deadline first; rows without a deadline go last
    rows.sort((a, b) => (a.state_due_at ?? "9999").localeCompare(b.state_due_at ?? "9999"));
  }

  return (
    <section className="card">
      <div className="row spread">
        <h2>{title}</h2>
        <select value={state} onChange={(e) => { setState(e.target.value); setCursor(null); }} aria-label="Filter by status">
          <option value="">All statuses</option>
          {Object.entries(STATE_LABELS).map(([code, label]) => (
            <option key={code} value={code}>{label}</option>
          ))}
        </select>
      </div>
      {page.error && <p className="error">{page.error}</p>}
      {rows.length === 0 && !page.error && <p className="hint">Nothing here yet.</p>}
      {rows.length > 0 && (
        <table>
          <thead>
            <tr><th>Filed</th><th>Merchant</th><th>Reason</th><th>Amount</th><th>Status</th><th>Due</th></tr>
          </thead>
          <tbody>
            {rows.map((d) => (
              <tr key={d.dispute_id} className="clickable" onClick={() => onOpen(d.dispute_id)}>
                <td>{formatTime(d.created_at)}</td>
                <td>{d.merchant_name}</td>
                <td>{d.reason_code}</td>
                <td>{formatMoney(d.disputed_amount_minor, d.currency)}</td>
                <td>{stateLabel(d.state)}</td>
                <td>{formatTime(d.state_due_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="row">
        {cursor && <button className="secondary" onClick={() => setCursor(null)}>First page</button>}
        {page.data?.next_cursor && (
          <button className="secondary" onClick={() => setCursor(page.data?.next_cursor ?? null)}>Next page</button>
        )}
      </div>
    </section>
  );
}
