"""CoreGateway: Amex core records and posting. Prototype = MockCore over the core_* tables.

Production replaces MockCore with an adapter that implements the same async methods.
"""
import uuid
from datetime import timedelta
from sqlalchemy import text


class MockCore:
    """Stand-in for Amex core records and the posting system (tables core_*)."""

    async def get_transaction(self, session, transaction_id):
        row = (await session.execute(text(
            "SELECT t.*, a.cardmember_ref, a.status AS account_status, a.shipping_postcode, a.card_display_mask, "
            "s.merchant_id, m.name AS merchant_name, m.return_postcode "
            "FROM core_transactions t "
            "JOIN core_accounts a ON a.account_token = t.account_token "
            "JOIN merchant_se_numbers s ON s.se_number = t.se_number "
            "JOIN merchants m ON m.merchant_id = s.merchant_id "
            "WHERE t.transaction_id = :t"), {"t": transaction_id})).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def list_similar_charges(self, session, txn, window_hours):
        window = timedelta(hours=window_hours)
        rows = (await session.execute(text(
            "SELECT transaction_id, account_token, se_number, amount_minor, currency, transaction_at, order_ref "
            "FROM core_transactions WHERE kind = 'CHARGE' AND account_token = :a AND se_number = :s "
            "AND amount_minor = :amt AND currency = :c AND transaction_id <> :t "
            "AND transaction_at BETWEEN :lo AND :hi"),
            {"a": txn["account_token"], "s": txn["se_number"], "amt": txn["amount_minor"], "c": txn["currency"],
             "t": txn["transaction_id"], "lo": txn["transaction_at"] - window,
             "hi": txn["transaction_at"] + window})).mappings().all()
        return [dict(r) for r in rows]

    async def list_credits(self, session, transaction_ids):
        rows = (await session.execute(text(
            "SELECT transaction_id, original_transaction_id, amount_minor FROM core_transactions "
            "WHERE kind = 'CREDIT' AND original_transaction_id = ANY(:ids)"),
            {"ids": list(transaction_ids)})).mappings().all()
        return [dict(r) for r in rows]

    async def transfer(self, session, idempotency_key, dispute_id, se_number, account_token, amount_minor, currency):
        """Move money merchant -> cardmember exactly once per idempotency key (double entry)."""
        transfer_id = uuid.uuid4()
        inserted = (await session.execute(text(
            "INSERT INTO core_transfers (transfer_id, idempotency_key, dispute_id, amount_minor, currency) "
            "VALUES (:t, :k, :d, :a, :c) ON CONFLICT (idempotency_key) DO NOTHING RETURNING transfer_id"),
            {"t": transfer_id, "k": idempotency_key, "d": dispute_id, "a": amount_minor, "c": currency})).first()
        if inserted is None:
            # already posted earlier: return the existing transfer, post nothing again
            existing = (await session.execute(text(
                "SELECT transfer_id FROM core_transfers WHERE idempotency_key = :k"), {"k": idempotency_key})).one()
            return existing.transfer_id
        await session.execute(text(
            "INSERT INTO core_ledger_lines (transfer_id, account_ref, direction, amount_minor) VALUES "
            "(:t, :se, 'DEBIT', :a), (:t, :card, 'CREDIT', :a)"),
            {"t": transfer_id, "se": f"SE:{se_number}", "card": f"CARD:{account_token}", "a": amount_minor})
        return transfer_id
