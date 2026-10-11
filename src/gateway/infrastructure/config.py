import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ..domain.config import GatewayConfig
from ..domain.errors import GatewayError

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")

DEFAULT_CONFIG_PATH = "config/gateway.yaml"


def _expand_env(value: object) -> object:
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            return ""

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def load_config(path: str | Path | None = None) -> GatewayConfig:
    """Load gateway YAML (+ ${ENV} expansion). Fail-fast on missing/invalid config."""
    config_path = Path(path or os.getenv("GATEWAY_CONFIG", DEFAULT_CONFIG_PATH))
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path

    if not config_path.exists():
        raise GatewayError(
            f"缺少网关配置文件: {config_path}",
            status_code=500,
            code="gateway_misconfigured",
        )

    try:
        raw: Any = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise GatewayError(
            f"YAML 解析失败: {exc}", status_code=500, code="gateway_misconfigured"
        ) from exc

    if not isinstance(raw, dict):
        raise GatewayError(
            f"配置根节点必须是映射: {config_path}",
            status_code=500,
            code="gateway_misconfigured",
        )

    try:
        config = GatewayConfig.model_validate(_expand_env(raw))
    except ValidationError as exc:
        raise GatewayError(
            f"配置非法: {exc}", status_code=500, code="gateway_misconfigured"
        ) from exc

    db_path = Path(config.database_url)
    if not db_path.is_absolute():
        config.database_url = str((Path.cwd() / db_path).resolve())
    return config
