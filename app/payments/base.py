"""What the rest of the app needs from a mobile money gateway. pawaPay implements it; tests
use FakeGateway."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

MTN = "MTN_MOMO_UGA"
AIRTEL = "AIRTEL_OAPI_UGA"
PROVIDER_NAMES = {MTN: "MTN Mobile Money", AIRTEL: "Airtel Money"}


class GatewayError(Exception):
    """The gateway could not be reached or answered with an error we can't act on.
    The outcome of the call is unknown: check the status before trying again."""


@dataclass(frozen=True)
class Initiated:
    status: str  # ACCEPTED | REJECTED | DUPLICATE_IGNORED
    failure_code: str | None = None


@dataclass(frozen=True)
class Transaction:
    """A deposit or refund as the gateway sees it. found=False: the gateway never got it."""

    found: bool
    status: str | None = None  # ACCEPTED | PROCESSING | IN_RECONCILIATION | COMPLETED | FAILED
    amount: str | None = None
    currency: str | None = None
    provider: str | None = None
    provider_transaction_id: str | None = None
    failure_code: str | None = None


class Gateway(Protocol):
    def predict_provider(self, phone: str) -> str | None: ...

    def initiate_deposit(
        self, deposit_id: str, amount_ugx: int, phone: str, provider: str, message: str
    ) -> Initiated: ...

    def get_deposit(self, deposit_id: str) -> Transaction: ...

    def initiate_refund(self, refund_id: str, deposit_id: str, amount_ugx: int) -> Initiated: ...

    def get_refund(self, refund_id: str) -> Transaction: ...


def provider_from_prefix(phone: str) -> str | None:
    """+25677... -> MTN. Used only when the gateway can't tell us."""
    prefix = phone.removeprefix("+256")[:2]
    if prefix in ("76", "77", "78"):
        return MTN
    if prefix in ("70", "74", "75"):
        return AIRTEL
    return None
