"""OpenTelemetry instrumentation for CUA SDK.

Provides metrics and tracing for the Four Golden Signals:
- Latency: Operation duration histograms
- Traffic: Operation counters
- Errors: Error counters
- Saturation: Concurrent operation gauges

Plus stability metrics:
- API request success/failure tracking
- Request latency histograms with SLO threshold tracking
- Customer churn rate (% of requests that failed or exceeded latency target)
"""

from __future__ import annotations

import atexit
import logging
import os
import time
from contextlib import contextmanager
from functools import wraps
from threading import Lock
from typing import Any, Callable, Dict, Generator, Optional, TypeVar, Union

logger = logging.getLogger("core.telemetry.otel")

# Type vars for decorator
F = TypeVar("F", bound=Callable[..., Any])

# Default OTEL endpoint
DEFAULT_OTEL_ENDPOINT = "https://otel.cua.ai"

# Default latency target in seconds — requests exceeding this are counted as
# "unhappy" for churn rate purposes.  Override via CUA_LATENCY_TARGET_SECONDS.
DEFAULT_LATENCY_TARGET_SECONDS = 30.0

# Lazy initialization state
_initialized = False
_init_failed = False
_init_lock = Lock()

# OTEL components (lazily initialized)
_meter: Optional[Any] = None
_tracer: Optional[Any] = None
_meter_provider: Optional[Any] = None
_tracer_provider: Optional[Any] = None

# Metrics (lazily initialized)
_operation_duration: Optional[Any] = None  # Histogram
_operations_total: Optional[Any] = None  # Counter
_errors_total: Optional[Any] = None  # Counter
_concurrent_operations: Optional[Any] = None  # UpDownCounter
_tokens_total: Optional[Any] = None  # Counter

# Stability metrics (lazily initialized)
_api_requests_total: Optional[Any] = None  # Counter
_api_request_duration: Optional[Any] = None  # Histogram
_api_errors_total: Optional[Any] = None  # Counter
_api_requests_exceeding_target: Optional[Any] = None  # Counter


