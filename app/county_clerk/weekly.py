"""Compatibility entry point; County Clerk scheduling is now quarterly YTD."""
from .quarterly import main, check_quarterly

# Keep old operational invocations functional without retaining a weekly schedule.
check_weekly = check_quarterly

if __name__ == '__main__':
    raise SystemExit(main())
