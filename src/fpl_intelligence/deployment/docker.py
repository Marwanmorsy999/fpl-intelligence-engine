"""Phase 9.8 — Docker Containerization.

Builds and validates the production Docker image for the system.

* :func:`validate_dockerfile` checks a ``Dockerfile`` for production
  readiness: a pinned base image, a ``WORKDIR``, an exposed port, a non-root
  ``USER`` and a ``CMD``/``ENTRYPOINT``. The checks are pure string analysis of
  the file — no Docker daemon is needed.
* :class:`DockerBuilder` is the build seam. The production implementation,
  :class:`SubprocessDockerBuilder`, shells out to the ``docker`` CLI, but the
  subprocess callable is injectable so **tests mock the build** and never
  invoke Docker.
* :func:`build_docker_image` orchestrates the two: validate first, then build,
  raising :class:`DockerError` on any production-readiness or build failure.

This module is additive: it does not modify the quantitative Phases 1–8 stack,
it makes **no** live ``docker``/network calls inside ``pytest`` (the runner is
mocked), and it hardcodes no credentials (build args are always supplied by the
caller).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

#: Directives that a production-ready Dockerfile must use.
_REQUIRED_DIRECTIVES = ("WORKDIR", "EXPOSE", "USER", "CMD", "ENTRYPOINT")


class DockerError(RuntimeError):
    """Raised when a Dockerfile is not production-ready or a build fails."""


@dataclass(frozen=True)
class DockerBuildConfig:
    """Everything needed to build one image (caller-supplied, never secrets)."""

    image_name: str
    tag: str = "latest"
    dockerfile: str = "Dockerfile"
    context: str = "."
    build_args: Mapping[str, str] = field(default_factory=dict)
    no_cache: bool = False
    platform: str | None = None

    @property
    def image_ref(self) -> str:
        """The full ``name:tag`` reference for the produced image."""
        return f"{self.image_name}:{self.tag}"


@dataclass(frozen=True)
class DockerBuildResult:
    """Outcome of one (mocked or real) Docker build."""

    image_ref: str
    command: tuple[str, ...]
    success: bool
    output: tuple[str, ...] = ()
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "image_ref": self.image_ref,
            "command": list(self.command),
            "success": self.success,
            "output": list(self.output),
            "error": self.error,
        }


@dataclass(frozen=True)
class DockerfileIssue:
    """One production-readiness violation found in a Dockerfile."""

    code: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class DockerfileValidationReport:
    """Result of validating a Dockerfile for production readiness."""

    dockerfile: str
    issues: list[DockerfileIssue]
    checks_run: int

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "dockerfile": self.dockerfile,
            "ok": self.ok,
            "checks_run": self.checks_run,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _directive(line: str) -> str | None:
    """Return the Dockerfile directive keyword of a line, if any.

    Continuation lines, comments and blank lines return ``None``.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    for keyword in ("FROM", "WORKDIR", "EXPOSE", "USER", "CMD", "ENTRYPOINT"):
        if stripped == keyword or stripped.startswith(keyword + " "):
            return keyword
    return None


def _from_image(line: str) -> str:
    """Extract the base image from a ``FROM`` line (ignoring flags/``AS``)."""
    rest = line.strip()[len("FROM") :].strip()
    for token in rest.split():
        if token.startswith("--"):
            continue
        return token.split(" AS ")[0].split(" as ")[0].strip()
    return ""


def _is_pinned(image: str) -> bool:
    """A base image is pinned when it carries a digest or a non-latest tag."""
    if "@" in image:
        return True  # digest pin (sha256:...)
    if ":" not in image:
        return False  # bare name -> implicit :latest
    tag = image.rsplit(":", 1)[1]
    return bool(tag) and tag != "latest"


