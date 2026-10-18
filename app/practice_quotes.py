"""Fetch-time checks for synthetic practice, not exchange quote certification."""
from datetime import datetime
import math


def fresh_practice_price(stock, now=None):
    """Naive stamps are local app time; aware stamps retain their UTC offset."""
    try:
        if isinstance(stock.price, bool) or not math.isfinite(stock.price) or stock.price <= 0:
            return False
        fetched = datetime.fromisoformat(stock.stamp)
        current = now if now is not None else datetime.now().astimezone()
        fetched = fetched.astimezone()
        current = current.astimezone()
        age = (current - fetched).total_seconds()
        return 0 <= age <= 180
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False
