"""Tests for stability metrics in the OTel telemetry module.

Tests cover:
- StabilityTracker success/error/churn rate computations
- record_api_request / record_api_error telemetry recording
- Latency target threshold behaviour
"""

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# The posthog module imports ``from core import __version__`` which relies on
# a namespace alias that may not be present in every environment.  Provide a
# stub so that importing cua_core.telemetry doesn't blow up during tests.
_core_stub = types.ModuleType("core")
_core_stub.__version__ = "0.0.0-test"
sys.modules.setdefault("core", _core_stub)


class TestStabilityTracker:
    """Unit tests for the in-process StabilityTracker."""

    def _make_tracker(self, latency_target=30.0):
        from cua_core.telemetry.otel import StabilityTracker

        return StabilityTracker(latency_target=latency_target)

    def test_initial_state(self):
        tracker = self._make_tracker()
        assert tracker.total_requests == 0
        assert tracker.failed_requests == 0
        assert tracker.success_rate == 1.0
        assert tracker.error_rate == 0.0
        assert tracker.churn_rate == 0.0
        assert tracker.stability_score == 1.0

    def test_all_successes(self):
        tracker = self._make_tracker()
        for _ in range(10):
            tracker.record(success=True, duration_seconds=1.0)
        assert tracker.total_requests == 10
        assert tracker.failed_requests == 0
        assert tracker.success_rate == 1.0
        assert tracker.error_rate == 0.0
        assert tracker.churn_rate == 0.0
        assert tracker.stability_score == 1.0

    def test_all_failures(self):
        tracker = self._make_tracker()
        for _ in range(5):
            tracker.record(success=False, duration_seconds=1.0)
        assert tracker.total_requests == 5
        assert tracker.failed_requests == 5
        assert tracker.success_rate == 0.0
        assert tracker.error_rate == 1.0

    def test_mixed_success_failure(self):
        tracker = self._make_tracker()
        for _ in range(7):
            tracker.record(success=True, duration_seconds=1.0)
        for _ in range(3):
            tracker.record(success=False, duration_seconds=1.0)
        assert tracker.total_requests == 10
        assert tracker.success_rate == pytest.approx(0.7)
        assert tracker.error_rate == pytest.approx(0.3)

    def test_slow_requests_increase_churn(self):
        tracker = self._make_tracker(latency_target=5.0)
        # 8 fast successes, 2 slow successes
        for _ in range(8):
            tracker.record(success=True, duration_seconds=1.0)
        for _ in range(2):
            tracker.record(success=True, duration_seconds=10.0)  # exceeds 5s target
        assert tracker.total_requests == 10
        assert tracker.failed_requests == 0
        assert tracker.success_rate == 1.0
        # Churn = slow / total = 2/10
        assert tracker.churn_rate == pytest.approx(0.2)
        assert tracker.stability_score == pytest.approx(0.8)

    def test_churn_combines_failures_and_slow(self):
        tracker = self._make_tracker(latency_target=5.0)
        # 6 fast successes, 2 slow successes, 2 fast failures
        for _ in range(6):
            tracker.record(success=True, duration_seconds=1.0)
        for _ in range(2):
            tracker.record(success=True, duration_seconds=10.0)
        for _ in range(2):
            tracker.record(success=False, duration_seconds=1.0)
        # unhappy = 2 failed + 2 slow = 4 / 10
        assert tracker.churn_rate == pytest.approx(0.4)
        assert tracker.stability_score == pytest.approx(0.6)

    def test_churn_capped_at_1(self):
        """A request that is both failed AND slow should not double-count beyond 1.0."""
        tracker = self._make_tracker(latency_target=5.0)
        # All requests are both failed and slow
        for _ in range(5):
            tracker.record(success=False, duration_seconds=10.0)
        # unhappy = 5 failed + 5 slow = 10, but capped at total=5
        assert tracker.churn_rate == pytest.approx(1.0)
        assert tracker.stability_score == pytest.approx(0.0)

    def test_reset(self):
        tracker = self._make_tracker()
        tracker.record(success=True, duration_seconds=1.0)
        tracker.record(success=False, duration_seconds=1.0)
        assert tracker.total_requests == 2
        tracker.reset()
        assert tracker.total_requests == 0
        assert tracker.failed_requests == 0
        assert tracker.success_rate == 1.0
        assert tracker.churn_rate == 0.0


class TestGetStabilityTracker:
    """Test the singleton get_stability_tracker function."""

    def test_returns_singleton(self):
        import cua_core.telemetry.otel as otel_mod

        # Reset global state
        otel_mod._stability_tracker = None

        from cua_core.telemetry import get_stability_tracker

        t1 = get_stability_tracker()
        t2 = get_stability_tracker()
        assert t1 is t2

        # Clean up
        otel_mod._stability_tracker = None


