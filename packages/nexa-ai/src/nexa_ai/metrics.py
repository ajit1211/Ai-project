"""In-process counters for the AI gateway (architecture doc 23.3).

Deliberately small: per-process counters and latency buckets, exposed as a snapshot so a
metrics endpoint or an OpenTelemetry exporter (Phase 17) can read them. The usage ledger in
PostgreSQL, not these counters, is the source of truth for tokens and cost.
"""

import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

LATENCY_BUCKETS_MS = (250, 500, 1000, 2500, 5000, 10000, 30000, 60000)


@dataclass
class GatewayMetrics:
    requests: Counter[tuple[str, str, str, str]] = field(default_factory=Counter)
    input_tokens: Counter[tuple[str, str]] = field(default_factory=Counter)
    output_tokens: Counter[tuple[str, str]] = field(default_factory=Counter)
    cost_micros: Counter[tuple[str, str]] = field(default_factory=Counter)
    attempts_failed: Counter[tuple[str, str, str]] = field(default_factory=Counter)
    retries: Counter[tuple[str, str]] = field(default_factory=Counter)
    fallbacks: Counter[str] = field(default_factory=Counter)
    latency: Counter[tuple[str, str, int]] = field(default_factory=Counter)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def observe_call(
        self,
        *,
        alias: str,
        provider: str,
        model: str,
        status: str,
        error_code: str | None,
        input_tokens: int,
        output_tokens: int,
        cost_micros: int,
        latency_ms: int,
        fallback_used: bool,
    ) -> None:
        bucket = next((b for b in LATENCY_BUCKETS_MS if latency_ms <= b), -1)
        with self._lock:
            self.requests[(alias, provider, model, error_code or status)] += 1
            self.input_tokens[(provider, model)] += input_tokens
            self.output_tokens[(provider, model)] += output_tokens
            self.cost_micros[(provider, model)] += cost_micros
            self.latency[(provider, model, bucket)] += 1
            if fallback_used:
                self.fallbacks[alias] += 1

    def observe_attempt_failure(self, *, provider: str, model: str, error_code: str) -> None:
        with self._lock:
            self.attempts_failed[(provider, model, error_code)] += 1

    def observe_retry(self, *, provider: str, model: str) -> None:
        with self._lock:
            self.retries[(provider, model)] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total = sum(self.requests.values())
            succeeded = sum(n for key, n in self.requests.items() if key[3] == "success")
            return {
                "requests_total": total,
                "success_rate": round(succeeded / total, 4) if total else None,
                "requests": [
                    {"alias": a, "provider": p, "model": m, "outcome": o, "count": n}
                    for (a, p, m, o), n in sorted(self.requests.items())
                ],
                "tokens": [
                    {
                        "provider": p,
                        "model": m,
                        "input": self.input_tokens[(p, m)],
                        "output": self.output_tokens[(p, m)],
                        "cost_micros": self.cost_micros[(p, m)],
                    }
                    for (p, m) in sorted(set(self.input_tokens) | set(self.output_tokens))
                ],
                "failed_attempts": [
                    {"provider": p, "model": m, "error_code": c, "count": n}
                    for (p, m, c), n in sorted(self.attempts_failed.items())
                ],
                "retries": [
                    {"provider": p, "model": m, "count": n}
                    for (p, m), n in sorted(self.retries.items())
                ],
                "fallbacks": dict(sorted(self.fallbacks.items())),
                "latency_ms_buckets": [
                    {"provider": p, "model": m, "le": b if b >= 0 else "inf", "count": n}
                    for (p, m, b), n in sorted(self.latency.items())
                ],
            }
