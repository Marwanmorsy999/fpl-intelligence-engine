"""Phase 9.8 — Deployment Runner.

Orchestrates a deployment pass:

    load production config -> validate dockerfile -> wire monitoring/logging
        -> readiness checks -> (optional) build the Docker image

:class:`DeploymentRunner` is dependency-injected (docker builder, monitoring
service, environment mapping), so ``pytest`` can run a full deployment — including
the Docker build — with everything mocked and zero network/daemon calls. The
default ``run()`` (no ``--build``) is a completely offline readiness check.

The runner consumes the Phase 9.5/9.6 (ingestion/scheduling), Phase 9.7
(verification) and the new Phase 9.8 deployment stack read-only. It does **not**
modify the quantitative Phases 1–8 stack and hardcodes no credentials.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fpl_intelligence.deployment.config import (
    DEFAULT_CONFIG_PATH,
    ProductionConfig,
    ProductionConfigError,
    load_production_config,
    validate_production_config,
)
from fpl_intelligence.deployment.docker import (
    DockerBuildConfig,
    DockerBuilder,
    DockerBuildResult,
    DockerfileValidationReport,
    build_docker_image,
    validate_dockerfile,
)
from fpl_intelligence.deployment.monitoring import (
    MonitoringService,
    build_monitoring_service,
    setup_production_logging,
)


@dataclass
class ReadinessCheck:
    """One deployment-readiness probe (config valid? Dockerfile production-ready?)."""

    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class ReadinessReport:
    """Every readiness probe plus the aggregate result."""

    checks: list[ReadinessCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass
class DeploymentReport:
    """Everything a deployment pass produced, for the CLI and callers."""

    config: dict[str, Any]
    dockerfile_ok: bool
    readiness: ReadinessReport
    build: DockerBuildResult | None = None
    dockerfile_validation: DockerfileValidationReport | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config,
            "dockerfile_ok": self.dockerfile_ok,
            "readiness": self.readiness.to_dict(),
            "build": self.build.to_dict() if self.build is not None else None,
            "dockerfile_validation": (
                self.dockerfile_validation.to_dict()
                if self.dockerfile_validation is not None
                else None
            ),
        }
class DeploymentRunner:
    """Run one deployment pass with injectable infrastructure.

    Args:
        config_path: Path to the production YAML config (mocked in tests).
        environ: Environment mapping; ``None`` means the real ``os.environ``.
        docker_builder: Build seam; tests inject a fake so ``docker`` never runs.
        monitoring: Pre-built monitoring service; ``None`` builds from config.
        logger: Logger for the runner's own diagnostics.
    """

    def __init__(
        self,
        *,
        config_path: Path | str = DEFAULT_CONFIG_PATH,
        environ: Mapping[str, str] | None = None,
        docker_builder: DockerBuilder | None = None,
        monitoring: MonitoringService | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config_path = Path(config_path)
        self._environ = dict(environ) if environ is not None else None
        self._docker_builder = docker_builder
        self._monitoring = monitoring
        self._logger = logger or logging.getLogger("fpl_intelligence.deployment")
        self._config: ProductionConfig | None = None

    @property
    def config(self) -> ProductionConfig:
        """The loaded production config (cached across the pass)."""
        if self._config is None:
            self._config = load_production_config(self._config_path, environ=self._environ)
        return self._config

    def dockerfile_validation(self) -> DockerfileValidationReport:
        return validate_dockerfile(self.config.dockerfile_path)

    def monitoring_service(self) -> MonitoringService:
        """The monitoring bundle; built from config unless injected."""
        if self._monitoring is None:
            self._monitoring = build_monitoring_service(self.config)
        return self._monitoring

    def readiness_checks(self) -> ReadinessReport:
        """Run every offline probe and aggregate the results."""
        checks: list[ReadinessCheck] = []
        try:
            config = self.config
        except ProductionConfigError as exc:
            checks.append(
                ReadinessCheck("config_load", False, f"{type(exc).__name__}: {exc}")
            )
            return ReadinessReport(checks)
        checks.append(ReadinessCheck("config_load", True, f"loaded {self._config_path}"))

        issues = validate_production_config(config)
        checks.append(
            ReadinessCheck(
                "config_valid",
                not issues,
                "; ".join(issues) or "production constraints satisfied",
            )
        )

        docker_report = self.dockerfile_validation()
        detail = (
            "production-ready Dockerfile"
            if docker_report.ok
            else "; ".join(f"{issue.code}: {issue.detail}" for issue in docker_report.issues)
        )
        checks.append(ReadinessCheck("dockerfile", docker_report.ok, detail))

        monitoring = self.monitoring_service()
        rule_count = len(monitoring.alerts.rules) if monitoring.alerts is not None else 0
        checks.append(
            ReadinessCheck(
                "monitoring",
                True,
                f"metric + health registries and {rule_count} alert rule(s) ready",
            )
        )
        return ReadinessReport(checks)

    def run(self, *, build: bool = False) -> DeploymentReport:
        """Execute the deployment pass.

        ``build=False`` (default) is a fully offline readiness check. ``build=True``
        additionally builds the Docker image through the (possibly injected) builder.
        """
        readiness = self.readiness_checks()
        docker_report = self.dockerfile_validation()
        build_result: DockerBuildResult | None = None
        if build:
            docker_config = DockerBuildConfig(
                image_name=self.config.docker_image_name,
                tag=self.config.docker_tag,
                dockerfile=self.config.dockerfile_path,
                context=self.config.docker_context_path,
            )
            build_result = build_docker_image(
                docker_config,
                builder=self._docker_builder,
            )
        return DeploymentReport(
            config=self.config.to_dict(redact_secrets=True),
            dockerfile_ok=docker_report.ok,
            readiness=readiness,
            build=build_result,
            dockerfile_validation=docker_report,
        )


def deploy(
    *,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    build: bool = False,
    environ: Mapping[str, str] | None = None,
    docker_builder: DockerBuilder | None = None,
    setup_logging: bool = True,
) -> DeploymentReport:
    """Convenience wrapper: configure production logging and run a deployment pass."""
    if setup_logging:
        setup_production_logging()
    return DeploymentRunner(
        config_path=config_path,
        environ=environ,
        docker_builder=docker_builder,
    ).run(build=build)