class TestRecordApiRequest:
    """Test record_api_request sends correct OTel metrics."""

    def test_successful_request_records_metrics(self, monkeypatch):
        """Verify a successful request records count + latency via OTel."""
        monkeypatch.setenv("CUA_TELEMETRY_ENABLED", "true")

        import cua_core.telemetry.otel as otel_mod

        # Mock the metric instruments
        mock_counter = MagicMock()
        mock_histogram = MagicMock()
        mock_error_counter = MagicMock()
        mock_exceed_counter = MagicMock()

        otel_mod._api_requests_total = mock_counter
        otel_mod._api_request_duration = mock_histogram
        otel_mod._api_errors_total = mock_error_counter
        otel_mod._api_requests_exceeding_target = mock_exceed_counter
        # Pretend already initialized
        otel_mod._initialized = True

        from cua_core.telemetry import record_api_request

        record_api_request(
            endpoint="/v1/images",
            method="GET",
            status_code=200,
            duration_seconds=0.5,
        )

        # Should record request count
        mock_counter.add.assert_called_once()
        attrs = mock_counter.add.call_args[0][1]
        assert attrs["status"] == "success"
        assert attrs["endpoint"] == "/v1/images"

        # Should record latency
        mock_histogram.record.assert_called_once()
        assert mock_histogram.record.call_args[0][0] == 0.5

        # Should NOT record error
        mock_error_counter.add.assert_not_called()

        # Should NOT record latency breach (0.5s < 30s default)
        mock_exceed_counter.add.assert_not_called()

        # Clean up
        otel_mod._initialized = False
        otel_mod._api_requests_total = None
        otel_mod._api_request_duration = None
        otel_mod._api_errors_total = None
        otel_mod._api_requests_exceeding_target = None

    def test_error_request_records_error_counter(self, monkeypatch):
        """Verify a 500 response records an error metric."""
        monkeypatch.setenv("CUA_TELEMETRY_ENABLED", "true")

        import cua_core.telemetry.otel as otel_mod

        mock_counter = MagicMock()
        mock_histogram = MagicMock()
        mock_error_counter = MagicMock()
        mock_exceed_counter = MagicMock()

        otel_mod._api_requests_total = mock_counter
        otel_mod._api_request_duration = mock_histogram
        otel_mod._api_errors_total = mock_error_counter
        otel_mod._api_requests_exceeding_target = mock_exceed_counter
        otel_mod._initialized = True

        from cua_core.telemetry import record_api_request

        record_api_request(
            endpoint="/v1/images",
            method="POST",
            status_code=500,
            duration_seconds=1.2,
        )

        # Should record error
        mock_error_counter.add.assert_called_once()
        error_attrs = mock_error_counter.add.call_args[0][1]
        assert error_attrs["status"] == "error"

        # Clean up
        otel_mod._initialized = False
        otel_mod._api_requests_total = None
        otel_mod._api_request_duration = None
        otel_mod._api_errors_total = None
        otel_mod._api_requests_exceeding_target = None

    def test_slow_request_records_latency_breach(self, monkeypatch):
        """Verify a slow request records a latency target breach."""
        monkeypatch.setenv("CUA_TELEMETRY_ENABLED", "true")
        monkeypatch.setenv("CUA_LATENCY_TARGET_SECONDS", "2.0")

        import cua_core.telemetry.otel as otel_mod

        mock_counter = MagicMock()
        mock_histogram = MagicMock()
        mock_error_counter = MagicMock()
        mock_exceed_counter = MagicMock()

        otel_mod._api_requests_total = mock_counter
        otel_mod._api_request_duration = mock_histogram
        otel_mod._api_errors_total = mock_error_counter
        otel_mod._api_requests_exceeding_target = mock_exceed_counter
        otel_mod._initialized = True

        from cua_core.telemetry import record_api_request

        record_api_request(
            endpoint="/v1/images",
            method="GET",
            status_code=200,
            duration_seconds=5.0,  # exceeds 2s target
        )

        # Should record latency breach
        mock_exceed_counter.add.assert_called_once()

        # Clean up
        otel_mod._initialized = False
        otel_mod._api_requests_total = None
        otel_mod._api_request_duration = None
        otel_mod._api_errors_total = None
        otel_mod._api_requests_exceeding_target = None


class TestRecordApiError:
    """Test record_api_error for connection-level failures."""

    def test_connection_error_records_with_status_code_zero(self, monkeypatch):
        monkeypatch.setenv("CUA_TELEMETRY_ENABLED", "true")

        import cua_core.telemetry.otel as otel_mod

        mock_counter = MagicMock()
        mock_histogram = MagicMock()
        mock_error_counter = MagicMock()
        mock_exceed_counter = MagicMock()

        otel_mod._api_requests_total = mock_counter
        otel_mod._api_request_duration = mock_histogram
        otel_mod._api_errors_total = mock_error_counter
        otel_mod._api_requests_exceeding_target = mock_exceed_counter
        otel_mod._initialized = True

        from cua_core.telemetry import record_api_error

        record_api_error(
            endpoint="/v1/images",
            method="GET",
            error_type="ConnectionError",
            duration_seconds=0.1,
        )

        # Should record with status_code=0
        attrs = mock_counter.add.call_args[0][1]
        assert attrs["status_code"] == "0"
        assert attrs["status"] == "error"

        # Should record error with error_type
        error_attrs = mock_error_counter.add.call_args[0][1]
        assert error_attrs["error_type"] == "ConnectionError"

        # Clean up
        otel_mod._initialized = False
        otel_mod._api_requests_total = None
        otel_mod._api_request_duration = None
        otel_mod._api_errors_total = None
        otel_mod._api_requests_exceeding_target = None


class TestLatencyTarget:
    """Test latency target configuration."""

    def test_default_target(self, monkeypatch):
        monkeypatch.delenv("CUA_LATENCY_TARGET_SECONDS", raising=False)
        from cua_core.telemetry.otel import _get_latency_target

        assert _get_latency_target() == 30.0

    def test_custom_target(self, monkeypatch):
        monkeypatch.setenv("CUA_LATENCY_TARGET_SECONDS", "10.0")
        from cua_core.telemetry.otel import _get_latency_target

        assert _get_latency_target() == 10.0

    def test_invalid_target_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CUA_LATENCY_TARGET_SECONDS", "not_a_number")
        from cua_core.telemetry.otel import _get_latency_target

        assert _get_latency_target() == 30.0
