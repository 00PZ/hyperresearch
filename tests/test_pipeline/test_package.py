"""Spec 2.2 engine verified package."""

from __future__ import annotations

import json
from pathlib import Path

from hyperresearch.core.runs import load_manifest, set_status
from hyperresearch.pipeline.orchestrator import execute_run, report_path, resume_run
from hyperresearch.pipeline.package import (
    FORBIDDEN_ENGINE_KEYS,
    PackageError,
    package_path,
    validate_package,
    verification_of,
    write_package,
)
from hyperresearch.runtime import FakeRuntime
from tests.test_pipeline.test_full import _rt, plant_src, run


class BoomRuntime(FakeRuntime):
    async def run(self, task, context):  # type: ignore[no-untyped-def]
        raise AssertionError("ModelRuntime must not run")


def _pkg(tmp_vault, tag: str = "pkg-1") -> Path:
    run_dir = tmp_vault.run_dir(tag)
    run_dir.mkdir(parents=True, exist_ok=True)
    dest = package_path(run_dir)
    write_package(
        run_dir,
        dest,
        company="shoshin",
        run_id=tag,
        report_bytes=b"# report\n",
        snapshots=[
            {
                "ref": "d1",
                "document_id": "d1",
                "namespace": "shoshin",
                "type": "wiki",
                "content_hash": "h",
                "body": "snap",
                "provenance": ["https://a.example"],
            }
        ],
        verification=verification_of(
            b"# report\n",
            [
                {
                    "ref": "d1",
                    "document_id": "d1",
                    "body": "snap",
                }
            ],
            verified_at="t",
        ),
    )
    return dest


def test_validator_only_package_dir(tmp_vault, tmp_path):
    dest = _pkg(tmp_vault)
    # Checkpoints next to the package must not be required.
    (tmp_vault.run_dir("pkg-1") / "run.json").write_text("{}", encoding="utf-8")
    manifest = validate_package(dest)
    assert manifest["schema"] == "hyperresearch.package.v1"
    mutated = dest / "report.md"
    mutated.write_bytes(b"# tampered\n")
    try:
        validate_package(dest)
        raise AssertionError("mutated report must fail")
    except PackageError as exc:
        assert exc.reason == "stale_bindings"


def test_no_gbrain_slug_index_published_at_on_engine_package(tmp_vault):
    dest = _pkg(tmp_vault, "pkg-2")
    manifest = validate_package(dest)
    for key in FORBIDDEN_ENGINE_KEYS:
        assert key not in manifest
    dumped = json.dumps(manifest)
    assert "published_at" not in dumped
    assert "companies/shoshin/research/index" not in dumped


def test_package_atomic_replace_rebuild_without_modelruntime(tmp_vault):
    tag = "pkg-rebuild"
    plant_src(tmp_vault, tag)
    result = run(
        execute_run(
            tmp_vault,
            "q",
            _rt(),
            profile="full",
            tag=tag,
            company="shoshin",
        )
    )
    assert result["manifest"]["status"] == "verified"
    dest = package_path(tmp_vault.run_dir(tag))
    assert dest.is_dir()
    import shutil

    shutil.rmtree(dest)
    set_status(tmp_vault, tag, "verified")
    second = run(resume_run(tmp_vault, tag, BoomRuntime()))
    assert second["verify"]["passed"] is True
    validate_package(package_path(tmp_vault.run_dir(tag)))


def test_artifact_error(tmp_vault):
    tag = "pkg-bad"
    plant_src(tmp_vault, tag)
    result = run(
        execute_run(
            tmp_vault,
            "q",
            _rt(),
            profile="full",
            tag=tag,
            company="shoshin",
        )
    )
    assert result["manifest"]["status"] == "verified"
    report_path(tmp_vault, tag).write_text("mutated-report-bytes-no-longer-match\n", encoding="utf-8")
    import shutil

    shutil.rmtree(package_path(tmp_vault.run_dir(tag)))
    set_status(tmp_vault, tag, "verified")
    second = run(resume_run(tmp_vault, tag, BoomRuntime()))
    pkg = load_manifest(tmp_vault, tag).get("package") or {}
    assert pkg.get("status") == "invalid"
    assert pkg.get("reason") == "artifact_error"
    assert second["manifest"]["vault_tag"] == tag


def test_validator_rejects_empty_verification(tmp_vault):
    run_dir = tmp_vault.run_dir("pkg-empty")
    run_dir.mkdir(parents=True, exist_ok=True)
    dest = package_path(run_dir)
    write_package(
        run_dir,
        dest,
        company="shoshin",
        run_id="pkg-empty",
        report_bytes=b"UNVERIFIED",
        snapshots=[],
        verification={},
    )
    try:
        validate_package(dest)
        raise AssertionError("empty verification must fail")
    except PackageError as exc:
        assert exc.reason == "stale_bindings"


def test_validator_rejects_mismatched_bindings(tmp_vault):
    run_dir = tmp_vault.run_dir("pkg-mis")
    run_dir.mkdir(parents=True, exist_ok=True)
    dest = package_path(run_dir)
    write_package(
        run_dir,
        dest,
        company="shoshin",
        run_id="pkg-mis",
        report_bytes=b"# report\n",
        snapshots=[{"document_id": "d1", "body": "snap"}],
        verification={
            "verified_hash": "nope",
            "cite_check_bind": "nope",
            "independence_bind": "nope",
            "verified_at": "t",
        },
    )
    try:
        validate_package(dest)
        raise AssertionError("mismatched binds must fail")
    except PackageError as exc:
        assert exc.reason == "stale_bindings"


def test_rebuild_reuses_original_bind_not_new_over_changed_evidence(tmp_vault):
    from hyperresearch.pipeline.package import rebuild_package

    dest = _pkg(tmp_vault, "pkg-rebind")
    original = validate_package(dest)
    cite = original["verification"]["cite_check_bind"]
    run_dir = tmp_vault.run_dir("pkg-rebind")
    report = run_dir / "report-artifact.md"
    report.write_bytes(b"# changed evidence\n")
    try:
        rebuild_package(
            run_dir,
            company="shoshin",
            run_id="pkg-rebind",
            report_path=report,
            snapshots=[{"document_id": "d1", "body": "snap"}],
            verification=dict(original["verification"]),
        )
        raise AssertionError("rebuild must not mint a new bind")
    except PackageError as exc:
        assert exc.reason == "artifact_error"
    assert original["verification"]["cite_check_bind"] == cite
