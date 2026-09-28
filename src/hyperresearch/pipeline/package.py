"""Atomic verified research package (hyperresearch.package.v1)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "hyperresearch.package.v1"
PACKAGE_DIR = "verified-package"
FORBIDDEN_ENGINE_KEYS = frozenset({"slug", "index", "published_at", "gbrain_slug"})
REQUIRED_VERIFICATION = ("verified_hash", "cite_check_bind", "independence_bind", "verified_at")
FROZEN_VERIFICATION = "package-verification.json"


class PackageError(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def canonical_payload(manifest: dict[str, Any]) -> bytes:
    body = {k: v for k, v in manifest.items() if k != "package_digest"}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def package_digest_of(manifest: dict[str, Any]) -> str:
    return _sha256_bytes(canonical_payload(manifest))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def report_bind(report_bytes: bytes) -> str:
    from hyperresearch.pipeline.patch import content_hash

    try:
        return content_hash(report_bytes.decode("utf-8-sig"))
    except UnicodeDecodeError:
        return _sha256_bytes(report_bytes)


def snapshots_bind(snapshots: list[dict[str, Any]]) -> str:
    from hyperresearch.pipeline.patch import content_hash

    lines = [
        f"{snap.get('document_id') or snap.get('ref') or ''}:{content_hash(str(snap.get('body') or ''))}"
        for snap in snapshots
    ]
    return content_hash("\n".join(sorted(lines)))


def verification_of(
    report_bytes: bytes,
    snapshots: list[dict[str, Any]],
    *,
    verified_at: str | None = None,
) -> dict[str, Any]:
    cite = report_bind(report_bytes)
    return {
        "verified_hash": cite,
        "cite_check_bind": cite,
        "independence_bind": snapshots_bind(snapshots),
        "verified_at": verified_at or _now(),
    }


def package_path(run_dir: Path) -> Path:
    return run_dir / PACKAGE_DIR


def load_manifest(package_dir: Path) -> dict[str, Any]:
    raw = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PackageError("artifact_error", "manifest is not an object")
    return raw


def validate_package(package_dir: Path) -> dict[str, Any]:
    """Read only this directory. Do not open checkpoints."""
    if not package_dir.is_dir():
        raise PackageError("artifact_error", "package directory missing")
    manifest_path = package_dir / "manifest.json"
    if not manifest_path.is_file():
        raise PackageError("artifact_error", "manifest.json missing")
    manifest = load_manifest(package_dir)
    if manifest.get("schema") != SCHEMA:
        raise PackageError("stale_bindings", "schema mismatch")
    for key in FORBIDDEN_ENGINE_KEYS:
        if key in manifest:
            raise PackageError("artifact_error", f"engine package must not contain {key}")
    report = manifest.get("report") or {}
    report_path = package_dir / str(report.get("path") or "report.md")
    if not report_path.is_file():
        raise PackageError("artifact_error", "report missing")
    if _sha256_file(report_path) != report.get("sha256"):
        raise PackageError("stale_bindings", "report hash mismatch")
    for snap in manifest.get("snapshots") or []:
        spath = package_dir / str(snap.get("path") or "")
        if not spath.is_file():
            raise PackageError("artifact_error", f"snapshot missing: {snap.get('document_id')}")
        if _sha256_file(spath) != snap.get("sha256"):
            raise PackageError("stale_bindings", f"snapshot hash mismatch: {snap.get('document_id')}")
    expected = package_digest_of(manifest)
    if manifest.get("package_digest") != expected:
        raise PackageError("stale_bindings", "package_digest mismatch")
    ver = manifest.get("verification")
    if not isinstance(ver, dict):
        raise PackageError("stale_bindings", "verification missing")
    for key in REQUIRED_VERIFICATION:
        if not ver.get(key):
            raise PackageError("stale_bindings", f"verification.{key} missing")
    report_bytes = report_path.read_bytes()
    cite = str(ver["cite_check_bind"])
    if cite != _sha256_bytes(report_bytes) and cite != report_bind(report_bytes):
        raise PackageError("stale_bindings", "cite_check_bind mismatch")
    loaded: list[dict[str, Any]] = []
    for snap in manifest.get("snapshots") or []:
        spath = package_dir / str(snap.get("path") or "")
        try:
            data = json.loads(spath.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PackageError("artifact_error", "snapshot is not JSON") from exc
        loaded.append(data if isinstance(data, dict) else {})
    if str(ver["independence_bind"]) != snapshots_bind(loaded):
        raise PackageError("stale_bindings", "independence_bind mismatch")
    return manifest


def write_package(
    staging_parent: Path,
    dest: Path,
    *,
    company: str,
    run_id: str,
    report_bytes: bytes,
    snapshots: list[dict[str, Any]],
    verification: dict[str, Any],
) -> dict[str, Any]:
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="pkg-", dir=str(staging_parent)))
    try:
        (staging / "snapshots").mkdir()
        report_rel = "report.md"
        (staging / report_rel).write_bytes(report_bytes)
        snap_rows: list[dict[str, Any]] = []
        for i, snap in enumerate(snapshots):
            rel = f"snapshots/{i:04d}.json"
            payload = {
                "ref": snap.get("ref"),
                "namespace": snap.get("namespace"),
                "document_id": snap.get("document_id"),
                "type": snap.get("type"),
                "content_hash": snap.get("content_hash"),
                "retrieved_at": snap.get("retrieved_at"),
                "body": snap.get("body"),
                "provenance": list(snap.get("provenance") or []),
            }
            (staging / rel).write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            snap_rows.append(
                {
                    "document_id": snap.get("document_id"),
                    "namespace": snap.get("namespace"),
                    "type": snap.get("type"),
                    "provenance": list(snap.get("provenance") or []),
                    "path": rel,
                    "sha256": _sha256_file(staging / rel),
                }
            )
        manifest: dict[str, Any] = {
            "schema": SCHEMA,
            "company": company,
            "run_id": run_id,
            "report": {"path": report_rel, "sha256": _sha256_bytes(report_bytes)},
            "snapshots": snap_rows,
            "verification": dict(verification),
        }
        manifest["package_digest"] = package_digest_of(manifest)
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_dest = dest.with_name(dest.name + ".next")
        if tmp_dest.exists():
            shutil.rmtree(tmp_dest)
        os.replace(staging, tmp_dest)
        if dest.exists():
            shutil.rmtree(dest)
        os.replace(tmp_dest, dest)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def rebuild_package(
    run_dir: Path,
    *,
    company: str,
    run_id: str,
    report_path: Path,
    snapshots: list[dict[str, Any]],
    verification: dict[str, Any],
) -> dict[str, Any]:
    if not report_path.is_file():
        raise PackageError("artifact_error", "report artifact missing")
    report_bytes = report_path.read_bytes()
    cite = str(verification.get("cite_check_bind") or "")
    if not cite or (cite != _sha256_bytes(report_bytes) and cite != report_bind(report_bytes)):
        raise PackageError("artifact_error", "report bytes no longer match cite_check_bind")
    indep = str(verification.get("independence_bind") or "")
    if not indep or indep != snapshots_bind(snapshots):
        raise PackageError("artifact_error", "evidence no longer matches independence_bind")
    return write_package(
        run_dir,
        package_path(run_dir),
        company=company,
        run_id=run_id,
        report_bytes=report_bytes,
        snapshots=snapshots,
        verification=verification,
    )
