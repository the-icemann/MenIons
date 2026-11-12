"""The WhatsApp provider interface, and the message limits every provider must respect."""

from typing import Protocol, TypedDict

MAX_BUTTONS = 3
MAX_BUTTON_TITLE = 20
MAX_LIST_ROWS = 10
MAX_LIST_BUTTON_LABEL = 20
MAX_ROW_TITLE = 24
MAX_ROW_DESCRIPTION = 72
MAX_SECTION_TITLE = 24


class MessageRejected(ValueError):
    """The message breaks a WhatsApp limit. Raised before anything is sent."""


class Row(TypedDict, total=False):
    id: str
    title: str
    description: str


class Section(TypedDict):
    title: str
    rows: list[Row]


def to_wa_id(phone: str) -> str:
    """+256772123456 -> 256772123456, the form the Cloud API uses."""
    digits = phone.lstrip("+")
    if not digits.isdigit():
        raise MessageRejected("phone must be in E.164 format")
    return digits


def validate_buttons(buttons: list[tuple[str, str]]) -> None:
    if not 1 <= len(buttons) <= MAX_BUTTONS:
        raise MessageRejected(f"a message takes 1 to {MAX_BUTTONS} buttons, got {len(buttons)}")
    for button_id, title in buttons:
        if not button_id or not title:
            raise MessageRejected("every button needs an id and a title")
        if len(title) > MAX_BUTTON_TITLE:
            raise MessageRejected(f"button title over {MAX_BUTTON_TITLE} characters: {title!r}")
    if len({button_id for button_id, _ in buttons}) != len(buttons):
        raise MessageRejected("button ids must be different")


def validate_list(button_label: str, sections: list[Section]) -> None:
    if not button_label or len(button_label) > MAX_LIST_BUTTON_LABEL:
        raise MessageRejected(f"the list button label must be 1 to {MAX_LIST_BUTTON_LABEL} characters")
    rows = [row for section in sections for row in section["rows"]]
    if not 1 <= len(rows) <= MAX_LIST_ROWS:
        raise MessageRejected(f"a list takes 1 to {MAX_LIST_ROWS} rows in total, got {len(rows)}")
    for section in sections:
        if len(section.get("title", "")) > MAX_SECTION_TITLE:
            raise MessageRejected(f"section title over {MAX_SECTION_TITLE} characters: {section['title']!r}")
    for row in rows:
        if not row.get("id") or not row.get("title"):
            raise MessageRejected("every list row needs an id and a title")
        if len(row["title"]) > MAX_ROW_TITLE:
            raise MessageRejected(f"row title over {MAX_ROW_TITLE} characters: {row['title']!r}")
        if len(row.get("description", "")) > MAX_ROW_DESCRIPTION:
            raise MessageRejected(f"row description over {MAX_ROW_DESCRIPTION} characters")
    if len({row["id"] for row in rows}) != len(rows):
        raise MessageRejected("list row ids must be different")


class WhatsAppProvider(Protocol):
    """Every send returns the provider's message id."""

    def send_text(self, phone: str, text: str) -> str: ...

    def send_buttons(self, phone: str, body: str, buttons: list[tuple[str, str]]) -> str: ...

    def send_list(self, phone: str, body: str, button_label: str, sections: list[Section]) -> str: ...

    def send_location_request(self, phone: str, body: str) -> str: ...

    def send_flow(
        self, phone: str, flow_id: str, cta: str, flow_token: str, screen: str, body: str | None = None
    ) -> str: ...

    def send_template(self, phone: str, template_name: str, language: str, components: list[dict]) -> str: ...

    def download_media(self, media_id: str) -> bytes: ...
