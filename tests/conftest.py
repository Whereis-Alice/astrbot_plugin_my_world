"""Keep legacy naive timestamps on the same calendar as the test worlds."""
import os
import time

import pytest


@pytest.fixture(scope="session", autouse=True)
def test_world_timezone():
    # Some older scenarios use datetime.timestamp() directly. Give them the
    # same explicit calendar as StubClock rather than the CI machine's zone.
    # Tests for other world calendars continue to pass explicit aware dates.
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    if hasattr(time, "tzset"):
        time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        if hasattr(time, "tzset"):
            time.tzset()
