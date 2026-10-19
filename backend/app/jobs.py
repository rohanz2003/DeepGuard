"""Bounded in-process scan jobs.

The worker boundary is deliberately small and local: a bounded
``ThreadPoolExecutor`` receives validated audio metadata, updates the scan
record as it progresses, and releases the in-memory audio reference when the
job finishes.  This is not a durable cross-process queue; deployments with
multiple processes have one independent job manager per process.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from datetime import datetime
from typing import Any, Callable

from .firebase import utc_now
from .inference import TransientInferenceError


logger = logging.getLogger(__name__)


class JobQueueFull(RuntimeError):
    """Raised when the bounded in-process job capacity is exhausted."""


class ScanJobManager:
    """Submit each scan at most once and persist explicit worker states."""

    _FAILURE_EXPLANATIONS = {
        "inference_failed": "The voice analysis could not be completed.",
        "invalid_inference_result": "The voice analysis returned no usable result.",
        "transient_inference_exhausted": (
            "The voice analysis service was temporarily unavailable after the allowed retries."
        ),
    }

    def __init__(
        self,
        repository: Any | Callable[[], Any],
        inference: Any,
        *,
        executor: Any | None = None,
        max_workers: int = 2,
        max_queue: int = 8,
        max_attempts: int = 2,
        clock: Callable[[], datetime] = utc_now,
    ):
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if max_queue < 0:
            raise ValueError("max_queue cannot be negative")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        self.max_workers = max_workers
        self.max_queue = max_queue
        self.max_attempts = max_attempts
        self._repository_provider = (
            repository if callable(repository) else lambda: repository
        )
        self._inference = inference
        self._clock = clock
        self._executor = (
            executor
            if executor is not None
            else ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="deepguard-scan",
            )
        )
        self._owns_executor = executor is None
        self._capacity = threading.BoundedSemaphore(max_workers + max_queue)
        self._lock = threading.RLock()
        self._active: dict[tuple[str, str], Future | None] = {}
        # Keep a process-local admission record after completion as well as
        # while active.  A scan ID is immutable, so a second enqueue for the
        # same record returns the original future instead of running inference
        # again.
        self._submitted: dict[tuple[str, str], Future | None] = {}
        self._closed = False

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def enqueue(self, user_id: str, scan_id: str, metadata: Any) -> Future | Any:
        """Queue one scan, returning its future or the existing duplicate job.

        The key is registered before submission so an injected synchronous or
        deterministic executor cannot race a second submission into duplicate
        inference.
        """

        key = (str(user_id), str(scan_id))
        with self._lock:
            if self._closed:
                raise RuntimeError("The scan job manager is closed.")
            if key in self._submitted:
                existing = self._submitted[key]
                return existing
            if not self._capacity.acquire(blocking=False):
                raise JobQueueFull("The scan job queue is full.")
            self._active[key] = None
            self._submitted[key] = None

        try:
            future = self._executor.submit(
                self._run,
                key,
                metadata,
            )
        except Exception:
            with self._lock:
                self._active.pop(key, None)
                self._submitted.pop(key, None)
            self._capacity.release()
            raise

        # A deterministic test executor may run the callable inline.  Only
        # replace the placeholder when the worker is still active.
        with self._lock:
            if key in self._active:
                self._active[key] = future
            if key in self._submitted:
                self._submitted[key] = future
        return future

    # ``submit`` makes the manager convenient to inject into small adapters
    # that already use executor terminology.
    submit = enqueue

    def _repository(self):
        return self._repository_provider()

    def _update(
        self,
        repository: Any,
        user_id: str,
        scan_id: str,
        updates: dict[str, Any],
    ) -> bool:
        update = getattr(repository, "update", None)
        if not callable(update):
            logger.error("Scan repository does not implement update().")
            return False
        try:
            result = update(user_id, scan_id, updates)
            if result is False:
                return False
            if result is not None:
                return True
            # Adapters that use a write-style ``update`` and return no value
            # are still valid.  Read back when possible so a missing/deleted
            # owner-scoped record cannot receive inference updates.
            read = getattr(repository, "get", None)
            if not callable(read):
                return True
            try:
                return read(user_id, scan_id, self._clock()) is not None
            except TypeError:
                return read(user_id, scan_id) is not None
        except Exception:
            # Provider details and exception text must not become scan output.
            logger.warning("Scan status update failed (%s)", type(updates).__name__)
            return False

    def _processing_updates(self, attempt: int) -> dict[str, Any]:
        return {
            "status": "processing",
            "attempt": attempt,
            "retry_count": attempt - 1,
            "max_attempts": self.max_attempts,
            "updated_at": self._clock().isoformat(),
        }

    def _pending_retry_updates(self, attempt: int) -> dict[str, Any]:
        return {
            "status": "pending",
            "attempt": attempt,
            "retry_count": attempt,
            "max_attempts": self.max_attempts,
            "updated_at": self._clock().isoformat(),
        }

    def _completed_updates(self, result: Any, attempt: int) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise ValueError("Inference returned an invalid result.")
        return {
            # A callable inference service may report model availability as
            # "unavailable".  The worker still completed that request; the
            # independent deepfake_status remains inconclusive in that case.
            "status": "completed",
            "deepfake_status": result.get("deepfake_status") or "inconclusive",
            "confidence": result.get("confidence"),
            "model_version": result.get("model_version") or "not-configured",
            "explanation": result.get("explanation")
            or "Voice analysis completed without a risk score.",
            "attempt": attempt,
            "retry_count": attempt - 1,
            "max_attempts": self.max_attempts,
            "updated_at": self._clock().isoformat(),
        }

    def _failed_updates(
        self,
        code: str,
        attempt: int,
    ) -> dict[str, Any]:
        return {
            "status": "failed",
            "deepfake_status": "inconclusive",
            "confidence": None,
            "explanation": "The scan could not be completed.",
            "error_code": code,
            "error_explanation": self._FAILURE_EXPLANATIONS.get(
                code, "The scan could not be completed."
            ),
            "attempt": attempt,
            "retry_count": attempt - 1,
            "max_attempts": self.max_attempts,
            "updated_at": self._clock().isoformat(),
        }

    def _run(self, key: tuple[str, str], metadata: Any) -> None:
        user_id, scan_id = key
        repository = None
        try:
            try:
                repository = self._repository()
            except Exception:
                logger.warning("Could not resolve scan repository for worker")
                return

            for attempt in range(1, self.max_attempts + 1):
                if not self._update(
                    repository,
                    user_id,
                    scan_id,
                    self._processing_updates(attempt),
                ):
                    # A deleted/expired scan or unavailable repository should
                    # not cause inference to run against an unowned record.
                    return

                try:
                    result = self._inference.analyze(metadata)
                    try:
                        updates = self._completed_updates(result, attempt)
                    except ValueError:
                        self._update(
                            repository,
                            user_id,
                            scan_id,
                            self._failed_updates(
                                "invalid_inference_result", attempt
                            ),
                        )
                        return
                except TransientInferenceError:
                    if attempt < self.max_attempts:
                        if not self._update(
                            repository,
                            user_id,
                            scan_id,
                            self._pending_retry_updates(attempt),
                        ):
                            return
                        continue
                    self._update(
                        repository,
                        user_id,
                        scan_id,
                        self._failed_updates(
                            "transient_inference_exhausted", attempt
                        ),
                    )
                    return
                except Exception:
                    # Only the dedicated transient exception is retryable.
                    self._update(
                        repository,
                        user_id,
                        scan_id,
                        self._failed_updates("inference_failed", attempt),
                    )
                    return

                self._update(repository, user_id, scan_id, updates)
                return
        finally:
            # Drop the worker's last strong reference to the raw audio as soon
            # as the attempt loop ends.  The repository never receives it.
            metadata = None
            with self._lock:
                self._active.pop(key, None)
            self._capacity.release()

    def wait_for_idle(self, timeout: float = 10.0) -> bool:
        """Wait for all currently active jobs; useful for smoke/tests."""

        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                active = [future for future in self._active.values() if future is not None]
                if not self._active:
                    return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for scan jobs.")
            if not active:
                time.sleep(min(0.01, remaining))
                continue
            for future in active:
                future.result(timeout=remaining)

    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
        if self._owns_executor:
            self._executor.shutdown(wait=wait)


__all__ = ["JobQueueFull", "ScanJobManager"]
