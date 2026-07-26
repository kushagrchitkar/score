"""Terminal title control using standard OSC sequences supported by Ghostty."""

import re


def osc_title(title: str) -> str:
    safe = re.sub(r"[\x00-\x1f\x7f]", "", title)
    return f"\x1b]0;{safe}\x07"
