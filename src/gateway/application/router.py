import itertools
import time
from dataclasses import dataclass

from ..domain.config import CircuitBreakerConfig, GatewayConfig, RouteTarget
from ..domain.errors import GatewayError


@dataclass
class CircuitState:
    failures: int = 0
    opened_at: float | None = None


class ModelRouter:
    """Resolves a public model alias to an ordered list of healthy route targets.

    Supports `priority` and `weighted_round_robin` strategies plus a lightweight
    in-process circuit breaker per provider.
    """

    def __init__(self, config: GatewayConfig) -> None:
        self._config = config
        self._circuit_config: CircuitBreakerConfig = config.circuit_breaker
        self._circuits: dict[str, CircuitState] = {}
        self._counters: dict[str, itertools.count] = {}

    def candidates(self, model: str, api: str) -> list[RouteTarget]:
        route_config = self._config.models.get(model)
        if route_config is None:
            raise GatewayError(
                f"Unknown model alias: {model}",
                status_code=404,
                error_type="invalid_request_error",
                code="model_not_found",
                param="model",
            )
        available = [
            route
            for route in route_config.routes
            if self._config.providers[route.provider].enabled
            and route.api in (api, "both")
            and self._is_available(route.provider)
        ]
        if not available:
            raise GatewayError(
                f"No healthy route supports {api!r} for model {model!r}",
                status_code=503,
                error_type="service_unavailable_error",
                code="no_healthy_route",
            )
        if route_config.strategy == "priority":
            return available

        weighted = [route for route in available for _ in range(route.weight)]
        counter = self._counters.setdefault(model, itertools.count())
        offset = next(counter) % len(weighted)
        primary = weighted[offset]
        return [primary, *[route for route in available if route != primary]]

    def record_success(self, provider: str) -> None:
        self._circuits[provider] = CircuitState()

    def record_failure(self, provider: str) -> None:
        state = self._circuits.setdefault(provider, CircuitState())
        state.failures += 1
        if state.failures >= self._circuit_config.failure_threshold:
            state.opened_at = time.monotonic()

    def _is_available(self, provider: str) -> bool:
        state = self._circuits.get(provider)
        if not state or state.opened_at is None:
            return True
        if time.monotonic() - state.opened_at >= self._circuit_config.cooldown_seconds:
            state.failures = 0
            state.opened_at = None
            return True
        return False

    def status(self) -> dict[str, dict[str, object]]:
        return {
            provider: {
                "failures": state.failures,
                "open": state.opened_at is not None and not self._is_available(provider),
            }
            for provider, state in self._circuits.items()
        }
