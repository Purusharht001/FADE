"""Validation of research subject identifiers."""

import re

# Shape of a research subject identifier such as "OAS1_0001_MR1". It rules
# out spaces, commas and other punctuation, which is how a name, a date of
# birth or a hospital number usually looks. It cannot prove an identifier
# is not PHI; whoever assigns identifiers remains responsible for that.
EXTERNAL_SUBJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


def validate_external_subject_id(value: str | None) -> str | None:
    """Returns the identifier stripped, None if it is blank, or raises
    ValueError. The message never repeats the rejected value: if it was
    rejected for looking like a name, it should not be echoed or logged.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not EXTERNAL_SUBJECT_ID_PATTERN.fullmatch(value):
        raise ValueError(
            "external_subject_id must be 1-64 characters of letters, digits, '_', '.' or '-', "
            "starting with a letter or digit. Use a research code, never a name or hospital "
            "number."
        )
    return value
