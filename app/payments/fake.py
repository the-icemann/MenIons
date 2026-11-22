"""An in-memory gateway for tests. Deposits start ACCEPTED; a test then settles them."""

from __future__ import annotations

from app.payments.base import GatewayError, Initiated, Transaction, provider_from_prefix


class FakeGateway:
    def __init__(self):
        self.deposits: dict[str, dict] = {}
        self.refunds: dict[str, dict] = {}
        self.reject_with: str | None = None  # next initiate is REJECTED with this code
        self.down = False  # every call raises GatewayError

    def _check(self):
        if self.down:
            raise GatewayError("fake gateway is down")

    def predict_provider(self, phone: str) -> str | None:
        self._check()
        return provider_from_prefix(phone)

    def initiate_deposit(self, deposit_id, amount_ugx, phone, provider, message) -> Initiated:
        self._check()
        if deposit_id in self.deposits:
            return Initiated("DUPLICATE_IGNORED")
        if self.reject_with:
            code, self.reject_with = self.reject_with, None
            return Initiated("REJECTED", code)
        self.deposits[deposit_id] = {
            "status": "ACCEPTED", "amount": str(amount_ugx), "phone": phone, "provider": provider,
            "message": message, "failure_code": None, "txn": None,
        }  # fmt: skip
        return Initiated("ACCEPTED")

    def complete(self, deposit_id: str, *, amount: str | None = None, txn: str = "MP2611.1234.A00001") -> None:
        d = self.deposits[deposit_id]
        d.update(status="COMPLETED", txn=txn)
        if amount is not None:
            d["amount"] = amount

    def fail(self, deposit_id: str, code: str = "INSUFFICIENT_BALANCE") -> None:
        self.deposits[deposit_id].update(status="FAILED", failure_code=code)

    def _tx(self, d: dict | None) -> Transaction:
        if d is None:
            return Transaction(found=False)
        return Transaction(
            found=True, status=d["status"], amount=d["amount"], currency="UGX", provider=d.get("provider"),
            provider_transaction_id=d["txn"], failure_code=d["failure_code"],
        )  # fmt: skip

    def get_deposit(self, deposit_id: str) -> Transaction:
        self._check()
        return self._tx(self.deposits.get(deposit_id))

    def initiate_refund(self, refund_id, deposit_id, amount_ugx) -> Initiated:
        self._check()
        if refund_id in self.refunds:
            return Initiated("DUPLICATE_IGNORED")
        self.refunds[refund_id] = {
            "status": "ACCEPTED", "amount": str(amount_ugx), "deposit_id": deposit_id,
            "failure_code": None, "txn": None,
        }  # fmt: skip
        return Initiated("ACCEPTED")

    def complete_refund(self, refund_id: str) -> None:
        self.refunds[refund_id].update(status="COMPLETED", txn="RF.0001")

    def get_refund(self, refund_id: str) -> Transaction:
        self._check()
        return self._tx(self.refunds.get(refund_id))
