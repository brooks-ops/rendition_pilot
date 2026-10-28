"""Quarterly year-to-date coverage in the district's local calendar."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TIMEZONE = 'America/Chicago'
RUN_MONTHS = (1, 4, 7, 10)
SCHEDULE_LABEL = 'January 1, April 1, July 1, October 1'


def local_date(now=None):
    now = now or datetime.now(ZoneInfo(TIMEZONE))
    if isinstance(now, datetime):
        if now.tzinfo is None:
            raise ValueError('Scheduled collection requires a timezone-aware timestamp')
        return now.astimezone(ZoneInfo(TIMEZONE)).date()
    return now


def window_for(scheduled_for):
    """January finalizes the prior year; all other runs start January 1."""
    if isinstance(scheduled_for, str):
        scheduled_for = date.fromisoformat(scheduled_for)
    if scheduled_for.day != 1 or scheduled_for.month not in RUN_MONTHS:
        raise ValueError('Choose a quarterly run date: January, April, July or October 1')
    end = scheduled_for - timedelta(days=1)
    return {'scheduled_for': scheduled_for.isoformat(),
            'start': date(end.year, 1, 1).isoformat(), 'end': end.isoformat(),
            'coverage_year': end.year, 'cadence': 'quarterly', 'coverage': 'year_to_date'}


def schedule(now=None):
    today = local_date(now)
    month = ((today.month - 1) // 3) * 3 + 1
    due = date(today.year, month, 1)
    upcoming = date(today.year + 1, 1, 1) if month == 10 else date(today.year, month + 3, 1)
    return {'cadence': 'quarterly', 'coverage': 'year_to_date', 'timezone': TIMEZONE,
            'scheduled_dates': SCHEDULE_LABEL, 'pending_window': window_for(due),
            'next_run': window_for(upcoming),
            'october_readiness_window': window_for(date(today.year, 10, 1)),
            'purpose': 'Prepare January–September filings for November/December appraisal work.'}
