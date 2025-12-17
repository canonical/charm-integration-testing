# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for retry_decorator module."""

import pytest

from utils.retry_decorator import retry_on_failure


class TestRetryOnFailure:
    """Test suite for the retry_on_failure decorator."""

    def test_success_on_first_attempt(self, monkeypatch):
        """Test that function succeeds without retries when it works on first attempt."""
        calls = {"count": 0}

        @retry_on_failure(message="test", max_retries=3, delay=0.1)
        def successful_func():
            calls["count"] += 1
            return "success"

        # avoid actual sleeping in tests
        monkeypatch.setattr("utils.retry_decorator.sleep", lambda s: None)

        result = successful_func()
        assert result == "success"
        assert calls["count"] == 1

    def test_success_after_retries_with_matching_message(self, monkeypatch):
        """Test that function succeeds after retries when RuntimeError contains matching message."""
        calls = {"count": 0}

        @retry_on_failure(message="sealed", max_retries=3, delay=0.1)
        def flaky_func():
            calls["count"] += 1
            if calls["count"] < 3:
                raise RuntimeError("Vault is sealed")
            return "success"

        monkeypatch.setattr("utils.retry_decorator.sleep", lambda s: None)

        result = flaky_func()
        assert result == "success"
        assert calls["count"] == 3

    def test_raises_after_max_retries_exhausted(self, monkeypatch):
        """Test that exception is raised after max retries are exhausted."""
        calls = {"count": 0}

        @retry_on_failure(message="sealed", max_retries=2, delay=0.1)
        def always_fail():
            calls["count"] += 1
            raise RuntimeError("Vault is sealed")

        monkeypatch.setattr("utils.retry_decorator.sleep", lambda s: None)

        with pytest.raises(RuntimeError, match="Vault is sealed"):
            always_fail()
        assert calls["count"] == 3  # initial try + 2 retries

    def test_no_retry_when_message_does_not_match(self, monkeypatch):
        """Test that no retries occur when error message doesn't match."""
        calls = {"count": 0}

        @retry_on_failure(message="sealed", max_retries=3, delay=0.1)
        def func_with_different_error():
            calls["count"] += 1
            raise RuntimeError("Different error message")

        monkeypatch.setattr("utils.retry_decorator.sleep", lambda s: None)

        with pytest.raises(RuntimeError, match="Different error message"):
            func_with_different_error()
        assert calls["count"] == 1  # no retries

    def test_no_retry_on_non_runtime_error(self, monkeypatch):
        """Test that non-RuntimeError exceptions are not retried."""
        calls = {"count": 0}

        @retry_on_failure(message="sealed", max_retries=3, delay=0.1)
        def func_with_value_error():
            calls["count"] += 1
            raise ValueError("Invalid value")

        monkeypatch.setattr("utils.retry_decorator.sleep", lambda s: None)

        with pytest.raises(ValueError, match="Invalid value"):
            func_with_value_error()
        assert calls["count"] == 1  # no retries

    def test_backoff_increases_delay(self, monkeypatch):
        """Test that delay increases with backoff factor on each retry."""
        calls = {"count": 0}
        sleep_delays = []

        def mock_sleep(delay):
            sleep_delays.append(delay)

        monkeypatch.setattr("utils.retry_decorator.sleep", mock_sleep)

        @retry_on_failure(message="sealed", max_retries=3, delay=1.0, backoff=2.0)
        def always_fail():
            calls["count"] += 1
            raise RuntimeError("Vault is sealed")

        with pytest.raises(RuntimeError):
            always_fail()

        # Verify exponential backoff: 1.0, 2.0, 4.0
        assert sleep_delays == [1.0, 2.0, 4.0]

    def test_case_insensitive_message_matching(self, monkeypatch):
        """Test that message matching is case-insensitive."""
        calls = {"count": 0}

        @retry_on_failure(message="sealed", max_retries=3, delay=0.1)
        def func_with_uppercase_error():
            calls["count"] += 1
            if calls["count"] < 2:
                raise RuntimeError("VAULT IS SEALED")
            return "success"

        monkeypatch.setattr("utils.retry_decorator.sleep", lambda s: None)

        result = func_with_uppercase_error()
        assert result == "success"
        assert calls["count"] == 2

    def test_message_substring_matching(self, monkeypatch):
        """Test that message matching works with substrings."""
        calls = {"count": 0}

        @retry_on_failure(message="sealed", max_retries=3, delay=0.1)
        def func_with_partial_match():
            calls["count"] += 1
            if calls["count"] < 2:
                raise RuntimeError("The vault is currently sealed and cannot be accessed")
            return "success"

        monkeypatch.setattr("utils.retry_decorator.sleep", lambda s: None)

        result = func_with_partial_match()
        assert result == "success"
        assert calls["count"] == 2

    def test_preserves_function_metadata(self):
        """Test that decorator preserves original function metadata."""

        @retry_on_failure(message="test", max_retries=3, delay=0.1)
        def documented_func():
            """This is a test function."""
            pass

        assert documented_func.__name__ == "documented_func"
        assert documented_func.__doc__ == "This is a test function."

    def test_with_function_arguments(self, monkeypatch):
        """Test that decorated function correctly handles arguments and kwargs."""
        calls = {"count": 0}

        @retry_on_failure(message="sealed", max_retries=2, delay=0.1)
        def func_with_args(x, y, z=10):
            calls["count"] += 1
            if calls["count"] < 2:
                raise RuntimeError("Vault is sealed")
            return x + y + z

        monkeypatch.setattr("utils.retry_decorator.sleep", lambda s: None)

        result = func_with_args(1, 2, z=3)
        assert result == 6
        assert calls["count"] == 2

    def test_return_types_preserved(self, monkeypatch):
        """Test that various return types are preserved."""

        @retry_on_failure(message="test", max_retries=1, delay=0.1)
        def return_dict():
            return {"key": "value"}

        @retry_on_failure(message="test", max_retries=1, delay=0.1)
        def return_list():
            return [1, 2, 3]

        @retry_on_failure(message="test", max_retries=1, delay=0.1)
        def return_none():
            return None

        monkeypatch.setattr("utils.retry_decorator.sleep", lambda s: None)

        assert return_dict() == {"key": "value"}
        assert return_list() == [1, 2, 3]
        assert return_none() is None

    def test_default_parameters(self, monkeypatch):
        """Test decorator with default parameters."""
        calls = {"count": 0}

        @retry_on_failure(message="error")
        def func():
            calls["count"] += 1
            if calls["count"] < 2:
                raise RuntimeError("error occurred")
            return "success"

        monkeypatch.setattr("utils.retry_decorator.sleep", lambda s: None)

        result = func()
        assert result == "success"
        # Default max_retries is 3, so it should succeed
        assert calls["count"] == 2

    def test_zero_delay(self, monkeypatch):
        """Test that decorator works with zero delay."""
        calls = {"count": 0}
        sleep_calls = {"count": 0}

        def mock_sleep(delay):
            sleep_calls["count"] += 1
            assert delay == 0.0

        monkeypatch.setattr("utils.retry_decorator.sleep", mock_sleep)

        @retry_on_failure(message="test", max_retries=2, delay=0.0, backoff=1.0)
        def func():
            calls["count"] += 1
            if calls["count"] < 2:
                raise RuntimeError("test error")
            return "success"

        result = func()
        assert result == "success"
        assert calls["count"] == 2
        assert sleep_calls["count"] == 1
