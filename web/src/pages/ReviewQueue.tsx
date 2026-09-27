// Reviewer queue: cases in HUMAN_REVIEW, oldest first, with why they are here.
import { formatMoney, formatScore, formatTime, humanize } from "../format";
import type { ListPage, ReviewItem } from "../types";
import { useData } from "../useData";

export default function ReviewQueue({ onOpen }: { onOpen: (disputeId: string) => void }) {
  const queue = useData<ListPage<ReviewItem>>("/api/v1/review/queue");
  return (
    <section className="card">
      <div className="row spread">
        <h2>Review queue</h2>
        <button className="secondary" onClick={queue.reload}>Refresh</button>
      </div>
      {queue.error && <p className="error">{queue.error}</p>}
      {queue.data?.items.length === 0 && <p className="hint">No cases are waiting for a reviewer.</p>}
      {(queue.data?.items.length ?? 0) > 0 && (
        <table>
          <thead>
            <tr><th>Waiting since</th><th>Reason</th><th>Amount</th><th>Why a person</th><th>Margin</th></tr>
          </thead>
          <tbody>
            {queue.data?.items.map((item) => (
              <tr key={item.dispute_id} className="clickable" onClick={() => onOpen(item.dispute_id)}>
                <td>{formatTime(item.waiting_since)}</td>
                <td>{item.reason_code}</td>
                <td>{formatMoney(item.disputed_amount_minor, item.currency)}</td>
                <td>{item.status_reason ? humanize(item.status_reason) : "—"}</td>
                <td>{formatScore(item.margin)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
