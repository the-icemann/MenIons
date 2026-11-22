"""Getting URA's Fiscal Document Number (FDN) for each plan sale, and a credit note after a
refund.

The sale itself never waits for EFRIS: the plan starts as soon as the money is in, and a
FiscalDocument row records that we owe URA a document. URA allows 24 hours between the sale
and the upload (the rule for when EFRIS is unavailable), so the worker retries, and admins
are alerted after EFRIS_ALERT_HOURS.

Providers:
  manual  An admin issues the document on URA's portal (or EFRIS app) and sends
          /fdn REF FDN VERIFICATION-CODE. The default until a direct connection exists.
  fake    Made-up FDNs for development. Refused in production.

TODO(efris-api): add a provider for URA's system-to-system API or an accredited integrator
once the access and the interface document are in hand. It implements issue() below and
nothing else changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.config import get_settings


class EfrisUnavailable(Exception):
    """EFRIS (or the integrator) could not be reached or refused for now: try again later."""


@dataclass(frozen=True)
class FiscalRequest:
    reference: str  # our reference, unique per document
    kind: str  # invoice | credit_note
    amount_ugx: int
    item: str  # e.g. "Delivery plan: Weekly Gold (20 drops)"
    buyer_name: str
    buyer_phone: str
    buyer_tin: str | None  # None: an ordinary buyer without a TIN (B2C)
    original_fdn: str | None = None  # for a credit note: the FDN it reverses


@dataclass(frozen=True)
class FiscalResult:
    fdn: str
    verification_code: str
    qr_code: str | None = None


class EfrisProvider(Protocol):
    automatic: bool

    def issue(self, request: FiscalRequest) -> FiscalResult: ...


class ManualEfris:
    """Nothing automatic: the admins are asked to issue it by hand."""

    automatic = False

    def issue(self, request: FiscalRequest) -> FiscalResult:  # pragma: no cover - never called
        raise EfrisUnavailable("EFRIS_PROVIDER=manual: an admin issues this document")


class FakeEfris:
    automatic = True

    def issue(self, request: FiscalRequest) -> FiscalResult:
        return FiscalResult(fdn=f"TEST-{request.reference}", verification_code="TESTCODE")


_provider: EfrisProvider | None = None


def get_provider() -> EfrisProvider:
    global _provider
    if _provider is None:
        _provider = FakeEfris() if get_settings().efris_provider == "fake" else ManualEfris()
    return _provider


def set_provider(provider: EfrisProvider | None) -> None:
    global _provider
    _provider = provider
