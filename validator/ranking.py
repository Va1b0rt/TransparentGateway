"""Pure scoring and diversity-aware ranking for validated proxy candidates."""
from __future__ import annotations

import ipaddress
from collections import Counter
from typing import Any


def base_score(entry: dict[str, Any]) -> float:
    latency_ms = max(1, int(entry.get("latency_ms", 1)))
    success_streak = max(1, int(entry.get("success_streak", 1)))
    latency_component = 1000.0 / (1.0 + latency_ms)
    stability_component = min(success_streak, 10) * 2.0
    return latency_component + stability_component


def subnet_key(entry: dict[str, Any]) -> str | None:
    endpoint = str(entry.get("endpoint", ""))
    host = endpoint.rsplit(":", 1)[0]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    prefix = 24 if address.version == 4 else 48
    return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))


def asn_key(entry: dict[str, Any]) -> str | None:
    metadata = entry.get("metadata")
    if not isinstance(metadata, dict):
        return None
    value = str(metadata.get("asn", "")).strip().upper()
    return value or None


def rank_entries(
    entries: list[dict[str, Any]],
    *,
    reserve_size: int,
    min_score: float = 0.0,
) -> list[dict[str, Any]]:
    """Greedily rank entries while reducing repeated ASN and subnet selection."""
    if reserve_size < 1:
        raise ValueError("RESERVE_SIZE must be positive")

    remaining = [dict(entry) for entry in entries if base_score(entry) >= min_score]
    asn_counts: Counter[str] = Counter()
    subnet_counts: Counter[str] = Counter()
    ranked: list[dict[str, Any]] = []

    while remaining and len(ranked) < reserve_size:
        def adjusted_score(entry: dict[str, Any]) -> tuple[float, float, str]:
            raw_score = base_score(entry)
            diversity_penalty = 1.0
            candidate_asn = asn_key(entry)
            candidate_subnet = subnet_key(entry)
            if candidate_asn:
                diversity_penalty += asn_counts[candidate_asn]
            if candidate_subnet:
                diversity_penalty += subnet_counts[candidate_subnet]
            return raw_score / diversity_penalty, raw_score, str(entry.get("address", ""))

        selected = max(remaining, key=adjusted_score)
        remaining.remove(selected)
        final_score, raw_score, _ = adjusted_score(selected)
        selected["base_score"] = round(raw_score, 6)
        selected["score"] = round(final_score, 6)
        ranked.append(selected)

        selected_asn = asn_key(selected)
        selected_subnet = subnet_key(selected)
        if selected_asn:
            asn_counts[selected_asn] += 1
        if selected_subnet:
            subnet_counts[selected_subnet] += 1

    return ranked
