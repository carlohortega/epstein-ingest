"""A small thread-safe rate limiter for the realtime path (Handoff §8).

Bounds client-side requests-per-minute and tokens-per-minute against a deployment's quota using two
sliding 60-second windows. ``acquire`` blocks the calling worker thread until both windows admit the
request, so a thread pool of ``concurrency`` workers never exceeds the configured RPM/TPM. When both
limits are 0 it is a no-op (rely purely on server 429 + backoff).

Deliberately simple (stdlib only — the venv has no token-bucket library): a lock + two deques of
``(timestamp, amount)`` events pruned to the trailing minute.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    def __init__(self, requests_per_minute: int = 0, tokens_per_minute: int = 0) -> None:
        self._rpm = max(0, int(requests_per_minute))
        self._tpm = max(0, int(tokens_per_minute))
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._reqs: deque[float] = deque()              # request timestamps
        self._toks: deque[tuple[float, int]] = deque()  # (timestamp, token_estimate)
        self._tok_sum = 0

    @property
    def enabled(self) -> bool:
        return self._rpm > 0 or self._tpm > 0

    def _prune(self, now: float) -> None:
        window = now - 60.0
        while self._reqs and self._reqs[0] <= window:
            self._reqs.popleft()
        while self._toks and self._toks[0][0] <= window:
            self._tok_sum -= self._toks.popleft()[1]

    def acquire(self, est_tokens: int = 0) -> None:
        """Block until a request of ~est_tokens fits both windows, then record it."""
        if not self.enabled:
            return
        est_tokens = max(0, int(est_tokens))
        with self._cond:
            while True:
                now = time.monotonic()
                self._prune(now)
                req_ok = self._rpm == 0 or len(self._reqs) < self._rpm
                # admit if under the TPM cap, OR the window is empty (a single oversized request still
                # has to go through eventually rather than deadlock).
                tok_ok = (self._tpm == 0 or self._tok_sum + est_tokens <= self._tpm
                          or self._tok_sum == 0)
                if req_ok and tok_ok:
                    self._reqs.append(now)
                    if est_tokens:
                        self._toks.append((now, est_tokens))
                        self._tok_sum += est_tokens
                    return
                # wait until the oldest event ages out of the window (or another thread frees room)
                oldest = self._reqs[0] if self._reqs else now
                if self._toks:
                    oldest = min(oldest, self._toks[0][0])
                self._cond.wait(timeout=max(0.01, 60.0 - (now - oldest)))
