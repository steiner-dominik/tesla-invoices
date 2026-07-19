"""Keep personal data out of log lines.

Local invoice file names embed the full VIN (needed for stable, per-vehicle
file names), but logs may be pasted into public bug reports — so every log
line that mentions a local file name or path must pass it through
``redact_vin()`` first. The last four characters are kept so log lines stay
correlatable with a specific vehicle, matching the "VIN ending in XXXX"
labels used elsewhere in the logs.
"""

import re

# A VIN is exactly 17 characters from [A-HJ-NPR-Z0-9] (I, O and Q are never
# used). The lookarounds keep the match anchored to a full 17-character run
# (underscores around the VIN in file names are not word boundaries, so \b
# would not work here).
_VIN_RE = re.compile(r"(?<![A-HJ-NPR-Z0-9])[A-HJ-NPR-Z0-9]{13}([A-HJ-NPR-Z0-9]{4})(?![A-HJ-NPR-Z0-9])")


def redact_vin(text: object) -> str:
    """Replace every full VIN in ``text`` with ``…<last 4 characters>``."""
    return _VIN_RE.sub(r"…\1", str(text))
