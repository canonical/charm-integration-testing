# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Log collection trigger test.

This test ensures that logs are collected by depending on the collect_logs_after_tests fixture.
The actual collection happens in the fixture's teardown phase, after all other tests complete.
"""


def test_trigger_log_collection(collect_logs_after_tests: None) -> None:
    """Trigger log collection by depending on the collect_logs_after_tests fixture.
    
    This test doesn't do anything itself - it just ensures the fixture runs,
    which will collect logs in its teardown phase after all tests complete.
    """
    # This test intentionally does nothing - the fixture handles log collection
    pass
