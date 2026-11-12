"""One client for any WhatsApp Cloud API-compatible endpoint (Kapso's Meta proxy, or Meta).

Only settings differ between providers: the base URL and the auth header.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.whatsapp.base import MessageRejected, Section, to_wa_id, validate_buttons, validate_list

log = logging.getLogger(__name__)

RETRY_DELAYS = (0.5, 2.0)  # seconds before the 2nd and 3rd attempt


class WhatsAppError(Exception):
    def __init__(self, status: int | None, code: Any, message: str):
        super().__init__(f"WhatsApp API error (HTTP {status}, code {code}): {message}")
        self.status = status
        self.code = code


class CloudApiProvider:
    def __init__(
        self,
        base_url: str,
        phone_number_id: str,
        auth_headers: dict[str, str],
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.phone_number_id = phone_number_id
        self.auth_headers = auth_headers
        self.client = client or httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0))
        self._sleep = sleep

    # --- transport ----------------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Retry on 429, 5xx and failures to connect, with backoff. Never retry other 4xx.

        A timeout after the request was sent is not retried: the message may have gone out,
        and sending it again could deliver it twice.
        """
        last: Exception | None = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            if attempt:
                self._sleep(RETRY_DELAYS[attempt - 1])
            try:
                response = self.client.request(method, url, headers=self.auth_headers, **kwargs)
            except httpx.ConnectError as exc:
                last = WhatsAppError(None, "connect_error", str(exc))
                log.warning("whatsapp connect error (attempt %d)", attempt + 1)
                continue
            if response.status_code < 400:
                return response
            last = self._error(response)
            log.warning("whatsapp api error: http=%s code=%s message=%s", response.status_code, last.code, last)
            if response.status_code != 429 and response.status_code < 500:
                raise last
        assert last is not None
        raise last

    @staticmethod
    def _error(response: httpx.Response) -> WhatsAppError:
        code: Any = None
        message = response.text[:300]
        try:
            error = response.json().get("error") or {}
            if isinstance(error, dict):
                code = error.get("code")
                message = error.get("message") or message
            else:
                message = str(error)
        except (ValueError, AttributeError):
            pass
        return WhatsAppError(response.status_code, code, message)

    def _send(self, phone: str, message: dict[str, Any]) -> str:
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to_wa_id(phone), **message}
        response = self._request("POST", f"{self.base_url}/{self.phone_number_id}/messages", json=body)
        try:
            return response.json()["messages"][0]["id"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise WhatsAppError(response.status_code, "bad_response", "no message id in the response") from None

    # --- messages -------------------------------------------------------------------

    def send_text(self, phone: str, text: str) -> str:
        if not text:
            raise MessageRejected("text is empty")
        return self._send(phone, {"type": "text", "text": {"preview_url": False, "body": text}})

    def send_buttons(self, phone: str, body: str, buttons: list[tuple[str, str]]) -> str:
        validate_buttons(buttons)
        action = {"buttons": [{"type": "reply", "reply": {"id": i, "title": t}} for i, t in buttons]}
        return self._send(
            phone, {"type": "interactive", "interactive": {"type": "button", "body": {"text": body}, "action": action}}
        )

    def send_list(self, phone: str, body: str, button_label: str, sections: list[Section]) -> str:
        validate_list(button_label, sections)
        action = {"button": button_label, "sections": sections}
        return self._send(
            phone, {"type": "interactive", "interactive": {"type": "list", "body": {"text": body}, "action": action}}
        )

    def send_location_request(self, phone: str, body: str) -> str:
        interactive = {"type": "location_request_message", "body": {"text": body}, "action": {"name": "send_location"}}
        return self._send(phone, {"type": "interactive", "interactive": interactive})

    def send_flow(
        self, phone: str, flow_id: str, cta: str, flow_token: str, screen: str, body: str | None = None
    ) -> str:
        parameters = {
            "flow_message_version": "3",
            "flow_token": flow_token,
            "flow_id": flow_id,
            "flow_cta": cta,
            "flow_action": "navigate",
            "flow_action_payload": {"screen": screen},
        }
        interactive = {
            "type": "flow",
            "body": {"text": body or cta},  # the Cloud API requires body text on a Flow message
            "action": {"name": "flow", "parameters": parameters},
        }
        return self._send(phone, {"type": "interactive", "interactive": interactive})

    def send_template(self, phone: str, template_name: str, language: str, components: list[dict]) -> str:
        template = {"name": template_name, "language": {"code": language}, "components": components}
        return self._send(phone, {"type": "template", "template": template})

    def download_media(self, media_id: str) -> bytes:
        """Cloud API media is fetched in two steps: look up the URL, then download it.

        TODO(kapso-docs): confirm that Kapso's proxy serves GET /{media_id} and the returned
        URL with the same X-API-Key header.
        """
        meta = self._request("GET", f"{self.base_url}/{media_id}").json()
        return self._request("GET", meta["url"]).content
