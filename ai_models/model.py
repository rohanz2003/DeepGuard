"""NumPy-only logistic regression and versioned, provenance-bearing JSON artifacts."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

import numpy as np

from .preprocess import feature_contract

ARTIFACT_TYPE = "audio_binary_logistic_regression"
ARTIFACT_VERSION = "1.1.0"
MODEL_VERSION = "research-baseline-1"
LABEL_MAPPING = {"real": 0, "fake": 1}
PCM_HASH_STRATEGY = "mono-16000hz-float64le-v1"
GROUPING_VERSIONS = {"content": "content-v1", "filename": "filename-v1"}
_PARAMETER_KEYS = {
    "artifact_type", "artifact_version", "threshold", "feature_contract",
    "preprocessing", "weights", "bias", "feature_mean", "feature_scale",
}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ModelError(ValueError):
    """Raised for invalid training data or model artifacts."""


def fingerprint_payload(payload: Any) -> str:
    """Hash a canonical finite JSON value, independent of dictionary insertion order."""

    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def fingerprint_records(records: Iterable[tuple[str, int, bytes]]) -> str:
    """Compatibility helper for hashing paths, labels, and bounded audio bytes."""

    return fingerprint_payload([
        {"path": name.replace("\\", "/"), "label": label,
         "sha256": hashlib.sha256(audio).hexdigest()}
        for name, label, audio in sorted(records, key=lambda item: item[0])
    ])


def dataset_fingerprint(records: Sequence[dict[str, Any]], manifest_sha256: str | None) -> str:
    return fingerprint_payload({
        "pcm_hash_strategy": PCM_HASH_STRATEGY,
        "manifest_sha256": manifest_sha256,
        "records": sorted(records, key=lambda item: item["relative_path"]),
    })


def membership_fingerprint(hashes: Sequence[str], labels: dict[str, int]) -> str:
    return fingerprint_payload([
        {"pcm_sha256": value, "label": labels[value]} for value in sorted(hashes)
    ])


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _relative_path(value: Any) -> bool:
    return (
        isinstance(value, str) and bool(value) and "\\" not in value
        and ":" not in value and "\x00" not in value
        and not PurePosixPath(value).is_absolute()
        and all(part not in ("", ".", "..") for part in value.split("/"))
    )


def _class_counts(hashes: Sequence[str], labels: dict[str, int]) -> dict[str, int]:
    return {name: sum(labels[value] == label for value in hashes)
            for name, label in LABEL_MAPPING.items()}


def validate_training_metadata(metadata: dict[str, Any]) -> None:
    """Validate saved membership and its invariants before trusting an artifact."""

    try:
        json.dumps(metadata, allow_nan=False)
        if (metadata["label_mapping"] != LABEL_MAPPING
                or any(type(value) is not int for value in metadata["label_mapping"].values())):
            raise ModelError("artifact label mapping is incompatible")
        version = metadata["model_version"]
        if not isinstance(version, str) or not version.startswith(MODEL_VERSION + "-"):
            raise ModelError("artifact model version is missing or incompatible")
        trained_at = datetime.fromisoformat(metadata["trained_at_utc"].replace("Z", "+00:00"))
        if trained_at.tzinfo is None or trained_at.utcoffset() != timezone.utc.utcoffset(trained_at):
            raise ModelError("artifact training time must be UTC")
        training = metadata["training"]
        if training["pcm_hash_strategy"] != PCM_HASH_STRATEGY:
            raise ModelError("artifact PCM identity strategy is incompatible")
        records = training["dataset_records"]
        if not isinstance(records, list) or not records:
            raise ModelError("artifact dataset provenance is missing")
        labels: dict[str, int] = {}
        paths: set[str] = set()
        raw_counts = {"real": 0, "fake": 0}
        for record in records:
            path, label = record["relative_path"], record["label"]
            pcm_hash, wav_hash = record["pcm_sha256"], record["wav_sha256"]
            if (not _relative_path(path) or path in paths
                    or type(label) is not int or label not in (0, 1)
                    or path.split("/")[0] != ("real" if label == 0 else "fake")
                    or not _is_hash(pcm_hash) or not _is_hash(wav_hash)
                    or not _number(record["duration_seconds"]) or record["duration_seconds"] <= 0):
                raise ModelError("artifact dataset record is malformed")
            if pcm_hash in labels and labels[pcm_hash] != label:
                raise ModelError("artifact contains conflicting decoded PCM labels")
            paths.add(path)
            labels[pcm_hash] = label
            raw_counts["real" if label == 0 else "fake"] += 1
            if not isinstance(record["sources"], list) or not record["sources"]:
                raise ModelError("artifact source provenance is missing")
            for source in record["sources"]:
                if (not _relative_path(source["source"]) or source["label"] != label
                        or type(source["label"]) is not int
                        or source["source"].split("/")[0] != ("real" if label == 0 else "fake")
                        or not _is_hash(source["source_sha256"])
                        or not _is_hash(source["pcm_sha256"])
                        or not _number(source["duration_seconds"])
                        or source["duration_seconds"] <= 0):
                    raise ModelError("artifact source provenance is malformed")
            aliases = record["source_aliases"]
            if (not isinstance(aliases, list) or len(set(aliases)) != len(aliases)
                    or any(not _relative_path(alias)
                           or alias.split("/")[0] != ("real" if label == 0 else "fake")
                           for alias in aliases)):
                raise ModelError("artifact duplicate source aliases are malformed")
        manifest_hash = training["manifest_sha256"]
        if manifest_hash is not None and not _is_hash(manifest_hash):
            raise ModelError("artifact manifest fingerprint is malformed")
        fingerprint = dataset_fingerprint(records, manifest_hash)
        if (metadata["training_data_fingerprint"] != fingerprint
                or training["data_fingerprint"] != fingerprint):
            raise ModelError("artifact dataset fingerprint does not match its provenance")
        split = training["split"]
        seed, fraction, group_by = split["seed"], split["test_size"], split["group_by"]
        if (type(seed) is not int or seed < 0 or not _number(fraction) or not 0 < fraction < 1
                or group_by not in GROUPING_VERSIONS
                or split["grouping_version"] != GROUPING_VERSIONS[group_by]
                or split["strategy"] != "group-stratified-greedy-v1"):
            raise ModelError("artifact split configuration is incompatible")
        if (any(training[key] != split[key] for key in ("seed", "test_size", "group_by"))
                or type(training["seed"]) is not int or not _number(training["test_size"])):
            raise ModelError("artifact split configuration is inconsistent")
        for key in ("sample_count", "unique_sample_count", "duplicate_count", "group_count",
                    "train_count", "test_count", "train_group_count", "test_group_count"):
            if type(training[key]) is not int or training[key] < 0:
                raise ModelError("artifact counts must be non-negative integers")
        for key in ("class_counts", "unique_class_counts", "train_class_counts", "test_class_counts"):
            values = training[key]
            if (not isinstance(values, dict) or set(values) != set(LABEL_MAPPING)
                    or any(type(value) is not int or value < 0 for value in values.values())):
                raise ModelError("artifact class counts must be non-negative integers")
        train_hashes, test_hashes = split["train_pcm_sha256"], split["test_pcm_sha256"]
        for membership in (train_hashes, test_hashes):
            if (not isinstance(membership, list) or not membership
                    or not all(_is_hash(value) for value in membership)
                    or len(set(membership)) != len(membership)):
                raise ModelError("artifact split membership is malformed")
        if set(train_hashes) & set(test_hashes) or set(train_hashes + test_hashes) != set(labels):
            raise ModelError("artifact train/test PCM membership overlaps or is incomplete")
        groups = split["group_ids"]
        if (not isinstance(groups, dict) or set(groups) != set(labels)
                or not all(_is_hash(group) for group in groups.values())):
            raise ModelError("artifact group membership is malformed")
        train_groups = {groups[value] for value in train_hashes}
        test_groups = {groups[value] for value in test_hashes}
        if train_groups & test_groups:
            raise ModelError("artifact train/test groups overlap")
        for name, hashes, group_set in (
            ("train", train_hashes, train_groups), ("test", test_hashes, test_groups),
        ):
            counts = _class_counts(hashes, labels)
            if min(counts.values()) < 1:
                raise ModelError("artifact train and held-out sets must both contain both classes")
            if (training[name + "_count"] != len(hashes)
                    or training[name + "_class_counts"] != counts
                    or training[name + "_group_count"] != len(group_set)
                    or split[name + "_fingerprint"] != membership_fingerprint(hashes, labels)):
                raise ModelError("artifact split counts or fingerprint are inconsistent")
        if (training["sample_count"] != len(records)
                or training["unique_sample_count"] != len(labels)
                or training["duplicate_count"] != len(records) - len(labels)
                or training["class_counts"] != raw_counts
                or training["unique_class_counts"] != _class_counts(list(labels), labels)
                or training["group_count"] != len(train_groups | test_groups)):
            raise ModelError("artifact dataset counts are inconsistent")
        grouping = training["grouping"]
        if (grouping["strategy"] != group_by or grouping["version"] != GROUPING_VERSIONS[group_by]
                or not isinstance(grouping["method_counts"], dict)
                or any(type(value) is not int or value < 0 for value in grouping["method_counts"].values())
                or not isinstance(grouping["heuristic_limitations"], list)
                or not all(isinstance(value, str) for value in grouping["heuristic_limitations"])):
            raise ModelError("artifact grouping metadata is inconsistent")
        preparation_counts = training["preparation_counts"]
        if (not isinstance(preparation_counts, dict)
                or any(type(value) is not int or value < 0 for value in preparation_counts.values())):
            raise ModelError("artifact preparation counts are malformed")
    except ModelError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise ModelError("artifact training provenance is missing or malformed") from exc


def _sigmoid(values: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(values, -60.0, 60.0)))


class LogisticRegressionModel:
    """Small full-batch binary logistic regression; filenames never enter features."""

    def __init__(
        self, weights: np.ndarray, bias: float, feature_mean: np.ndarray,
        feature_scale: np.ndarray, threshold: float = 0.5,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.weights = np.asarray(weights, dtype=np.float64)
        self.bias = float(bias)
        self.feature_mean = np.asarray(feature_mean, dtype=np.float64)
        self.feature_scale = np.asarray(feature_scale, dtype=np.float64)
        self.threshold = float(threshold)
        self.metadata = copy.deepcopy(metadata) if metadata is not None else {}
        self.artifact_sha256: str | None = None
        if (self.weights.ndim != 1 or self.weights.size == 0
                or self.feature_mean.shape != self.weights.shape):
            raise ModelError("model feature arrays have incompatible shapes")
        if self.feature_scale.shape != self.weights.shape or np.any(self.feature_scale <= 0):
            raise ModelError("model feature scales must be positive")
        if not np.isfinite(self.weights).all() or not np.isfinite(
            np.r_[self.bias, self.feature_mean, self.feature_scale, self.threshold]
        ).all():
            raise ModelError("model contains non-finite values")
        if not 0.0 < self.threshold < 1.0:
            raise ModelError("threshold must be between 0 and 1")

    @classmethod
    def fit(
        cls, features: np.ndarray, labels: np.ndarray, *, epochs: int = 1000,
        learning_rate: float = 0.1, l2: float = 1e-3,
    ) -> "LogisticRegressionModel":
        x = np.asarray(features, dtype=np.float64)
        raw_labels = np.asarray(labels)
        if raw_labels.dtype.kind not in "biuf":
            raise ModelError("binary training labels must be numeric")
        y = raw_labels.astype(np.float64)
        if (x.ndim != 2 or x.shape[1] == 0 or y.ndim != 1
                or x.shape[0] != y.size or x.shape[0] < 2):
            raise ModelError("features and labels must contain at least two aligned rows")
        if not np.isfinite(x).all() or not np.isfinite(y).all():
            raise ModelError("training data contains non-finite values")
        if set(np.unique(y).tolist()) != {0.0, 1.0}:
            raise ModelError("binary training requires both labels 0 and 1")
        if (type(epochs) is not int or epochs < 1 or not _number(learning_rate)
                or not _number(l2) or learning_rate <= 0 or l2 < 0):
            raise ModelError("invalid optimization parameters")
        # Fit normalization on training rows only; held-out rows are never passed here.
        mean = x.mean(axis=0)
        scale = x.std(axis=0)
        scale = np.where(scale < 1e-12, 1.0, scale)
        standardized = (x - mean) / scale
        weights = np.zeros(x.shape[1], dtype=np.float64)
        bias = 0.0
        for _ in range(epochs):
            error = _sigmoid(standardized @ weights + bias) - y
            weights -= learning_rate * ((standardized.T @ error) / x.shape[0] + l2 * weights)
            bias -= learning_rate * float(error.mean())
        return cls(weights, bias, mean, scale)

    def predict_proba(self, features: np.ndarray) -> np.ndarray:
        x = np.asarray(features, dtype=np.float64)
        if x.ndim == 1:
            x = x.reshape(1, -1)
        if x.ndim != 2 or x.shape[1] != self.weights.size:
            raise ModelError("features do not match the model feature count")
        if not np.isfinite(x).all():
            raise ModelError("features contain non-finite values")
        return _sigmoid(((x - self.feature_mean) / self.feature_scale) @ self.weights + self.bias)

    def predict(self, features: np.ndarray) -> np.ndarray:
        return (self.predict_proba(features) >= self.threshold).astype(np.int64)

    def to_artifact(self) -> dict[str, Any]:
        contract = feature_contract()
        if self.weights.size != contract["feature_count"]:
            raise ModelError("model does not match the current 36-feature contract")
        if self.threshold != 0.5:
            raise ModelError("research artifacts require the fixed threshold 0.5")
        validate_training_metadata(self.metadata)
        artifact = copy.deepcopy(self.metadata)
        artifact.update({
            "artifact_type": ARTIFACT_TYPE, "artifact_version": ARTIFACT_VERSION,
            "threshold": self.threshold, "feature_contract": contract,
            "preprocessing": contract["preprocessing"], "weights": self.weights.tolist(),
            "bias": self.bias, "feature_mean": self.feature_mean.tolist(),
            "feature_scale": self.feature_scale.tolist(),
        })
        # Also revalidate numeric parameters if a caller has mutated the arrays.
        self.__class__(self.weights, self.bias, self.feature_mean, self.feature_scale, self.threshold)
        return artifact

    @classmethod
    def from_artifact(cls, artifact: dict[str, Any]) -> "LogisticRegressionModel":
        if (not isinstance(artifact, dict) or artifact.get("artifact_type") != ARTIFACT_TYPE
                or artifact.get("artifact_version") != ARTIFACT_VERSION):
            raise ModelError("unsupported model artifact schema/version; retrain with this package")
        contract = feature_contract()
        try:
            if (json.dumps(artifact["feature_contract"], sort_keys=True, allow_nan=False)
                    != json.dumps(contract, sort_keys=True, allow_nan=False)
                    or artifact["preprocessing"] != contract["preprocessing"]):
                raise ModelError("artifact feature contract does not exactly match this package")
            for key in ("weights", "feature_mean", "feature_scale"):
                values = artifact[key]
                if (not isinstance(values, list) or len(values) != contract["feature_count"]
                        or not all(_number(value) for value in values)):
                    raise ModelError("artifact feature parameters are malformed or non-finite")
            if not _number(artifact["bias"]) or not _number(artifact["threshold"]) or artifact["threshold"] != 0.5:
                raise ModelError("artifact bias or fixed threshold is invalid")
            metadata = {key: copy.deepcopy(value) for key, value in artifact.items()
                        if key not in _PARAMETER_KEYS}
            validate_training_metadata(metadata)
            return cls(artifact["weights"], artifact["bias"], artifact["feature_mean"],
                       artifact["feature_scale"], artifact["threshold"], metadata)
        except ModelError:
            raise
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ModelError("malformed model artifact") from exc


def serialized_model(model: LogisticRegressionModel) -> str:
    return json.dumps(model.to_artifact(), indent=2, sort_keys=True, allow_nan=False) + "\n"


def save_model(model: LogisticRegressionModel, path: str | Path, *, overwrite: bool = False) -> None:
    """Write finite JSON without executable serialization or implicit replacement."""

    serialized = serialized_model(model)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("w" if overwrite else "x", encoding="utf-8", newline="\n") as stream:
            stream.write(serialized)
    except FileExistsError as exc:
        raise ModelError("model output already exists; use --overwrite explicitly") from exc
    model.artifact_sha256 = hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def load_model(path: str | Path) -> LogisticRegressionModel:
    try:
        raw = Path(path).read_bytes()
        artifact = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelError("could not read JSON model artifact") from exc
    model = LogisticRegressionModel.from_artifact(artifact)
    model.artifact_sha256 = hashlib.sha256(raw).hexdigest()
    return model
