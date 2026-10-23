"""Regression tests for the build/publish pipeline and locked dependencies (M1.1).

These read the workflow and Docker files as data, so a future edit that lets
an untested image reach GHCR, or bypasses uv.lock, fails `pytest` locally and
in CI before it can ever run.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"


def _wf(name: str) -> dict:
    return yaml.safe_load((WF / name).read_text())


def _steps(name: str, job: str) -> list[dict]:
    return _wf(name)["jobs"][job]["steps"]


def _index(steps: list[dict], needle: str) -> int:
    for i, step in enumerate(steps):
        if needle in (step.get("name") or "") or needle in (step.get("uses") or ""):
            return i
    raise AssertionError(f"step {needle!r} not found")


# ---------------------------------------------------------------- publication gate


def test_image_is_built_without_pushing() -> None:
    build = _steps("image.yml", "image")[_index(_steps("image.yml", "image"), "build-push-action")]
    w = build["with"]
    assert "push" not in w or w["push"] is False
    assert w["outputs"].startswith("type=oci,dest=")
    assert set(w["platforms"].split(",")) == {"linux/amd64", "linux/arm64"}


def test_publish_happens_only_after_every_smoke_test() -> None:
    steps = _steps("image.yml", "image")
    build = _index(steps, "build-push-action")
    amd = _index(steps, "Smoke test amd64")
    arm = _index(steps, "Smoke test arm64")
    pub = _index(steps, "Publish")
    assert build < amd < pub and build < arm < pub
    assert steps[pub]["if"] == "inputs.publish"
    # No step may opt out of failing the job (which would let publish run anyway).
    for step in steps:
        assert step.get("continue-on-error") in (None, False), step.get("name")
        assert "always()" not in str(step.get("if", "")), step.get("name")


def test_published_bytes_are_the_tested_bytes() -> None:
    steps = _steps("image.yml", "image")
    for name in ("Smoke test amd64", "Smoke test arm64"):
        assert "$ARCHIVE" in steps[_index(steps, name)]["run"]
    run = steps[_index(steps, "Publish")]["run"]
    assert "oci-archive:$ARCHIVE" in run
    assert "--preserve-digests" in run
    assert '"$pushed" = "$BUILT"' in run  # every pushed tag is verified against the built digest


def test_only_image_workflow_pushes() -> None:
    for path in WF.glob("*.yml"):
        if path.name == "image.yml":
            continue
        text = path.read_text()
        assert "docker://" not in text, path.name
        assert not re.search(r"^\s*push:\s*true", text, re.M), path.name
        assert "docker/login-action" not in text, path.name


def test_ci_and_release_gate_image_on_checks() -> None:
    for name in ("ci.yml", "release.yml"):
        jobs = _wf(name)["jobs"]
        assert jobs["checks"]["uses"] == "./.github/workflows/checks.yml"
        assert jobs["image"]["uses"] == "./.github/workflows/image.yml"
        assert jobs["image"]["needs"] == "checks"
    ci = _wf("ci.yml")["jobs"]["image"]["with"]
    assert "refs/heads/main" in ci["publish"] and "push" in ci["publish"]  # PRs never publish
    release = _wf("release.yml")["jobs"]
    assert release["image"]["with"]["publish"] is True
    assert release["github-release"]["needs"] == "image"


def test_smoke_script_checks_what_matters() -> None:
    script = ROOT / "docker" / "smoke-test.sh"
    assert os.access(script, os.X_OK)
    text = script.read_text()
    for must in (
        "/health",
        "must not include external services",
        "/proc/1/status",
        "setup-code.txt",
        "wrong code accepted",
        "docker restart",
    ):
        assert must in text, must


# ---------------------------------------------------------------- locked dependencies


def test_lockfile_is_committed_and_used_everywhere() -> None:
    assert (ROOT / "uv.lock").exists()
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text()
    assert "COPY pyproject.toml uv.lock" in dockerfile
    assert dockerfile.count("uv sync --locked") == 2
    assert "pip install ." not in dockerfile  # the unlocked path must not come back

    checks = (WF / "checks.yml").read_text()
    assert "uv lock --check" in checks
    syncs = re.findall(r"uv sync[^\n]*", checks)
    assert syncs and all("--locked" in s for s in syncs)
    assert "pip install" not in checks


def test_uv_version_is_pinned_identically() -> None:
    docker_ver = re.search(
        r"ARG UV_VERSION=([\d.]+)", (ROOT / "docker" / "Dockerfile").read_text()
    ).group(1)
    ci_ver = _wf("checks.yml")["env"]["UV_VERSION"]
    assert docker_ver == ci_ver
