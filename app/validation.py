"""Per-row recipient validation.

Rows are validated one by one (not by the request schema) so one bad row is
recorded as a failed certificate instead of rejecting the whole batch.
"""
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")
MAX_EMAIL_LENGTH = 254


@dataclass
class CleanRecipient:
    name: str
    email: str


class RowError(Exception):
    pass


def _clean_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise RowError(f"'{field}' must be a string")
    value = unicodedata.normalize("NFC", value)
    # Control / format characters (newlines, NUL, RTL overrides...) are never legitimate in a name.
    if any(unicodedata.category(c) in ("Cc", "Cf") for c in value):
        raise RowError(f"'{field}' contains control or invisible characters")
    value = " ".join(value.split())  # trim + collapse whitespace
    if not value:
        raise RowError(f"'{field}' is required")
    return value


def validate_row(row: Any, max_name_length: int) -> CleanRecipient:
    if not isinstance(row, dict):
        raise RowError("recipient must be an object with 'name' and 'email'")
    if "name" not in row:
        raise RowError("'name' is required")
    if "email" not in row:
        raise RowError("'email' is required")

    name = _clean_text(row["name"], "name")
    if len(name) > max_name_length:
        raise RowError(f"'name' exceeds {max_name_length} characters")
    if not any(c.isalpha() for c in name):
        raise RowError("'name' must contain at least one letter")
    try:
        name.encode("cp1252")  # the built-in PDF font is WinAnsi; anything else would render as garbage
    except UnicodeEncodeError:
        raise RowError("'name' contains characters the certificate font cannot render")

    email = _clean_text(row["email"], "email").lower()
    if len(email) > MAX_EMAIL_LENGTH or not EMAIL_RE.match(email):
        raise RowError("'email' is not a valid email address")

    return CleanRecipient(name=name, email=email)
