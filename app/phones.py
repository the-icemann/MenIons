"""Phone numbers. Stored everywhere in E.164: +2567XXXXXXXX."""

import re

_UG_MOBILE = re.compile(r"^(?:\+?256|0)(7\d{8})$")


class InvalidPhone(ValueError):
    pass


def normalize_phone(raw: str) -> str:
    """Accept 07XXXXXXXX, 2567XXXXXXXX or +2567XXXXXXXX (spaces and dashes allowed).

    Returns +2567XXXXXXXX. Anything else raises InvalidPhone: a phone number is never guessed.
    """
    if not isinstance(raw, str):
        raise InvalidPhone("phone must be text")
    cleaned = re.sub(r"[\s\-()]", "", raw)
    match = _UG_MOBILE.match(cleaned)
    if not match:
        raise InvalidPhone(f"not a Ugandan mobile number: {mask_phone(raw)}")
    return "+256" + match.group(1)


def normalize_wa_id(raw: str) -> str:
    """Normalise a sender id from a WhatsApp webhook (digits, no '+').

    Ugandan numbers go through normalize_phone. A foreign number (for example a customer
    with a Kenyan line) is kept as +<digits> so the message is still stored and routed.
    """
    try:
        return normalize_phone(raw)
    except InvalidPhone:
        digits = re.sub(r"\D", "", raw or "")
        if not 7 <= len(digits) <= 15:
            raise
        return "+" + digits


def mask_phone(phone: str) -> str:
    """For logs: keep the first 6 and last 3 characters."""
    phone = str(phone)
    if len(phone) <= 9:
        return "***"
    return phone[:6] + "*" * (len(phone) - 9) + phone[-3:]
