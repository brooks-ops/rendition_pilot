"""Password rules for creating or changing application credentials."""
import re
from fastapi import HTTPException

MESSAGE = 'Use at least 8 letters, 1 number, and 1 special character in your password.'


def validate_password(password):
    if (len(re.findall(r'[A-Za-z]', password)) < 8
            or not re.search(r'[0-9]', password)
            or not any(not c.isalnum() and not c.isspace() for c in password)):
        raise HTTPException(400, MESSAGE)
    return password