def validate_dockerfile(path: Path | str) -> DockerfileValidationReport:
    """Check a Dockerfile for the production-readiness invariants.

    Returns a :class:`DockerfileValidationReport`; never raises for a missing or
    malformed file (that is reported as an ``UNREADABLE`` issue instead).
    """
    dockerfile = Path(path)
    if not dockerfile.is_file():
        return DockerfileValidationReport(
            dockerfile=str(dockerfile),
            issues=[DockerfileIssue("UNREADABLE", f"no file at {dockerfile}")],
            checks_run=0,
        )
    try:
        lines = dockerfile.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return DockerfileValidationReport(
            dockerfile=str(dockerfile),
            issues=[DockerfileIssue("UNREADABLE", f"cannot read {dockerfile}: {exc}")],
            checks_run=0,
        )

    issues: list[DockerfileIssue] = []
    checks_run = 0
    directives: dict[str, list[str]] = {keyword: [] for keyword in _REQUIRED_DIRECTIVES + ("FROM",)}

    for line in lines:
        keyword = _directive(line)
        if keyword is not None:
            directives[keyword].append(line)

    if not directives["FROM"]:
        issues.append(DockerfileIssue("MISSING_FROM", "no FROM instruction found"))
    else:
        for line in directives["FROM"]:
            image = _from_image(line)
            checks_run += 1
            if not image:
                issues.append(
                    DockerfileIssue("MISSING_FROM", f"cannot parse base image in {line!r}")
                )
            elif not _is_pinned(image):
                issues.append(
                    DockerfileIssue(
                        "UNPINNED_BASE",
                        f"base image {image!r} is not pinned to a version/digest "
                        "(pin e.g. python:3.12-slim, never :latest)",
                    )
                )

    for keyword in _REQUIRED_DIRECTIVES:
        checks_run += 1
        if not directives[keyword]:
            if keyword in ("CMD", "ENTRYPOINT"):
                # A Dockerfile needs at least one entry point directive.
                other = "ENTRYPOINT" if keyword == "CMD" else "CMD"
                if directives[other]:
                    continue
                issues.append(
                    DockerfileIssue("MISSING_CMD", "no CMD or ENTRYPOINT instruction found")
                )
            else:
                issues.append(
                    DockerfileIssue(
                        f"MISSING_{keyword}",
                        f"no {keyword} instruction found (required for production)",
                    )
                )
        elif keyword == "USER" and directives["USER"][0].strip().startswith("USER root"):
            issues.append(
                DockerfileIssue(
                    "RUNNING_AS_ROOT",
                    "USER root is forbidden in production; use a non-root user",
                )
            )

    return DockerfileValidationReport(
        dockerfile=str(dockerfile),
        issues=issues,
        checks_run=checks_run,
    )


#: The injected runner shape: given a command list, return a completed process.
SubprocessRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _default_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )


def _docker_build_command(config: DockerBuildConfig) -> list[str]:
    """Assemble the ``docker build`` command line for a config."""
    command = ["docker", "build", "-t", config.image_ref, "-f", config.dockerfile]
    if config.no_cache:
        command.append("--no-cache")
    if config.platform:
        command.extend(["--platform", config.platform])
    for key, value in config.build_args.items():
        command.extend(["--build-arg", f"{key}={value}"])
    command.append(config.context)
    return command


class DockerBuilder(Protocol):
    """Build seam. The real implementation shells out to the Docker CLI; tests
    inject a fake so ``pytest`` never invokes Docker."""

    def build(self, config: DockerBuildConfig) -> DockerBuildResult: ...


class SubprocessDockerBuilder:
    """Build via the ``docker`` CLI. The subprocess runner is injectable."""

    def __init__(self, runner: SubprocessRunner | None = None) -> None:
        self._runner = runner or _default_runner

    def build(self, config: DockerBuildConfig) -> DockerBuildResult:
        command = _docker_build_command(config)
        proc = self._runner(command)
        output = tuple(proc.stdout.splitlines()) if proc.stdout else ()
        if proc.returncode == 0:
            return DockerBuildResult(
                image_ref=config.image_ref,
                command=tuple(command),
                success=True,
                output=output,
            )
        detail = (proc.stderr or proc.stdout or "").strip()
        return DockerBuildResult(
            image_ref=config.image_ref,
            command=tuple(command),
            success=False,
            output=output,
            error=detail or f"docker build exited with code {proc.returncode}",
        )


def build_docker_image(
    config: DockerBuildConfig,
    builder: DockerBuilder | None = None,
    *,
    validate_first: bool = True,
) -> DockerBuildResult:
    """Validate the Dockerfile and build the image through ``builder``.

    ``validate_first`` gates the production-readiness checks (a failing check
    aborts the build with :class:`DockerError`). ``builder`` defaults to
    :class:`SubprocessDockerBuilder`; tests inject a fake builder so the build
    itself is fully mocked.
    """
    if validate_first:
        report = validate_dockerfile(config.dockerfile)
        if not report.ok:
            codes = ", ".join(f"{issue.code}" for issue in report.issues)
            raise DockerError(f"Dockerfile {config.dockerfile!r} is not production-ready: {codes}")
    engine = builder or SubprocessDockerBuilder()
    result = engine.build(config)
    if not result.success:
        raise DockerError(f"docker build failed for {config.image_ref}: {result.error}")
    return result
