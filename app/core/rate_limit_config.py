"""
Loads and resolves rate limit rules from config/rate_limit_config.json.

Design goal: rate limit rules should be data, not code. Adding a new
endpoint-specific limit - or switching which algorithm an endpoint uses -
should mean editing JSON, not touching the middleware. This also mirrors
how real API gateways (Kong, Envoy, AWS API Gateway) let you configure
limits per-route without redeploying code.

Each rule carries an `algorithm` name plus a `params` dict whose shape
depends on that algorithm (e.g. token_bucket needs capacity/refill_rate,
sliding_window needs limit/window_size_seconds). See app/core/strategy.py
for how these params get turned into an actual strategy instance.
"""

import json
import os
from dataclasses import dataclass, field


@dataclass
class RateLimitRule:
    algorithm: str
    params: dict = field(default_factory=dict)
    fail_mode: str = "open"  # "open" or "closed" - see app/middleware/rate_limit.py


class RateLimitConfig:
    def __init__(self, config_path: str):
        with open(config_path, "r") as f:
            raw = json.load(f)

        self.rules: dict[str, RateLimitRule] = {}
        for rule in raw.get("rules", []):
            self.rules[rule["endpoint"]] = self._parse_rule(rule)

        self.default_rule = self._parse_rule(raw["default"])

    @staticmethod
    def _parse_rule(rule: dict) -> RateLimitRule:
        algorithm = rule["algorithm"]
        fail_mode = rule.get("fail_mode", "open")
        # Everything except these bookkeeping fields is an algorithm-specific
        # parameter (capacity, refill_rate, limit, window_size_seconds, etc.)
        excluded = ("endpoint", "algorithm", "fail_mode")
        params = {k: v for k, v in rule.items() if k not in excluded}
        return RateLimitRule(algorithm=algorithm, params=params, fail_mode=fail_mode)

    def resolve(self, path: str) -> RateLimitRule:
        """
        Find the rule for a given request path.
        Exact match first; falls back to the default rule if no
        endpoint-specific rule is configured.
        """
        return self.rules.get(path, self.default_rule)


def load_default_config() -> RateLimitConfig:
    config_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "config", "rate_limit_config.json"
    )
    return RateLimitConfig(os.path.normpath(config_path))
