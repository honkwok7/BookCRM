"""Validators shared by several apps."""

import zoneinfo

from django.core.exceptions import ValidationError


def validate_timezone(value: str) -> None:
    if value not in zoneinfo.available_timezones():
        raise ValidationError(f"“{value}” is not a known time zone.", code="invalid_timezone")
