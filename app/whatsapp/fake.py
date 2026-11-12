"""A test double: records every call, sends nothing, applies the same limits as the real client."""

from dataclasses import dataclass, field
from typing import Any

from app.whatsapp.base import MessageRejected, Section, to_wa_id, validate_buttons, validate_list


@dataclass
class SentMessage:
    id: str
    method: str
    phone: str
    payload: dict[str, Any]


@dataclass
class FakeProvider:
    sent: list[SentMessage] = field(default_factory=list)
    media: dict[str, bytes] = field(default_factory=dict)

    def _record(self, method: str, phone: str, **payload: Any) -> str:
        to_wa_id(phone)
        message = SentMessage(f"wamid.FAKE{len(self.sent) + 1:06d}", method, phone, payload)
        self.sent.append(message)
        return message.id

    def send_text(self, phone: str, text: str) -> str:
        if not text:
            raise MessageRejected("text is empty")
        return self._record("send_text", phone, text=text)

    def send_buttons(self, phone: str, body: str, buttons: list[tuple[str, str]]) -> str:
        validate_buttons(buttons)
        return self._record("send_buttons", phone, body=body, buttons=buttons)

    def send_list(self, phone: str, body: str, button_label: str, sections: list[Section]) -> str:
        validate_list(button_label, sections)
        return self._record("send_list", phone, body=body, button_label=button_label, sections=sections)

    def send_location_request(self, phone: str, body: str) -> str:
        return self._record("send_location_request", phone, body=body)

    def send_flow(
        self, phone: str, flow_id: str, cta: str, flow_token: str, screen: str, body: str | None = None
    ) -> str:
        return self._record(
            "send_flow", phone, flow_id=flow_id, cta=cta, flow_token=flow_token, screen=screen, body=body or cta
        )

    def send_template(self, phone: str, template_name: str, language: str, components: list[dict]) -> str:
        return self._record(
            "send_template", phone, template_name=template_name, language=language, components=components
        )

    def download_media(self, media_id: str) -> bytes:
        return self.media[media_id]
