"""Public connector API and the deliberately manual built-in registry."""
from .base import ALLOWED_PROTOCOLS, ProxyCandidate, ProxySourceConnector, parse_candidate
from .http_json_feed import HttpJsonFeedConnector
from .jsonl_inventory import JsonlInventoryConnector
from .registry import ConnectorRegistry, collect_all, load_connectors

CONNECTOR_REGISTRY = ConnectorRegistry()
CONNECTOR_REGISTRY.register("jsonl_inventory", JsonlInventoryConnector.from_config)
CONNECTOR_REGISTRY.register("http_json_feed", HttpJsonFeedConnector.from_config)

__all__ = [
    "ALLOWED_PROTOCOLS",
    "CONNECTOR_REGISTRY",
    "ConnectorRegistry",
    "HttpJsonFeedConnector",
    "JsonlInventoryConnector",
    "ProxyCandidate",
    "ProxySourceConnector",
    "collect_all",
    "load_connectors",
    "parse_candidate",
]
