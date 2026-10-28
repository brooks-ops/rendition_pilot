"""Reject identifiers and form instructions before considering valuation amounts."""
import re


def non_value_line(line: str) -> bool:
    return bool(re.search(
        r'\b(?:account\s*(?:number|no\.?|#)|parcel|phone|telephone|fax|zip\s*code|'
        r'mailing\s*address|property\s*address|business\s*address|tax\s*code|'
        r'not\s*more\s*than|or\s*less|or\s*more)\b'
        r'|\b\d{1,6}\s+(?:[A-Z0-9]+\s+){0,5}(?:STREET|ST|AVENUE|AVE|ROAD|RD|DRIVE|DR|BOULEVARD|BLVD|LANE|LN|HIGHWAY|HWY)\b'
        r'|\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b'
        r'|\b\d{3}[-.)\s]+\d{3}[-.\s]+\d{4}\b',
        line, re.IGNORECASE))


def non_value_at(text: str, start: int, end: int) -> bool:
    line = text[text.rfind('\n', 0, start)+1:text.find('\n', end) if '\n' in text[end:] else len(text)]
    if non_value_line(line):
        return True
    # Do not pull digits out of an account/serial identifier such as P341728.
    return start > 0 and text[start-1].isalnum()
