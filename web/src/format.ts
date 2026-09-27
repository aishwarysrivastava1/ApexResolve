// Display helpers. Money stays an integer number of paise everywhere; these functions only
// turn it into text (and text back into paise) with integer arithmetic, never floating point.

const groupIndian = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });

// 123456789 paise -> "₹12,34,567.89" (Indian digit grouping)
export function formatMoney(minor: number, currency: string): string {
  const sign = minor < 0 ? "-" : "";
  const absolute = Math.abs(minor);
  const whole = Math.trunc(absolute / 100);
  const fraction = String(absolute % 100).padStart(2, "0");
  const symbol = currency === "INR" ? "₹" : `${currency} `;
  return `${sign}${symbol}${groupIndian.format(whole)}.${fraction}`;
}

// "4999" or "4999.5" or "4,999.50" -> 499950 paise. Returns null for anything else.
export function parseMoney(text: string): number | null {
  const cleaned = text.replace(/[,\s₹]/g, "");
  const match = /^(\d{1,9})(?:\.(\d{1,2}))?$/.exec(cleaned);
  if (match === null) {
    return null;
  }
  const whole = Number(match[1]);
  const fraction = Number((match[2] ?? "").padEnd(2, "0"));
  return whole * 100 + fraction;
}

const istDateTime = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata", day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
});

// All times are shown in India Standard Time with the date.
export function formatTime(iso: string | null): string {
  if (!iso) {
    return "—";
  }
  return `${istDateTime.format(new Date(iso))} IST`;
}

// Decimal strings from the server ("0.8550") shown as a percentage ("85.5%") without float maths on money.
export function formatScore(value: string | null): string {
  if (value === null) {
    return "—";
  }
  return `${(Number(value) * 100).toFixed(1)}%`;
}

export const REASONS: Record<string, string> = {
  C08: "I paid but it never arrived (goods or services not received)",
  C31: "What arrived is not what was described",
  C02: "The merchant promised a refund that never came",
  P08: "I was charged twice for the same thing",
};

export const STATE_LABELS: Record<string, string> = {
  REJECTED_INELIGIBLE: "Not eligible",
  AWAITING_MERCHANT: "Waiting for the merchant",
  AWAITING_CARDMEMBER_REBUTTAL: "Waiting for the cardmember's reply",
  READY_FOR_DECISION: "Being decided",
  SETTLEMENT_OFFERED: "Settlement offered",
  HUMAN_REVIEW: "With a reviewer",
  DECIDED: "Decided",
  SETTLEMENT_PENDING: "Refund being sent",
  CLOSED: "Closed",
};

export const EVENT_LABELS: Record<string, string> = {
  DISPUTE_CREATED: "Dispute filed",
  EVIDENCE_ADDED: "Evidence added",
  STATE_CHANGED: "Status changed",
  DECISION_RECORDED: "Decision recorded",
  OFFER_CREATED: "Settlement offered",
  OFFER_RESPONDED: "Offer answered",
  APPEAL_FILED: "Appeal filed",
  TRANSFER_QUEUED: "Refund queued",
  TRANSFER_CONFIRMED: "Refund sent",
};

// "carrier_delivery_confirmation" -> "Carrier delivery confirmation"
export function humanize(code: string): string {
  const words = code.toLowerCase().replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function stateLabel(state: string): string {
  return STATE_LABELS[state] ?? humanize(state);
}
