// Operational metrics for reviewers and auditors (APX-05 §14). Rates arrive as decimal strings.
import { useState } from "react";
import { formatScore, stateLabel } from "../format";
import type { MetricsSummary } from "../types";
import { useData } from "../useData";

function isoDay(date: Date): string {
  return date.toISOString().slice(0, 10);
}

export default function Metrics() {
  const today = new Date();
  const monthAgo = new Date(today.getTime() - 30 * 24 * 3600 * 1000);
  const [from, setFrom] = useState(isoDay(monthAgo));
  const [to, setTo] = useState(isoDay(today));
  const metrics = useData<MetricsSummary>(`/api/v1/metrics/summary?from=${from}&to=${to}`);
  const m = metrics.data;

  return (
    <section className="card">
      <div className="row spread">
        <h2>Metrics (cases closed in the period)</h2>
        <div className="row">
          <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} aria-label="From" />
          <input type="date" value={to} onChange={(e) => setTo(e.target.value)} aria-label="To" />
        </div>
      </div>
      {metrics.error && <p className="error">{metrics.error}</p>}
      {m && (
        <>
          <dl className="facts">
            <dt>Closed</dt><dd>{m.closed}</dd>
            <dt>Decided automatically</dt><dd>{formatScore(m.automation_rate)}</dd>
            <dt>Fast path</dt><dd>{formatScore(m.fast_path_rate)}</dd>
            <dt>Got an offer</dt><dd>{formatScore(m.offer_rate)}</dd>
            <dt>Offers accepted</dt><dd>{formatScore(m.offer_acceptance_rate)}</dd>
            <dt>Went to a reviewer</dt><dd>{formatScore(m.human_review_rate)}</dd>
            <dt>Appeals</dt><dd>{m.appeal_count}</dd>
            <dt>Median days to close</dt><dd>{m.time_to_close_days_p50 === null ? "—" : m.time_to_close_days_p50.toFixed(1)}</dd>
          </dl>
          <h3>All disputes by status (now)</h3>
          <ul>
            {Object.entries(m.counts_by_state).map(([state, count]) => (
              <li key={state}>{stateLabel(state)}: {count}</li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
