"""Phase 9.8 — Production Deployment.

Packages the system for a production environment:

* :mod:`fpl_intelligence.deployment.config` — production configuration (file +
  environment variables; secrets are env-only) and validation.
* :mod:`fpl_intelligence.deployment.docker` — Docker containerization:
  production-Dockerfile validation and a (mockable) build pipeline.
* :mod:`fpl_intelligence.deployment.monitoring` — metric/health registries,
  threshold alerting (log + webhook sinks) and JSON logging.
* :mod:`fpl_intelligence.deployment.resilience` — retry with exponential
  backoff, a circuit breaker, and a recovery manager with dead-lettering.
* :mod:`fpl_intelligence.deployment.runner` — the deployment runner that turns
  all of the above into readiness checks and, optionally, an image build.

The CLI entry point is ``scripts/deploy.py`` (``--check-only`` is the offline
default; ``--build`` performs the Docker build). This layer is additive: it does
**not** modify the quantitative Phases 1–8 stack, makes **no** live API/``docker``
calls inside ``pytest`` (builders, webhooks and clocks are injected seams), and
hardcodes no API keys.
"""
from fpl_intelligence.deployment.config import (
    CONFIG_SCHEMA_VERSION,
    DEFAULT_CONFIG_PATH,
    ENV_FIELD_MAP,
    PRODUCTION_ENV,
    SECRET_FIELDS,
    ProductionConfig,
    ProductionConfigError,
    get_production_config,
    load_production_config,
    validate_production_config,
)
from fpl_intelligence.deployment.docker import (
    DockerBuildConfig,
    DockerBuilder,
    DockerBuildResult,
    DockerError,
    DockerfileIssue,
    DockerfileValidationReport,
    SubprocessDockerBuilder,
    build_docker_image,
    validate_dockerfile,
)
from fpl_intelligence.deployment.monitoring import (
    Alert,
    AlertDeliveryError,
    AlertManager,
    AlertRule,
    AlertSink,
    HealthCheck,
    HealthRegistry,
    HealthStatus,
    LogAlertSink,
    Metric,
    MetricKind,
    MetricRegistry,
    MonitoringConfig,
    MonitoringService,
    ProductionJsonFormatter,
    WebhookAlertSink,
    build_monitoring_service,
    setup_production_logging,
)
from fpl_intelligence.deployment.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    DeadLetterSink,
    RecordingDeadLetterSink,
    RecoveryEntry,
    RecoveryManager,
    RecoveryReport,
    RetryOutcome,
    RetryPolicy,
    retry,
)
from fpl_intelligence.deployment.runner import (
    DeploymentReport,
    DeploymentRunner,
    ReadinessCheck,
    ReadinessReport,
    deploy,
)

__all__ = [
    "Alert",
    "AlertDeliveryError",
    "AlertManager",
    "AlertRule",
    "AlertSink",
    "CONFIG_SCHEMA_VERSION",
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
    "DeadLetterSink",
    "DEFAULT_CONFIG_PATH",
    "DeploymentReport",
    "DeploymentRunner",
    "DockerBuildConfig",
    "DockerBuildResult",
    "DockerBuilder",
    "DockerError",
    "DockerfileIssue",
    "DockerfileValidationReport",
    "ENV_FIELD_MAP",
    "HealthCheck",
    "HealthRegistry",
    "HealthStatus",
    "LogAlertSink",
    "Metric",
    "MetricKind",
    "MetricRegistry",
    "MonitoringConfig",
    "MonitoringService",
    "PRODUCTION_ENV",
    "ProductionConfig",
    "ProductionConfigError",
    "ProductionJsonFormatter",
    "ReadinessCheck",
    "ReadinessReport",
    "RecordingDeadLetterSink",
    "RecoveryEntry",
    "RecoveryManager",
    "RecoveryReport",
    "RetryOutcome",
    "RetryPolicy",
    "SECRET_FIELDS",
    "SubprocessDockerBuilder",
    "WebhookAlertSink",
    "build_docker_image",
    "build_monitoring_service",
    "deploy",
    "get_production_config",
    "load_production_config",
    "retry",
    "setup_production_logging",
    "validate_dockerfile",
    "validate_production_config",
]