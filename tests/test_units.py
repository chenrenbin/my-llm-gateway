
import logging

import pytest
from tests.conftest import make_config

from gateway.application.costing import calculate_cost
from gateway.application.router import ModelRouter
from gateway.application.structured import (
    content_from_response,
    repair_instruction,
    schema_from_request,
    validate_response,
)
from gateway.domain.config import PriceConfig
from gateway.domain.errors import GatewayError, StructuredOutputError
from gateway.infrastructure.config import load_config
from gateway.observability.logging_setup import request_id, request_id_var


def test_load_config_expands_env_and_resolves_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret-key")
    cfg = tmp_path / "gateway.yaml"
    cfg.write_text(
        """
providers:
  p:
    base_url: https://up.test/
    api_key: ${TEST_PROVIDER_KEY}
models:
  m:
    routes:
      - provider: p
        model: real
pricing:
  real: {input_per_million: 1, output_per_million: 2}
database_url: data/gw.db
""",
        encoding="utf-8",
    )
    config = load_config(cfg)
    assert config.providers["p"].api_key.get_secret_value() == "secret-key"
    assert config.providers["p"].base_url == "https://up.test"
    assert config.database_url.endswith("data/gw.db")


def test_load_config_rejects_unknown_provider(tmp_path):
    cfg = tmp_path / "gateway.yaml"
    cfg.write_text(
        """
providers:
  p: {base_url: https://up.test}
models:
  m:
    routes:
      - provider: ghost
        model: real
""",
        encoding="utf-8",
    )
    with pytest.raises(GatewayError) as exc:
        load_config(cfg)
    assert exc.value.code == "gateway_misconfigured"


def _config(tmp_path, **overrides):
    return make_config(tmp_path / "g.db", **overrides)


def test_router_unknown_model_404(tmp_path):
    router = ModelRouter(_config(tmp_path))
    with pytest.raises(GatewayError) as exc:
        router.candidates("ghost", "chat")
    assert exc.value.status_code == 404
    assert exc.value.code == "model_not_found"


def test_router_filters_by_api(tmp_path):
    router = ModelRouter(_config(tmp_path, models={"smart": {"routes": [
        {"provider": "primary", "model": "primary-model", "api": "chat"},
    ]}}))
    assert router.candidates("smart", "chat")
    with pytest.raises(GatewayError) as exc:
        router.candidates("smart", "responses")
    assert exc.value.code == "no_healthy_route"


def test_router_weighted_round_robin_rotates_primary(tmp_path):
    config = _config(
        tmp_path,
        models={
            "smart": {
                "strategy": "weighted_round_robin",
                "routes": [
                    {"provider": "primary", "model": "primary-model", "weight": 1},
                    {"provider": "fallback", "model": "fallback-model", "weight": 1},
                ],
            }
        },
    )
    router = ModelRouter(config)
    first = router.candidates("smart", "chat")[0].provider
    router.record_success(first)
    second = router.candidates("smart", "chat")[0].provider
    assert first != second


def test_router_circuit_breaker_opens_after_threshold(tmp_path, caplog):
    config = _config(tmp_path, circuit_breaker={"failure_threshold": 1, "cooldown_seconds": 30})
    router = ModelRouter(config)
    with caplog.at_level(logging.WARNING, logger="gateway.application.router"):
        router.record_failure("primary")
        providers = [c.provider for c in router.candidates("smart", "chat")]
    assert "primary" not in providers
    assert router.status()["primary"]["open"] is True

    events = [r for r in caplog.records if r.__dict__.get("event") == "circuit_breaker_open"]
    assert len(events) == 1
    assert events[0].__dict__["provider"] == "primary"
    assert events[0].__dict__["failures"] == 1


def test_router_circuit_breaker_half_open_after_cooldown(tmp_path, caplog):
    config = _config(tmp_path, circuit_breaker={"failure_threshold": 1, "cooldown_seconds": 0})
    router = ModelRouter(config)
    router.record_failure("primary")
    with caplog.at_level(logging.INFO, logger="gateway.application.router"):
        providers = [c.provider for c in router.candidates("smart", "chat")]
    assert providers
    events = [r for r in caplog.records if r.__dict__.get("event") == "circuit_breaker_half_open"]
    assert len(events) == 1


def test_request_id_context_binds_and_resets():
    assert request_id_var.get() is None
    with request_id("req_abc"):
        assert request_id_var.get() == "req_abc"
    assert request_id_var.get() is None


def test_calculate_cost_with_cached_tokens():
    price = PriceConfig(input_per_million=10, output_per_million=20, cached_input_per_million=1)
    assert calculate_cost(price, 1_000_000, 1_000_000, cached_tokens=1_000_000) == pytest.approx(21)
    assert calculate_cost(None, 10, 10) == 0


def test_schema_from_request_extraction():
    chat = {"response_format": {"type": "json_schema", "json_schema": {"schema": {"type": "object"}}}}
    assert schema_from_request("chat", chat) == {"type": "object"}
    assert schema_from_request("chat", {}) is None
    resp = {"text": {"format": {"type": "json_schema", "schema": {"type": "array"}}}}
    assert schema_from_request("responses", resp) == {"type": "array"}


def test_validate_response_strips_fences_and_reports_errors():
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
    payload = {"choices": [{"message": {"content": '```json\n{"a": 1}\n```'}}]}
    assert validate_response("chat", payload, schema) == {"a": 1}

    bad = {"choices": [{"message": {"content": "not json"}}]}
    with pytest.raises(StructuredOutputError):
        validate_response("chat", bad, schema)

    wrong = {"choices": [{"message": {"content": '{"a": "x"}'}}]}
    with pytest.raises(StructuredOutputError) as exc:
        validate_response("chat", wrong, schema)
    assert repair_instruction(exc.value, schema).startswith("Your previous response failed")


def test_content_from_response_responses_api():
    payload = {"output_text": "hello"}
    assert content_from_response("responses", payload) == "hello"
    fail = {"output": []}
    with pytest.raises(StructuredOutputError):
        content_from_response("responses", fail)
