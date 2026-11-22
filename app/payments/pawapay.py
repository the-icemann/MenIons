"""pawaPay Merchant API v2 (https://docs.pawapay.io/v2).

Confirmed from the docs (22 Nov 2026): base URLs, Bearer auth, POST /v2/deposits with
depositId / amount (string) / currency / payer.accountDetails, the provider codes for Uganda,
GET /v2/deposits/{id} returning {"status": "FOUND" | "NOT_FOUND", "data": {...}},
POST /v2/predict-provider, and DUPLICATE_IGNORED for a repeated depositId.

TODO(pawapay-docs): the refund request body and the refund status response were not
confirmed. They are assumed to mirror deposits (refundId, depositId, amount, currency;
GET /v2/refunds/{id}). Check them in the sandbox before the first real refund.
"""

from __future__ import annotations

import logging

import httpx

from app.payments.base import GatewayError, Initiated, Transaction
from app.phones import mask_phone

log = logging.getLogger(__name__)
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class PawaPay:
    def __init__(self, base_url: str, token: str, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=TIMEOUT,
        )

    def _call(self, method: str, path: str, json: dict | None = None) -> dict:
        try:
            response = self._client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise GatewayError(f"pawaPay unreachable: {exc!r}") from exc
        if response.status_code >= 500:
            raise GatewayError(f"pawaPay {response.status_code} on {path}")
        try:
            body = response.json()
        except ValueError:
            raise GatewayError(f"pawaPay sent no JSON ({response.status_code}) on {path}") from None
        if response.status_code >= 400:
            log.warning("pawaPay %s on %s: %s", response.status_code, path, str(body)[:300])
        return body

    @staticmethod
    def _msisdn(phone: str) -> str:
        return phone.lstrip("+")  # pawaPay wants 2567XXXXXXXX

    @staticmethod
    def _failure(body: dict) -> str | None:
        reason = body.get("failureReason") or {}
        return reason.get("failureCode") if isinstance(reason, dict) else None

    def predict_provider(self, phone: str) -> str | None:
        try:
            body = self._call("POST", "/v2/predict-provider", {"phoneNumber": self._msisdn(phone)})
        except GatewayError:
            log.warning("predict-provider failed for %s", mask_phone(phone))
            return None
        return body.get("provider") if body.get("country") in (None, "UGA") else None

    def initiate_deposit(self, deposit_id: str, amount_ugx: int, phone: str, provider: str, message: str) -> Initiated:
        body = self._call(
            "POST",
            "/v2/deposits",
            {
                "depositId": deposit_id,
                "amount": str(amount_ugx),  # whole shillings: Airtel Uganda takes no decimals
                "currency": "UGX",
                "payer": {"type": "MMO", "accountDetails": {"phoneNumber": self._msisdn(phone), "provider": provider}},
                "customerMessage": message,
            },
        )
        status = body.get("status")
        if status not in ("ACCEPTED", "REJECTED", "DUPLICATE_IGNORED"):
            raise GatewayError(f"unexpected deposit response: {str(body)[:300]}")
        return Initiated(status, self._failure(body))

    def _transaction(self, path: str) -> Transaction:
        body = self._call("GET", path)
        if body.get("status") == "NOT_FOUND":
            return Transaction(found=False)
        data = body.get("data")
        if body.get("status") != "FOUND" or not isinstance(data, dict):
            raise GatewayError(f"unexpected status response: {str(body)[:300]}")
        payer = data.get("payer") or data.get("recipient") or {}
        account = payer.get("accountDetails") or {} if isinstance(payer, dict) else {}
        return Transaction(
            found=True,
            status=data.get("status"),
            amount=data.get("amount"),
            currency=data.get("currency"),
            provider=account.get("provider"),
            provider_transaction_id=data.get("providerTransactionId"),
            failure_code=self._failure(data),
        )

    def get_deposit(self, deposit_id: str) -> Transaction:
        return self._transaction(f"/v2/deposits/{deposit_id}")

    def initiate_refund(self, refund_id: str, deposit_id: str, amount_ugx: int) -> Initiated:
        body = self._call(
            "POST",
            "/v2/refunds",
            {"refundId": refund_id, "depositId": deposit_id, "amount": str(amount_ugx), "currency": "UGX"},
        )
        status = body.get("status")
        if status not in ("ACCEPTED", "REJECTED", "DUPLICATE_IGNORED"):
            raise GatewayError(f"unexpected refund response: {str(body)[:300]}")
        return Initiated(status, self._failure(body))

    def get_refund(self, refund_id: str) -> Transaction:
        return self._transaction(f"/v2/refunds/{refund_id}")
