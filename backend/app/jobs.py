"""Bounded in-process scan jobs.

The worker boundary is deliberately small and local: a bounded
``ThreadPoolExecutor`` receives validated audio metadata, updates the scan
record as it progresses, and releases the in-memory audio reference when the
job finishes.  This is not a durable cross-process queue; deployments with
multiple processes have one independent job manager per process.
"""

from __future__ import annotations

import logging
import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from datetime import datetime
from typing import Any, Callable

from .firebase import utc_now
from .inference import TransientInferenceError
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
        retry_backoff_seconds: float = 0.0,
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
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
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
        updates = {
            # A callable inference service may report model availability as
            # "unavailable".  The worker still completed that request; the
            # independent deepfake_status remains inconclusive in that case.
            "status": "completed",
            "deepfake_status": result.get("deepfake_status") or "inconclusive",
            "confidence": result.get("confidence"),
            "synthetic_audio_score": result.get("synthetic_audio_score"),
            "calibrated_score": result.get("calibrated_score"),
            "score_type": result.get("score_type") or "uncalibrated_synthetic_audio_score",
            "audio_quality": result.get("audio_quality") or {},
            "segment_scores": result.get("segment_scores") or [],
            "inference_ms": result.get("inference_ms"),
            "model_version": result.get("model_version") or "not-configured",
            "explanation": result.get("explanation")
            or "Voice analysis completed without a risk score.",
            "attempt": attempt,
            "retry_count": attempt - 1,
            "max_attempts": self.max_attempts,
            "updated_at": self._clock().isoformat(),
        }
        # Optional worker-produced transcript signals replace the admission
        # result only when the inference adapter actually generated them. This
        # preserves a manually supplied transcript when a voice adapter does
        # not implement transcription.
        if str(result.get("transcript_source", "")).startswith("local"):
            for field in (
                "scam_status",
                "scam_score",
                "scam_categories",
                "scam_explanation",
                "transcript_language",
                "transcript_source",
            ):
                if field in result:
                    updates[field] = result[field]
        return updates

    def _failed_updates(
        self,
        code: str,
        attempt: int,
    ) -> dict[str, Any]:
        return {
            "status": "failed",
            "deepfake_status": "inconclusive",
            "confidence": None,
            "synthetic_audio_score": None,
            "calibrated_score": None,
            "score_type": "uncalibrated_synthetic_audio_score",
            "audio_quality": {},
            "segment_scores": [],
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

    def process(self, user_id: str, scan_id: str, metadata: Any) -> None:
        """Process a previously admitted job from any queue backend."""

        self._run((str(user_id), str(scan_id)), metadata, release_capacity=False)

    def _run(
        self,
        key: tuple[str, str],
        metadata: Any,
        *,
        release_capacity: bool = True,
    ) -> None:
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
                        if self.retry_backoff_seconds:
                            time.sleep(self.retry_backoff_seconds * (2 ** (attempt - 1)))
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
            if release_capacity:
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


class RedisScanJobManager:
    """Durable Redis-backed admission and worker boundary.

    Audio is placed in the configured audio store with a short-lived private
    pointer before the Redis message is admitted.  The pointer is deleted
    after processing, so a durable queue does not put raw samples in Redis.
    Retained playback uses its separate, user-selected storage pointer.
    Run ``python -m app.queue_worker`` in a dedicated worker process.
    """

    def __init__(
        self,
        repository: Any | Callable[[], Any],
        inference: Any,
        audio_store: Any,
        *,
        redis_url: str,
        queue_name: str = "deepguard-scans",
        max_attempts: int = 2,
        retry_backoff_seconds: float = 2.0,
        clock: Callable[[], datetime] = utc_now,
        job_ttl_seconds: int = 86_400,
    ):
        try:
            import redis
        except ImportError as exc:  # pragma: no cover - deployment path
            raise RuntimeError("Install the redis dependency to enable QUEUE_BACKEND=redis.") from exc
        if not str(redis_url or "").strip():
            raise ValueError("QUEUE_REDIS_URL is required for QUEUE_BACKEND=redis.")
        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self.queue_name = queue_name
        self.processing_name = f"{queue_name}:processing"
        self.dead_letter_name = f"{queue_name}:dead-letter"
        self.audio_store = audio_store
        self.max_attempts = max_attempts
        self.job_ttl_seconds = job_ttl_seconds
        self._processor = ScanJobManager(
            repository,
            inference,
            max_workers=1,
            max_queue=0,
            max_attempts=max_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            clock=clock,
        )

    def _job_key(self, user_id: str, scan_id: str) -> str:
        return f"{self.queue_name}:job:{user_id}:{scan_id}"

    def enqueue(self, user_id: str, scan_id: str, metadata: Any) -> dict[str, str]:
        upload = getattr(self.audio_store, "upload", None)
        if not callable(upload):
            raise RuntimeError("A temporary audio store is required for the durable queue.")
        expires_at = utc_now().replace(microsecond=0)
        from datetime import timedelta

        expires_at += timedelta(seconds=self.job_ttl_seconds)
        temp_id = upload(
            metadata.audio_bytes,
            filename=f"{scan_id}.wav",
            content_type="audio/wav",
            expires_at=expires_at,
        )
        payload = {
            "version": 1,
            "user_id": str(user_id),
            "scan_id": str(scan_id),
            "audio_storage_id": str(temp_id),
            "metadata": {
                "filename": metadata.filename,
                "mime_type": metadata.mime_type,
                "size_bytes": metadata.size_bytes,
                "duration_seconds": metadata.duration_seconds,
                "audio_quality": metadata.audio_quality or {},
                "transcribe_audio": bool(getattr(metadata, "transcribe_audio", False)),
                "transcript_language": getattr(metadata, "transcript_language", "auto"),
            },
        }
        key = self._job_key(user_id, scan_id)
        serialized = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        try:
            admitted = self._redis.set(key, serialized, nx=True, ex=self.job_ttl_seconds)
            if not admitted:
                self._delete_temp(temp_id)
                return {"status": "duplicate", "scan_id": str(scan_id)}
            self._redis.rpush(self.queue_name, serialized)
        except Exception:
            self._redis.delete(key)
            self._delete_temp(temp_id)
            raise
        return {"status": "queued", "scan_id": str(scan_id)}

    def _delete_temp(self, storage_id: str) -> None:
        deleter = getattr(self.audio_store, "delete", None)
        if callable(deleter):
            try:
                deleter(storage_id)
            except Exception:
                logging.getLogger(__name__).warning("temporary audio cleanup failed")

    def _read_temp(self, storage_id: str) -> bytes:
        opener = getattr(self.audio_store, "open", None)
        if not callable(opener):
            raise RuntimeError("temporary audio store cannot open queued audio")
        stream = opener(storage_id)
        try:
            return bytes(stream.read() if hasattr(stream, "read") else stream)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

    def run_once(self, timeout: int = 5) -> bool:
        """Process one message; return False when Redis has no work."""

        serialized = self._redis.brpoplpush(
            self.queue_name,
            self.processing_name,
            timeout=timeout,
        )
        if not serialized:
            return False
        try:
            payload = json.loads(serialized)
            if payload.get("version") != 1:
                raise ValueError("unsupported queue payload")
            metadata_payload = payload["metadata"]
            audio = self._read_temp(payload["audio_storage_id"])
            metadata = AudioMetadata(
                filename=metadata_payload["filename"],
                mime_type=metadata_payload["mime_type"],
                size_bytes=int(metadata_payload["size_bytes"]),
                duration_seconds=float(metadata_payload["duration_seconds"]),
                audio_bytes=audio,
                original_bytes=b"",
                audio_quality=metadata_payload.get("audio_quality") or {},
                transcribe_audio=bool(metadata_payload.get("transcribe_audio", False)),
                transcript_language=metadata_payload.get("transcript_language", "auto"),
            )
            self._processor.process(payload["user_id"], payload["scan_id"], metadata)
            self._redis.delete(self._job_key(payload["user_id"], payload["scan_id"]))
        except Exception as exc:
            # The dead-letter record contains identifiers and a safe exception
            # class only; it never contains audio or provider details.
            try:
                dead = {
                    "version": 1,
                    "user_id": payload.get("user_id", "unknown"),
                    "scan_id": payload.get("scan_id", "unknown"),
                    "error": type(exc).__name__,
                    "queued_at": utc_now().isoformat(),
                }
            except (UnboundLocalError, AttributeError):
                dead = {"version": 1, "error": "invalid_payload", "queued_at": utc_now().isoformat()}
            self._redis.rpush(self.dead_letter_name, json.dumps(dead, separators=(",", ":")))
            if "payload" in locals() and payload.get("audio_storage_id"):
                self._delete_temp(payload["audio_storage_id"])
            return True
        finally:
            try:
                self._redis.lrem(self.processing_name, 1, serialized)
            except Exception:
                logging.getLogger(__name__).warning("durable queue acknowledgement failed")
            if "payload" in locals() and payload.get("audio_storage_id"):
                self._delete_temp(payload["audio_storage_id"])
        return True

    def recover_inflight(self) -> int:
        """Requeue messages left in the processing list after a worker crash."""

        lock_key = f"{self.queue_name}:recovery-lock"
        if not self._redis.set(lock_key, "1", nx=True, ex=30):
            return 0
        recovered = 0
        try:
            for serialized in self._redis.lrange(self.processing_name, 0, -1):
                self._redis.lrem(self.processing_name, 1, serialized)
                self._redis.rpush(self.queue_name, serialized)
                recovered += 1
        finally:
            self._redis.delete(lock_key)
        return recovered

    def run_forever(self, *, timeout: int = 5, stop_event: Any | None = None) -> None:
        while stop_event is None or not stop_event.is_set():
            self.run_once(timeout=timeout)

    def readiness(self) -> dict[str, Any]:
        try:
            queue_depth = int(self._redis.llen(self.queue_name))
            processing_depth = int(self._redis.llen(self.processing_name))
            dead_letters = int(self._redis.llen(self.dead_letter_name))
            return {
                "status": "ready",
                "backend": "redis",
                "queue": self.queue_name,
                "queue_depth": queue_depth,
                "processing_depth": processing_depth,
                "dead_letter_depth": dead_letters,
            }
        except Exception:
            return {"status": "unavailable", "backend": "redis", "queue": self.queue_name}

    def shutdown(self, wait: bool = True) -> None:
        self._processor.shutdown(wait=wait)


__all__ = ["JobQueueFull", "RedisScanJobManager", "ScanJobManager"]
