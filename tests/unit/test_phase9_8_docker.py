"""Phase 9.8 unit tests — Docker Containerization.

The Docker build is fully mocked: tests inject a fake :class:`DockerBuilder`
(or a fake subprocess runner) so ``pytest`` never starts a Docker daemon. The
Dockerfile parser is exercised against fixture files written to ``tmp_path`` and
against the repository's own production Dockerfile.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fpl_intelligence.deployment.docker import (
    DockerBuildConfig,
    DockerBuildResult,
    DockerError,
    SubprocessDockerBuilder,
    build_docker_image,
    validate_dockerfile,
)

VALID_DOCKERFILE = """\
FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml ./
RUN pip install .
FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
RUN groupadd --system fpl && useradd --system fpl
USER fpl
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
"""


class _FakeBuilder:
    """Records each config it is asked to build."""

    def __init__(self, result: DockerBuildResult) -> None:
        self.result = result
        self.built: list[DockerBuildConfig] = []

    def build(self, config: DockerBuildConfig) -> DockerBuildResult:
        self.built.append(config)
        return self.result


def _ok_result(image: str = "fpl-intelligence-engine:production") -> DockerBuildResult:
    return DockerBuildResult(image_ref=image, command=("docker", "build", image), success=True)


def _write(tmp_path: Path, text: str, name: str = "Dockerfile") -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _fake_runner_ok(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, stdout="Successfully built abc123")


def _fake_runner_fail(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 1, stdout="", stderr="error: cannot find author")


def test_docker_build_config_image_ref() -> None:
    config = DockerBuildConfig(image_name="fpl", tag="v0.9.8")
    assert config.image_ref == "fpl:v0.9.8"


def test_build_docker_image_success_with_fake_builder(tmp_path: Path) -> None:
    dockerfile = _write(tmp_path, VALID_DOCKERFILE)
    config = DockerBuildConfig(image_name="fpl", tag="v0.9.8", dockerfile=dockerfile, context=".")
    builder = _FakeBuilder(_ok_result("fpl:v0.9.8"))
    result = build_docker_image(config, builder=builder)
    assert result.success
    assert result.image_ref == "fpl:v0.9.8"
    assert builder.built == [config]


def test_build_raises_when_builder_fails(tmp_path: Path) -> None:
    dockerfile = _write(tmp_path, VALID_DOCKERFILE)
    config = DockerBuildConfig(image_name="fpl", dockerfile=dockerfile)
    builder = _FakeBuilder(
        DockerBuildResult(image_ref=config.image_ref, command=(), success=False, error="boom")
    )
    with pytest.raises(DockerError, match="docker build failed"):
        build_docker_image(config, builder=builder)


def test_build_raises_on_invalid_dockerfile(tmp_path: Path) -> None:
    config = DockerBuildConfig(image_name="fpl", dockerfile=str(tmp_path / "NopeDockerfile"))
    with pytest.raises(DockerError, match="not production-ready"):
        build_docker_image(config, builder=_FakeBuilder(_ok_result()))


def test_build_skips_validation_when_disabled(tmp_path: Path) -> None:
    config = DockerBuildConfig(image_name="fpl", dockerfile=str(tmp_path / "nope"))
    result = build_docker_image(
        config, builder=_FakeBuilder(_ok_result()), validate_first=False
    )
    assert result.success


def test_validate_dockerfile_missing_file(tmp_path: Path) -> None:
    report = validate_dockerfile(tmp_path / "missing")
    assert not report.ok
    assert report.checks_run == 0
    assert report.issues[0].code == "UNREADABLE"

def test_validate_dockerfile_accepts_valid(tmp_path: Path) -> None:
    report = validate_dockerfile(_write(tmp_path, VALID_DOCKERFILE))
    assert report.ok
    assert report.checks_run >= 5


@pytest.mark.parametrize(
    "snippet,missing_code",
    [
        ("FROM python:3.12-slim\nEXPOSE 8000\nUSER fpl\nCMD [\"x\"]\n", "MISSING_WORKDIR"),
        ("FROM python:3.12-slim\nWORKDIR /app\nEXPOSE 8000\nUSER fpl\n", "MISSING_CMD"),
        ("WORKDIR /app\nEXPOSE 8000\nUSER fpl\nCMD [\"x\"]\n", "MISSING_FROM"),
        ("FROM python:3.12-slim\nWORKDIR /app\nUSER fpl\nCMD [\"x\"]\n", "MISSING_EXPOSE"),
        ("FROM python:3.12-slim\nWORKDIR /app\nEXPOSE 8000\nCMD [\"x\"]\n", "MISSING_USER"),
    ],
)
def test_validate_dockerfile_rejects_missing_directives(
    tmp_path: Path, snippet: str, missing_code: str
) -> None:
    dockerfile = _write(tmp_path, snippet)
    report = validate_dockerfile(dockerfile)
    assert not report.ok
    assert any(issue.code == missing_code for issue in report.issues)


def test_validate_dockerfile_rejects_unpinned_latest(tmp_path: Path) -> None:
    dockerfile = _write(
        tmp_path,
        "FROM python:latest\nWORKDIR /app\nEXPOSE 8000\nUSER fpl\nCMD [\"x\"]\n",
    )
    report = validate_dockerfile(dockerfile)
    assert any(issue.code == "UNPINNED_BASE" for issue in report.issues)


def test_validate_dockerfile_rejects_no_tag_base(tmp_path: Path) -> None:
    dockerfile = _write(
        tmp_path,
        "FROM python\nWORKDIR /app\nEXPOSE 8000\nUSER fpl\nCMD [\"x\"]\n",
    )
    report = validate_dockerfile(dockerfile)
    assert any(issue.code == "UNPINNED_BASE" for issue in report.issues)


def test_validate_dockerfile_allows_digest_pin(tmp_path: Path) -> None:
    dockerfile = _write(
        tmp_path,
        "FROM python@sha256:abcdef\nWORKDIR /app\nEXPOSE 8000\nUSER fpl\nCMD [\"x\"]\n",
    )
    assert validate_dockerfile(dockerfile).ok


def test_validate_dockerfile_rejects_running_as_root(tmp_path: Path) -> None:
    dockerfile = _write(
        tmp_path,
        "FROM python:3.12-slim\nWORKDIR /app\nEXPOSE 8000\nUSER root\nCMD [\"x\"]\n",
    )
    report = validate_dockerfile(dockerfile)
    assert any(issue.code == "RUNNING_AS_ROOT" for issue in report.issues)


def test_validate_repo_dockerfile_passes() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    report = validate_dockerfile(repo_root / "Dockerfile")
    assert report.ok, [issue.to_dict() for issue in report.issues]


def test_build_applies_build_args_flags(tmp_path: Path) -> None:
    dockerfile = _write(tmp_path, VALID_DOCKERFILE)
    config = DockerBuildConfig(
        image_name="fpl",
        tag="v1",
        dockerfile=dockerfile,
        context=".",
        build_args={"FOO": "bar"},
        no_cache=True,
        platform="linux/amd64",
    )
    builder = SubprocessDockerBuilder(_fake_runner_ok)
    result = builder.build(config)
    assert result.success
    command = list(result.command)
    assert "--no-cache" in command
    assert "--platform" in command and command[command.index("--platform") + 1] == "linux/amd64"
    assert "--build-arg" in command and "FOO=bar" in command
    assert command[-1] == "."


def test_subprocess_builder_success() -> None:
    builder = SubprocessDockerBuilder(_fake_runner_ok)
    result = builder.build(DockerBuildConfig(image_name="fpl", tag="v1"))
    assert result.success
    assert result.image_ref == "fpl:v1"
    assert result.command[:4] == ("docker", "build", "-t", "fpl:v1")
    assert result.output == ("Successfully built abc123",)


def test_subprocess_builder_failure() -> None:
    builder = SubprocessDockerBuilder(_fake_runner_fail)
    result = builder.build(DockerBuildConfig(image_name="fpl", tag="v1"))
    assert not result.success
    assert result.error == "error: cannot find author"


def test_subprocess_builder_failure_uses_stdout_when_no_stderr() -> None:
    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="boom stdout", stderr="")

    builder = SubprocessDockerBuilder(runner)
    result = builder.build(DockerBuildConfig(image_name="fpl", tag="v1"))
    assert not result.success
    assert result.error == "boom stdout"