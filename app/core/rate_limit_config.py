"""
Loads and resolves rate limit rules from config/rate_limit_config.json.

Design goal: rate limit rules should be data, not code. Adding a new
endpoint-specific limit should mean editing JSON, not touching the
middleware. This also mirrors how real API gateways (Kong, Envoy, AWS
API Gateway) let you configure limits per-route without redeploying code.
"""

import json
import os
from dataclasses import dataclass


@dataclass
class RateLimitRule:
    algorithm: str
    capacity: float
    refill_rate: float


class RateLimitConfig:
    def __init__(self, config_path: str):
        with open(config_path, "r") as f:
            raw = json.load(f)

        self.rules: dict[str, RateLimitRule] = {}
        for rule in raw.get("rules", []):
            self.rules[rule["endpoint"]] = RateLimitRule(
                algorithm=rule["algorithm"],
                capacity=rule["capacity"],
                refill_rate=rule["refill_rate"],
            )

        default = raw["default"]
        self.default_rule = RateLimitRule(
            algorithm=default["algorithm"],
            capacity=default["capacity"],
            refill_rate=default["refill_rate"],
        )

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