def is_otel_enabled() -> bool:
    """Check if OpenTelemetry is enabled.

    Canonical opt-out: ``CUA_TELEMETRY_ENABLED=false``.
    ``CUA_TELEMETRY_DISABLED`` is deprecated — a warning is emitted on first
    use and the value is honoured for backwards compatibility.
    """
    import warnings

    disabled_val = os.environ.get("CUA_TELEMETRY_DISABLED", "")
    if disabled_val:
        warnings.warn(
            "CUA_TELEMETRY_DISABLED is deprecated. " "Use CUA_TELEMETRY_ENABLED=false instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        if disabled_val.lower() in {"1", "true", "yes", "on"}:
            return False

    return os.environ.get("CUA_TELEMETRY_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _get_otel_endpoint() -> str:
    """Get the OTLP endpoint URL."""
    return os.environ.get("CUA_OTEL_ENDPOINT", DEFAULT_OTEL_ENDPOINT)


def _get_service_name() -> str:
    """Get the service name for OTEL."""
    return os.environ.get("CUA_OTEL_SERVICE_NAME", "cua-sdk")


def _get_latency_target() -> float:
    """Get the latency target in seconds for churn rate calculation."""
    try:
        return float(os.environ.get("CUA_LATENCY_TARGET_SECONDS", str(DEFAULT_LATENCY_TARGET_SECONDS)))
    except (ValueError, TypeError):
        return DEFAULT_LATENCY_TARGET_SECONDS


def _initialize_otel() -> bool:
    """Initialize OpenTelemetry components.

    Returns True if initialization succeeded, False otherwise.
    Thread-safe via lock.
    """
    global _initialized, _init_failed, _meter, _tracer, _meter_provider, _tracer_provider
    global _operation_duration, _operations_total, _errors_total
    global _concurrent_operations, _tokens_total
    global _api_requests_total, _api_request_duration, _api_errors_total
    global _api_requests_exceeding_target

    if _initialized:
        return True
    if _init_failed:
        return False

    with _init_lock:
        # Double-check after acquiring lock
        if _initialized:
            return True
        if _init_failed:
            return False

        if not is_otel_enabled():
            logger.debug("OpenTelemetry disabled via CUA_TELEMETRY_DISABLED")
            return False

        try:
            # Import OTEL packages lazily
            from opentelemetry import metrics, trace
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,
            )
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            # Create resource with service info
            resource = Resource.create(
                {
                    "service.name": _get_service_name(),
                    "service.version": _get_sdk_version(),
                }
            )

            endpoint = _get_otel_endpoint()

            # Set up metrics
            metric_exporter = OTLPMetricExporter(
                endpoint=f"{endpoint}/v1/metrics",
            )
            metric_reader = PeriodicExportingMetricReader(
                metric_exporter,
                export_interval_millis=60000,  # Export every 60 seconds
            )
            _meter_provider = MeterProvider(
                resource=resource,
                metric_readers=[metric_reader],
            )
            metrics.set_meter_provider(_meter_provider)
            _meter = metrics.get_meter("cua-sdk", _get_sdk_version())

            # Set up tracing
            trace_exporter = OTLPSpanExporter(
                endpoint=f"{endpoint}/v1/traces",
            )
            _tracer_provider = TracerProvider(resource=resource)
            _tracer_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
            trace.set_tracer_provider(_tracer_provider)
            _tracer = trace.get_tracer("cua-sdk", _get_sdk_version())

            # Create metrics instruments
            _operation_duration = _meter.create_histogram(
                name="cua_sdk_operation_duration_seconds",
                description="Duration of SDK operations in seconds",
                unit="s",
            )

            _operations_total = _meter.create_counter(
                name="cua_sdk_operations_total",
                description="Total number of SDK operations",
                unit="1",
            )

            _errors_total = _meter.create_counter(
                name="cua_sdk_errors_total",
                description="Total number of SDK errors",
                unit="1",
            )

            _concurrent_operations = _meter.create_up_down_counter(
                name="cua_sdk_concurrent_operations",
                description="Number of concurrent SDK operations",
                unit="1",
            )

            _tokens_total = _meter.create_counter(
                name="cua_sdk_tokens_total",
                description="Total tokens consumed",
                unit="1",
            )

            # --- Stability metrics ---
            _api_requests_total = _meter.create_counter(
                name="cua_sdk_api_requests_total",
                description="Total API requests by endpoint and status",
                unit="1",
            )

            _api_request_duration = _meter.create_histogram(
                name="cua_sdk_api_request_duration_seconds",
                description="API request latency in seconds",
                unit="s",
            )

            _api_errors_total = _meter.create_counter(
                name="cua_sdk_api_errors_total",
                description="Total API request errors by type and endpoint",
                unit="1",
            )

            _api_requests_exceeding_target = _meter.create_counter(
                name="cua_sdk_api_requests_exceeding_latency_target",
                description="API requests that exceeded the latency target (contributes to churn)",
                unit="1",
            )

            # Register shutdown handler
            atexit.register(_shutdown_otel)

            _initialized = True
            logger.info(f"OpenTelemetry initialized with endpoint: {endpoint}")
            return True

        except ImportError as e:
            _init_failed = True
            logger.debug(
                f"OpenTelemetry packages not installed: {e}. "
                "Install with: pip install opentelemetry-api opentelemetry-sdk "
                "opentelemetry-exporter-otlp-proto-http"
            )
            return False
        except Exception as e:
            _init_failed = True
            logger.warning(f"Failed to initialize OpenTelemetry: {e}")
            return False


def _shutdown_otel() -> None:
    """Shutdown OpenTelemetry providers gracefully."""
    global _meter_provider, _tracer_provider

    try:
        if _meter_provider is not None:
            _meter_provider.shutdown()
        if _tracer_provider is not None:
            _tracer_provider.shutdown()
        logger.debug("OpenTelemetry shutdown complete")
    except Exception as e:
        logger.debug(f"Error during OpenTelemetry shutdown: {e}")


def _get_sdk_version() -> str:
    """Get the CUA SDK version."""
    try:
        from core import __version__

        return __version__
    except ImportError:
        return "unknown"


# --- Public API ---


def record_operation(
    operation: str,
    duration_seconds: float,
    status: str = "success",
    model: Optional[str] = None,
    os_type: Optional[str] = None,
    **extra_attributes: Any,
) -> None:
    """Record an operation metric (latency + traffic).

    Args:
        operation: Operation name (e.g., "agent.run", "computer.action.click")
        duration_seconds: Duration of the operation in seconds
        status: Operation status ("success" or "error")
        model: Model name if applicable
        os_type: OS type if applicable
        **extra_attributes: Additional attributes to record
    """
    if not _initialize_otel():
        return

    attributes: Dict[str, str] = {
        "operation": operation,
        "status": status,
    }
    if model:
        attributes["model"] = model
    if os_type:
        attributes["os_type"] = os_type
    for key, value in extra_attributes.items():
        if value is not None:
            attributes[key] = str(value)

    try:
        if _operation_duration is not None:
            _operation_duration.record(duration_seconds, attributes)
        if _operations_total is not None:
            _operations_total.add(1, attributes)
    except Exception as e:
        logger.debug(f"Failed to record operation metric: {e}")


def record_error(
    error_type: str,
    operation: str,
    model: Optional[str] = None,
    **extra_attributes: Any,
) -> None:
    """Record an error metric.

    Args:
        error_type: Type of error (e.g., "api_error", "timeout", "computer_error")
        operation: Operation that failed
        model: Model name if applicable
        **extra_attributes: Additional attributes to record
    """
    if not _initialize_otel():
        return

    attributes: Dict[str, str] = {
        "error_type": error_type,
        "operation": operation,
    }
    if model:
        attributes["model"] = model
    for key, value in extra_attributes.items():
        if value is not None:
            attributes[key] = str(value)

    try:
        if _errors_total is not None:
            _errors_total.add(1, attributes)
    except Exception as e:
        logger.debug(f"Failed to record error metric: {e}")


def record_tokens(
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    model: Optional[str] = None,
) -> None:
    """Record token usage metrics.

    Args:
        prompt_tokens: Number of prompt tokens
        completion_tokens: Number of completion tokens
        model: Model name
    """
    if not _initialize_otel():
        return

    try:
        if _tokens_total is not None:
            if prompt_tokens > 0:
                _tokens_total.add(
                    prompt_tokens,
                    {"token_type": "prompt", "model": model or "unknown"},
                )
            if completion_tokens > 0:
                _tokens_total.add(
                    completion_tokens,
                    {"token_type": "completion", "model": model or "unknown"},
                )
    except Exception as e:
        logger.debug(f"Failed to record token metric: {e}")


@contextmanager
def track_concurrent(operation_type: str) -> Generator[None, None, None]:
    """Context manager to track concurrent operations.

    Args:
        operation_type: Type of operation (e.g., "sessions", "runs")

    Example:
        with track_concurrent("agent_sessions"):
            # session is active
            pass
    """
    if not _initialize_otel():
        yield
        return

    attributes = {"operation_type": operation_type}

    try:
        if _concurrent_operations is not None:
            _concurrent_operations.add(1, attributes)
    except Exception as e:
        logger.debug(f"Failed to increment concurrent counter: {e}")

    try:
        yield
    finally:
        try:
            if _concurrent_operations is not None:
                _concurrent_operations.add(-1, attributes)
        except Exception as e:
            logger.debug(f"Failed to decrement concurrent counter: {e}")


@contextmanager
def create_span(
    name: str,
    attributes: Optional[Dict[str, Any]] = None,
) -> Generator[Any, None, None]:
    """Create a trace span context.

    Args:
        name: Span name
        attributes: Span attributes

    Yields:
        The span object (or None if tracing disabled)

    Example:
        with create_span("agent.run", {"model": "claude-3"}) as span:
            # do work
            if span:
                span.set_attribute("steps", 5)
    """
    if not _initialize_otel() or _tracer is None:
        yield None
        return

    try:
        with _tracer.start_as_current_span(name, attributes=attributes) as span:
            yield span
    except Exception as e:
        logger.debug(f"Failed to create span: {e}")
        yield None


def instrument_async(
    operation: str,
    model_attr: Optional[str] = None,
    os_type_attr: Optional[str] = None,
) -> Callable[[F], F]:
    """Decorator to instrument an async function.

    Records duration, success/error status, and creates a trace span.

    Args:
        operation: Operation name for metrics
        model_attr: Attribute name to extract model from kwargs
        os_type_attr: Attribute name to extract os_type from kwargs

    Example:
        @instrument_async("agent.run", model_attr="model")
        async def run(self, prompt: str, model: str = "claude-3"):
            ...
    """

    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not is_otel_enabled():
                return await func(*args, **kwargs)

            model = kwargs.get(model_attr) if model_attr else None
            os_type = kwargs.get(os_type_attr) if os_type_attr else None

            start_time = time.perf_counter()
            status = "success"
            error_type = None

            with create_span(operation, {"model": model, "os_type": os_type}):
                try:
                    result = await func(*args, **kwargs)
                    return result
                except Exception as e:
                    status = "error"
                    error_type = type(e).__name__
                    raise
                finally:
                    duration = time.perf_counter() - start_time
                    record_operation(
                        operation=operation,
                        duration_seconds=duration,
                        status=status,
                        model=model,
                        os_type=os_type,
                    )
                    if error_type:
                        record_error(
                            error_type=error_type,
                            operation=operation,
                            model=model,
                        )

        return wrapper  # type: ignore

    return decorator


def instrument_sync(
    operation: str,
    model_attr: Optional[str] = None,
    os_type_attr: Optional[str] = None,
) -> Callable[[F], F]:
    """Decorator to instrument a sync function.

    Records duration, success/error status, and creates a trace span.

    Args:
        operation: Operation name for metrics
        model_attr: Attribute name to extract model from kwargs
        os_type_attr: Attribute name to extract os_type from kwargs

    Example:
        @instrument_sync("computer.screenshot")
        def screenshot(self):
            ...
    """

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not is_otel_enabled():
                return func(*args, **kwargs)

            model = kwargs.get(model_attr) if model_attr else None
            os_type = kwargs.get(os_type_attr) if os_type_attr else None

            start_time = time.perf_counter()
            status = "success"
            error_type = None

            with create_span(operation, {"model": model, "os_type": os_type}):
                try:
                    result = func(*args, **kwargs)
                    return result
                except Exception as e:
                    status = "error"
                    error_type = type(e).__name__
                    raise
                finally:
                    duration = time.perf_counter() - start_time
                    record_operation(
                        operation=operation,
                        duration_seconds=duration,
                        status=status,
                        model=model,
                        os_type=os_type,
                    )
                    if error_type:
                        record_error(
                            error_type=error_type,
                            operation=operation,
                            model=model,
                        )

        return wrapper  # type: ignore

    return decorator


# --- Stability Metrics API ---


def record_api_request(
    endpoint: str,
    method: str,
    status_code: int,
    duration_seconds: float,
    error_type: Optional[str] = None,
    **extra_attributes: Any,
) -> None:
    """Record an API request for stability tracking.

    Tracks success/failure, latency, and whether the request exceeded
    the latency target (contributing to churn rate).

    Args:
        endpoint: API endpoint path (e.g., "/v1/images")
        method: HTTP method (e.g., "GET", "POST")
        status_code: HTTP response status code (0 for connection failures)
        duration_seconds: Request duration in seconds
        error_type: Error class name if the request failed with an exception
        **extra_attributes: Additional attributes to record
    """
    if not _initialize_otel():
        return

    is_success = 200 <= status_code < 400 and error_type is None
    status = "success" if is_success else "error"

    attributes: Dict[str, str] = {
        "endpoint": endpoint,
        "method": method,
        "status_code": str(status_code),
        "status": status,
    }
    for key, value in extra_attributes.items():
        if value is not None:
            attributes[key] = str(value)

    try:
        # Track request count
        if _api_requests_total is not None:
            _api_requests_total.add(1, attributes)

        # Track latency
        if _api_request_duration is not None:
            _api_request_duration.record(duration_seconds, attributes)

        # Track errors
        if not is_success and _api_errors_total is not None:
            error_attrs = {**attributes}
            if error_type:
                error_attrs["error_type"] = error_type
            _api_errors_total.add(1, error_attrs)

        # Track latency target breaches (contributes to churn)
        latency_target = _get_latency_target()
        if duration_seconds > latency_target and _api_requests_exceeding_target is not None:
            _api_requests_exceeding_target.add(
                1,
                {
                    "endpoint": endpoint,
                    "method": method,
                    "latency_target_seconds": str(latency_target),
                },
            )

    except Exception as e:
        logger.debug(f"Failed to record API request metric: {e}")


def record_api_error(
    endpoint: str,
    method: str,
    error_type: str,
    duration_seconds: float = 0.0,
    **extra_attributes: Any,
) -> None:
    """Record an API request that failed with an exception (no HTTP status).

    Use this for connection errors, timeouts, DNS failures, etc. where
    no HTTP response was received.

    Args:
        endpoint: API endpoint path
        method: HTTP method
        error_type: Exception class name (e.g., "ConnectionError", "TimeoutError")
        duration_seconds: Time elapsed before the error
        **extra_attributes: Additional attributes to record
    """
    record_api_request(
        endpoint=endpoint,
        method=method,
        status_code=0,
        duration_seconds=duration_seconds,
        error_type=error_type,
        **extra_attributes,
    )


class StabilityTracker:
    """In-process tracker that computes client-side stability scores.

    This supplements the OTel counters/histograms with a simple rolling view
    that can be queried locally (e.g. for adaptive retry logic or health
    checks).

    Thread-safe.
    """

    def __init__(self, latency_target: Optional[float] = None):
        self._lock = Lock()
        self._total_requests = 0
        self._failed_requests = 0
        self._slow_requests = 0  # exceeded latency target
        self._latency_target = latency_target or _get_latency_target()

    def record(
        self,
        success: bool,
        duration_seconds: float,
    ) -> None:
        """Record an API request outcome."""
        with self._lock:
            self._total_requests += 1
            if not success:
                self._failed_requests += 1
            if duration_seconds > self._latency_target:
                self._slow_requests += 1

    @property
    def total_requests(self) -> int:
        with self._lock:
            return self._total_requests

    @property
    def failed_requests(self) -> int:
        with self._lock:
            return self._failed_requests

    @property
    def success_rate(self) -> float:
        """Fraction of requests that succeeded (0.0 – 1.0)."""
        with self._lock:
            if self._total_requests == 0:
                return 1.0
            return (self._total_requests - self._failed_requests) / self._total_requests

    @property
    def error_rate(self) -> float:
        """Fraction of requests that failed (0.0 – 1.0)."""
        return 1.0 - self.success_rate

    @property
    def churn_rate(self) -> float:
        """Fraction of requests that were 'unhappy' — either failed or exceeded
        the latency target (0.0 – 1.0).  Inspired by customer-happiness models
        in tycoon-style simulations.
        """
        with self._lock:
            if self._total_requests == 0:
                return 0.0
            unhappy = self._failed_requests + self._slow_requests
            # A request can be both failed *and* slow; cap at total.
            return min(unhappy, self._total_requests) / self._total_requests

    @property
    def stability_score(self) -> float:
        """Overall stability score (0.0 – 1.0).

        ``1.0`` means all requests succeeded within the latency target.
        ``0.0`` means every request was unhappy.
        """
        return 1.0 - self.churn_rate

    def reset(self) -> None:
        """Reset all counters (useful for windowed tracking)."""
        with self._lock:
            self._total_requests = 0
            self._failed_requests = 0
            self._slow_requests = 0


# Global singleton tracker
_stability_tracker: Optional[StabilityTracker] = None
_tracker_lock = Lock()


def get_stability_tracker() -> StabilityTracker:
    """Return the global :class:`StabilityTracker` instance, creating it if needed."""
    global _stability_tracker
    if _stability_tracker is None:
        with _tracker_lock:
            if _stability_tracker is None:
                _stability_tracker = StabilityTracker()
    return _stability_tracker
