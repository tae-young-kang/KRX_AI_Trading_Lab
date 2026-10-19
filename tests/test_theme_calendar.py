from datetime import datetime,timezone
import pytest
from app.theme_calendar import request_selection


def test_request_day_weekend_holiday_and_rolling_window():
    monday=request_selection('REAL_QUOTES',now=datetime(2026,10,19,18,tzinfo=timezone.utc))
    assert monday['through']=='2026-10-19'  # Tuesday in Korea, but before KRX opens.
    opened=request_selection('REAL_QUOTES',now=datetime(2026,10,20,1,tzinfo=timezone.utc))
    assert opened['calendar']['days']==['2026-10-12','2026-10-13','2026-10-14','2026-10-15','2026-10-16','2026-10-19','2026-10-20']
    holiday=request_selection('REAL_QUOTES',now=datetime(2026,10,9,9,tzinfo=timezone.utc))
    assert holiday['through']=='2026-10-08'
    weekend=request_selection('REAL_QUOTES',now=datetime(2026,10,18,9,tzinfo=timezone.utc))
    assert weekend['through']=='2026-10-16'
    with pytest.raises(ValueError):request_selection('REAL_QUOTES',now=datetime(2026,10,19))
