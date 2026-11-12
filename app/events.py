"""Normalized inbound events. Everything past the adapter works with these, never with a
provider's raw payload."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, kw_only=True)
class InboundEvent:
    provider_message_id: str | None
    received_at: datetime


@dataclass(frozen=True, kw_only=True)
class TextMessage(InboundEvent):
    phone: str
    text: str


@dataclass(frozen=True, kw_only=True)
class ButtonReply(InboundEvent):
    phone: str
    button_id: str
    title: str


@dataclass(frozen=True, kw_only=True)
class ListReply(InboundEvent):
    phone: str
    row_id: str
    title: str


@dataclass(frozen=True, kw_only=True)
class FlowSubmit(InboundEvent):
    phone: str
    flow_token: str | None
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class LocationShared(InboundEvent):
    phone: str
    lat: float
    lng: float
    name: str | None = None
    address: str | None = None


@dataclass(frozen=True, kw_only=True)
class MediaReceived(InboundEvent):
    phone: str
    kind: str  # image | video | audio | document | sticker
    media_id: str
    caption: str | None = None


@dataclass(frozen=True, kw_only=True)
class StatusUpdate(InboundEvent):
    """Delivery status of a message we sent. provider_message_id equals message_id."""

    message_id: str
    status: str  # sent | delivered | read | failed
    error: dict[str, Any] | None = None


@dataclass(frozen=True, kw_only=True)
class UnknownEvent(InboundEvent):
    raw: Any
