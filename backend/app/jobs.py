"""Scan job boundaries.

``ScanJobManager`` remains a bounded in-process adapter for existing injected
unit-test fixtures.  Production admission uses ``DurableScanQueue`` and a
separate ``DurableScanWorker`` process backed by Firestore leases and private
temporary Cloud Storage objects.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable
from uuid import uuid4

from .firebase import utc_now
from .inference import TransientInferenceError
from .storage import StorageUnavailable
from .validation import AudioMetadata


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


@dataclass(frozen=True)
class ClaimedScan:
    """Worker lease returned by a durable repository."""

    user_id: str
    scan_id: str
    lease_id: str
    attempt: int
    record: dict[str, Any]

    @classmethod
    def from_value(cls, value: Any) -> "ClaimedScan":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ValueError("The scan repository returned an invalid claim.")
        record = value.get("record") or {}
        return cls(
            user_id=str(value["user_id"]),
            scan_id=str(value["scan_id"]),
            lease_id=str(value["lease_id"]),
            attempt=int(value.get("attempt") or record.get("attempt") or 1),
            record=dict(record),
        )


class DurableScanQueue:
    """Admit validated audio into Firestore plus a private temp object.

    This class intentionally does not execute inference.  A separate worker
    process claims the Firestore record later, which keeps admission durable
    across API restarts and independently scaled API instances.
    """

    def __init__(
        self,
        repository: Any | Callable[[], Any],
        storage: Any | Callable[[], Any],
        *,
        raw_retention_seconds: int = 3600,
        max_attempts: int = 2,
        clock: Callable[[], datetime] = utc_now,
    ):
        if raw_retention_seconds < 1:
            raise ValueError("raw_retention_seconds must be at least 1")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._repository_provider = repository if callable(repository) else lambda: repository
        self._storage_provider = storage if callable(storage) else lambda: storage
        self.raw_retention_seconds = int(raw_retention_seconds)
        self.max_attempts = int(max_attempts)
        self._clock = clock

    def _repository(self):
        return self._repository_provider()

    def _storage(self):
        return self._storage_provider()

    @staticmethod
    def _object_name(scan_id: str) -> str:
        # Scan IDs are UUIDs generated by the API.  Keeping the path free of a
        # user identifier avoids putting identity data into object listings.
        return f"deepguard/temporary/{scan_id}.wav"

    def admit(self, user_id: str, scan: dict, metadata: AudioMetadata) -> dict:
        """Upload raw WAV then atomically admit its metadata/job record."""

        created = _coerce_time(scan.get("created_at"), self._clock())
        object_expires = created + timedelta(seconds=self.raw_retention_seconds)
        object_name = self._object_name(str(scan["id"]))
        storage = self._storage()
        try:
            uploaded_name = storage.upload(
                object_name,
                metadata.audio_bytes,
                content_type=metadata.mime_type,
                expires_at=object_expires,
            )
        except Exception as exc:
            if isinstance(exc, StorageUnavailable):
                raise
            raise StorageUnavailable("Cloud Storage is unavailable.") from exc

        admitted = dict(scan)
        admitted.update(
            {
                "raw_audio_path": str(uploaded_name or object_name),
                "raw_audio_expires_at": object_expires.isoformat(),
                "cleanup_required": True,
                "next_attempt_at": created.isoformat(),
                "lease_id": None,
                "lease_expires_at": None,
                "worker_id": None,
            }
        )
        try:
            stored = self._repository().create(str(user_id), admitted)
            if stored is False:
                raise RuntimeError("The scan repository rejected admission.")
        except Exception:
            self._best_effort_delete(admitted["raw_audio_path"])
            raise
        return stored or dict(admitted)

    def _best_effort_delete(self, object_name: str | None) -> None:
        if not object_name:
            return
        try:
            self._storage().delete(object_name)
        except Exception:
            logger.warning("Could not clean up temporary scan object")

    def delete(self, user_id: str, scan_id: str) -> bool:
        """Delete an owner-scoped scan and its temporary object."""

        repository = self._repository()
        getter = getattr(repository, "get_internal", None)
        record = None
        if callable(getter):
            record = getter(str(user_id), str(scan_id), self._clock())
        if record is None:
            getter = getattr(repository, "get", None)
            if callable(getter):
                try:
                    record = getter(str(user_id), str(scan_id), self._clock())
                except TypeError:
                    record = getter(str(user_id), str(scan_id))
        if record is None:
            return False
        object_name = record.get("raw_audio_path") if isinstance(record, dict) else None
        if object_name:
            # Keep the Firestore record if object deletion is unavailable so a
            # later worker cleanup pass still has the authoritative path.
            self._storage().delete(object_name)
        return bool(repository.delete(str(user_id), str(scan_id)))

    def create_worker(
        self,
        inference: Any,
        *,
        worker_id: str | None = None,
        lease_seconds: int = 300,
        poll_interval_seconds: float = 5,
    ) -> "DurableScanWorker":
        return DurableScanWorker(
            self._repository_provider,
            self._storage_provider,
            inference,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            max_attempts=self.max_attempts,
            poll_interval_seconds=poll_interval_seconds,
            clock=self._clock,
        )


class DurableScanWorker:
    """Claim, process, recover, and clean durable scan jobs."""

    _FAILURE_EXPLANATIONS = {
        "inference_failed": "The voice analysis could not be completed.",
        "invalid_inference_result": "The voice analysis returned no usable result.",
        "transient_inference_exhausted": (
            "The voice analysis service was temporarily unavailable after the allowed retries."
        ),
        "raw_audio_unavailable": "The temporary audio object was unavailable.",
    }

    def __init__(
        self,
        repository: Any | Callable[[], Any],
        storage: Any | Callable[[], Any],
        inference: Any,
        *,
        worker_id: str | None = None,
        lease_seconds: int = 300,
        max_attempts: int = 2,
        poll_interval_seconds: float = 5,
        clock: Callable[[], datetime] = utc_now,
    ):
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be at least 1")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._repository_provider = repository if callable(repository) else lambda: repository
        self._storage_provider = storage if callable(storage) else lambda: storage
        self._inference = inference
        self.worker_id = str(worker_id or uuid4().hex)
        self.lease_seconds = int(lease_seconds)
        self.max_attempts = int(max_attempts)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self._clock = clock

    def _repository(self):
        return self._repository_provider()

    def _storage(self):
        return self._storage_provider()

    def _claim(self) -> ClaimedScan | None:
        repository = self._repository()
        claim_method = getattr(repository, "claim_next", None) or getattr(
            repository, "claim_pending", None
        )
        if not callable(claim_method):
            raise RuntimeError("Durable scan repository does not implement a claim method.")
        try:
            claim = claim_method(
                self.worker_id,
                self.lease_seconds,
                self._clock(),
            )
        except TypeError:
            # Keep the boundary easy to fake in small unit tests that use a
            # repository with an implicit clock.
            claim = claim_method(self.worker_id, self.lease_seconds)
        return None if claim is None else ClaimedScan.from_value(claim)

    @staticmethod
    def _record_metadata(claim: ClaimedScan, audio_bytes: bytes) -> AudioMetadata:
        record = claim.record
        return AudioMetadata(
            filename=str(record.get("filename") or "audio.wav"),
            mime_type=str(record.get("mime_type") or "audio/wav"),
            size_bytes=int(record.get("size_bytes") or len(audio_bytes)),
            duration_seconds=float(record.get("duration_seconds") or 0),
            audio_bytes=audio_bytes,
        )

    def _transition(self, claim: ClaimedScan, updates: dict[str, Any]) -> bool:
        repository = self._repository()
        transition = getattr(repository, "transition_claim", None) or getattr(
            repository, "update_claim", None
        )
        if not callable(transition):
            logger.error("Durable scan repository does not implement transition_claim()")
            return False
        try:
            try:
                result = transition(
                    claim.user_id,
                    claim.scan_id,
                    claim.lease_id,
                    updates,
                    self._clock(),
                )
            except TypeError:
                result = transition(
                    claim.user_id,
                    claim.scan_id,
                    claim.lease_id,
                    updates,
                )
            return result is not False
        except Exception:
            # A stale lease or provider failure must never expose provider
            # details or make a worker update an unowned/newer claim.
            logger.warning("Durable scan transition failed")
            return False

    def _terminal_updates(
        self,
        claim: ClaimedScan,
        *,
        code: str,
        explanation: str | None = None,
    ) -> dict[str, Any]:
        max_attempts = int(claim.record.get("max_attempts") or self.max_attempts)
        return {
            "status": "failed",
            "deepfake_status": "inconclusive",
            "confidence": None,
            "explanation": "The scan could not be completed.",
            "error_code": code,
            "error_explanation": explanation
            or self._FAILURE_EXPLANATIONS.get(code, "The scan could not be completed."),
            "attempt": claim.attempt,
            "retry_count": max(0, claim.attempt - 1),
            "max_attempts": max_attempts,
            "updated_at": self._clock().isoformat(),
            "lease_id": None,
            "lease_expires_at": None,
            "worker_id": None,
        }

    def _completed_updates(self, claim: ClaimedScan, result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise ValueError("Inference returned an invalid result.")
        max_attempts = int(claim.record.get("max_attempts") or self.max_attempts)
        return {
            "status": "completed",
            "deepfake_status": result.get("deepfake_status") or "inconclusive",
            "confidence": result.get("confidence"),
            "model_version": result.get("model_version") or "not-configured",
            "explanation": result.get("explanation")
            or "Voice analysis completed without a risk score.",
            "attempt": claim.attempt,
            "retry_count": max(0, claim.attempt - 1),
            "max_attempts": max_attempts,
            "updated_at": self._clock().isoformat(),
            "lease_id": None,
            "lease_expires_at": None,
            "worker_id": None,
        }

    def _cleanup(self, claim: ClaimedScan) -> bool:
        object_name = claim.record.get("raw_audio_path")
        if not object_name:
            return True
        try:
            self._storage().delete(object_name)
        except Exception:
            logger.warning("Could not delete terminal scan object")
            return False
        marker = getattr(self._repository(), "mark_cleanup_complete", None)
        if callable(marker):
            try:
                marker_result = marker(claim.user_id, claim.scan_id, object_name)
                return marker_result is not False
            except Exception:
                logger.warning("Could not clear terminal scan cleanup marker")
                return False
        return True

    def _cleanup_pending_objects(self) -> bool:
        repository = self._repository()
        listing = getattr(repository, "list_cleanup_candidates", None)
        if not callable(listing):
            return False
        try:
            candidates = listing(self._clock()) or []
        except Exception:
            logger.warning("Could not list terminal scan cleanup candidates")
            return False
        cleaned = False
        for candidate in candidates:
            try:
                claim = ClaimedScan(
                    user_id=str(candidate["user_id"]),
                    scan_id=str(candidate["scan_id"]),
                    lease_id="cleanup",
                    attempt=int(candidate.get("record", {}).get("attempt") or 0),
                    record=dict(candidate.get("record") or {}),
                )
                if self._cleanup(claim):
                    cleaned = True
            except Exception:
                logger.warning("Could not clean terminal scan object")
        return cleaned

    def process_claim(self, claim_value: Any) -> bool:
        """Process one already-held lease; return whether it stayed current."""

        claim = ClaimedScan.from_value(claim_value)
        object_name = claim.record.get("raw_audio_path")
        try:
            max_attempts = int(claim.record.get("max_attempts") or self.max_attempts)
            if claim.attempt > max_attempts:
                transitioned = self._transition(
                    claim,
                    self._terminal_updates(
                        claim, code="transient_inference_exhausted"
                    ),
                )
                if transitioned:
                    self._cleanup(claim)
                return transitioned
            if not object_name:
                transitioned = self._transition(
                    claim, self._terminal_updates(claim, code="raw_audio_unavailable")
                )
                if transitioned:
                    self._cleanup(claim)
                return transitioned
            try:
                audio_bytes = self._storage().download(object_name)
            except Exception:
                transitioned = self._transition(
                    claim, self._terminal_updates(claim, code="raw_audio_unavailable")
                )
                if transitioned:
                    self._cleanup(claim)
                return transitioned

            metadata = self._record_metadata(claim, audio_bytes)
            try:
                result = self._inference.analyze(metadata)
                updates = self._completed_updates(claim, result)
            except ValueError:
                updates = self._terminal_updates(claim, code="invalid_inference_result")
            except TransientInferenceError:
                if claim.attempt < int(
                    claim.record.get("max_attempts") or self.max_attempts
                ):
                    updates = {
                        "status": "pending",
                        "attempt": claim.attempt,
                        "retry_count": claim.attempt,
                        "max_attempts": int(
                            claim.record.get("max_attempts") or self.max_attempts
                        ),
                        "next_attempt_at": self._clock().isoformat(),
                        "updated_at": self._clock().isoformat(),
                        "lease_id": None,
                        "lease_expires_at": None,
                        "worker_id": None,
                    }
                    return self._transition(claim, updates)
                updates = self._terminal_updates(
                    claim, code="transient_inference_exhausted"
                )
            except Exception:
                # Only TransientInferenceError is retryable.  This includes
                # ordinary model/provider exceptions and protects secrets from
                # entering scan metadata.
                updates = self._terminal_updates(claim, code="inference_failed")

            transitioned = self._transition(claim, updates)
            if transitioned and updates.get("status") in {"completed", "failed"}:
                self._cleanup(claim)
            return transitioned
        except Exception:
            # Keep the worker loop alive if an injected adapter is malformed.
            logger.warning("Durable scan processing failed")
            return False

    def run_once(self) -> bool:
        """Claim and process one job, including expired-lease recovery."""

        try:
            claim = self._claim()
        except Exception:
            logger.warning("Could not claim durable scan job")
            return False
        if claim is None:
            return self._cleanup_pending_objects()
        self.process_claim(claim)
        return True

    def run_forever(self, stop_event=None) -> None:
        """Poll until ``stop_event`` is set (or forever when it is omitted)."""

        while stop_event is None or not stop_event.is_set():
            did_work = self.run_once()
            if did_work:
                continue
            if stop_event is not None and stop_event.wait(self.poll_interval_seconds):
                return
            if stop_event is None:
                time.sleep(self.poll_interval_seconds)


def _coerce_time(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=fallback.tzinfo)
    if isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=fallback.tzinfo)
    return fallback


__all__ = [
    "ClaimedScan",
    "DurableScanQueue",
    "DurableScanWorker",
    "JobQueueFull",
    "ScanJobManager",
]
