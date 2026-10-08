"""Shared errors and deterministic serialization."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile


SCHEMA_VERSION = 1


class CampaignError(ValueError):
    """A permanent, actionable campaign failure."""


class TransientError(CampaignError):
    def __init__(self, message: str, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


class UnknownDelivery(CampaignError):
    """A remote write may have succeeded and must be reconciled."""


class Cancelled(CampaignError):
    pass


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CampaignError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: str | Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(), object_pairs_hook=_unique_pairs,
                           parse_constant=lambda value: (_ for _ in ()).throw(CampaignError(
                               f"non-finite JSON number: {value}")))
    except OSError as exc:
        raise CampaignError(f"unable to read JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CampaignError(f"invalid JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise CampaignError("expected a JSON object")
    return value


def atomic_json(path: str | Path, value: object) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def seal(value: dict) -> dict:
    result = {key: val for key, val in value.items() if key != "digest"}
    result["digest"] = digest(result)
    return result


def verify_seal(value: dict) -> None:
    if not isinstance(value.get("digest"), str) or seal(value)["digest"] != value["digest"]:
        raise CampaignError("manifest/config digest mismatch")


def positive(value, name: str, *, integer: bool = False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise CampaignError(f"{name} must be finite and positive")
    if integer and not isinstance(value, int):
        raise CampaignError(f"{name} must be an integer")
    return value
