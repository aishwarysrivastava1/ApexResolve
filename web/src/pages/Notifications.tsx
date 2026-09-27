// The bell in the header: polls unread notifications every 15 seconds while the app is open.
import { useEffect, useState } from "react";
import { postJson } from "../api";
import { formatTime } from "../format";
import type { ListPage, Notification } from "../types";
import { useData } from "../useData";

const POLL_MS = 15000;

export default function Notifications({ onOpen }: { onOpen: (disputeId: string) => void }) {
  const unread = useData<ListPage<Notification>>("/api/v1/notifications?unread=true");
  const [open, setOpen] = useState(false);
  const { reload } = unread;

  useEffect(() => {
    // polling instead of WebSockets: status changes take hours, 15 s is plenty
    const timer = window.setInterval(reload, POLL_MS);
    return () => window.clearInterval(timer);
  }, [reload]);

  const items = unread.data?.items ?? [];
  const openOne = async (item: Notification) => {
    await postJson(`/api/v1/notifications/${item.notification_id}/read`);
    reload();
    setOpen(false);
    if (item.dispute_id) {
      onOpen(item.dispute_id);
    }
  };

  return (
    <div className="bell">
      <button className="secondary" onClick={() => setOpen(!open)} aria-expanded={open}>
        Notifications{items.length > 0 ? ` (${items.length})` : ""}
      </button>
      {open && (
        <ul className="dropdown">
          {items.length === 0 && <li className="hint">No new notifications.</li>}
          {items.map((item) => (
            <li key={item.notification_id}>
              <button className="link" onClick={() => openOne(item)}>{item.message}</button>
              <span className="hint"> {formatTime(item.created_at)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
