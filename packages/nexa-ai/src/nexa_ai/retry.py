"""When to retry the same model, and how long to wait.

Only errors marked retryable are retried: provider rate limits and provider-side failures.
Timeouts, authentication failures, invalid requests and refusals are never retried against
the same model (the fallback matrix in docs/architecture/ai-gateway.md).
"""

import random
from dataclasses import dataclass

from nexa_ai.errors import AIError


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int = 2
    base_delay: float = 0.5
    max_delay: float = 4.0
    # A provider's Retry-After longer than this is not worth waiting for: try the next model.
    max_retry_after: float = 4.0

    def delay(self, error: AIError, retries_so_far: int) -> float | None:
        """Seconds to wait before retrying, or None when this error should not be retried."""
        if not error.retryable or retries_so_far >= self.max_retries:
            return None
        if error.retry_after_seconds is not None:
            if error.retry_after_seconds > self.max_retry_after:
                return None
            return max(0.0, error.retry_after_seconds)
        # Exponential backoff with full jitter: avoids retry storms when a provider recovers.
        ceiling = min(self.max_delay, self.base_delay * (2**retries_so_far))
        return random.uniform(0, ceiling)  # noqa: S311 - jitter, not cryptography
