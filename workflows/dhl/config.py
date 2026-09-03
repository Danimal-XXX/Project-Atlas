"""Configuration boundary for the DHL Express MyDHL API."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv


TEST_BASE_URL = "https://express.api.dhl.com/mydhlapi/test"
PRODUCTION_BASE_URL = "https://express.api.dhl.com/mydhlapi"


class DHLConfigurationError(RuntimeError):
    """Raised when DHL configuration is missing or unsafe."""


class DHLEnvironment(str, Enum):
    """Supported MyDHL API environments."""

    TEST = "test"
    PRODUCTION = "production"


@dataclass(frozen=True)
class DHLConfig:
    """MyDHL connection settings with production disabled by default."""

    username: str
    password: str
    account_number: str
    environment: DHLEnvironment = DHLEnvironment.TEST
    production_enabled: bool = False
    approver_id: str | None = None
    approval_ledger_path: Path | None = None
    allowed_production_draft_id: str | None = None
    timeout: float = 45.0
    max_read_retries: int = 3

    @property
    def base_url(self) -> str:
        return (
            PRODUCTION_BASE_URL
            if self.environment is DHLEnvironment.PRODUCTION
            else TEST_BASE_URL
        )

    def assert_environment_safe(self) -> None:
        """Block production unless both production controls are configured."""
        if self.environment is not DHLEnvironment.PRODUCTION:
            return
        if not self.production_enabled:
            raise DHLConfigurationError(
                "DHL production is disabled; use the test environment"
            )
        if not self.approver_id:
            raise DHLConfigurationError(
                "DHL_APPROVER_ID is required for production"
            )
        if self.approval_ledger_path is None:
            raise DHLConfigurationError(
                "DHL_APPROVAL_LEDGER_PATH is required for production"
            )
        if str(self.approval_ledger_path) == ":memory:":
            raise DHLConfigurationError(
                "DHL production requires a persistent approval ledger"
            )
        if not self.allowed_production_draft_id:
            raise DHLConfigurationError(
                "DHL_PRODUCTION_ALLOWED_DRAFT_ID is required for production"
            )

    @classmethod
    def from_env(cls) -> "DHLConfig":
        """Load credentials without ever including their values in errors."""
        load_dotenv()
        values = {
            "DHL_API_USERNAME": os.getenv("DHL_API_USERNAME"),
            "DHL_API_PASSWORD": os.getenv("DHL_API_PASSWORD"),
            "DHL_ACCOUNT_NUMBER": os.getenv("DHL_ACCOUNT_NUMBER"),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise DHLConfigurationError(
                "Missing DHL environment values: " + ", ".join(missing)
            )
        raw_environment = os.getenv("DHL_ENVIRONMENT", "test").strip().lower()
        try:
            environment = DHLEnvironment(raw_environment)
        except ValueError as error:
            raise DHLConfigurationError(
                "DHL_ENVIRONMENT must be 'test' or 'production'"
            ) from error
        production_enabled = (
            os.getenv("DHL_ENABLE_PRODUCTION", "false").strip().lower() == "true"
        )
        config = cls(
            username=str(values["DHL_API_USERNAME"]),
            password=str(values["DHL_API_PASSWORD"]),
            account_number=str(values["DHL_ACCOUNT_NUMBER"]),
            environment=environment,
            production_enabled=production_enabled,
            approver_id=os.getenv("DHL_APPROVER_ID") or None,
            approval_ledger_path=(
                Path(os.environ["DHL_APPROVAL_LEDGER_PATH"]).expanduser()
                if os.getenv("DHL_APPROVAL_LEDGER_PATH")
                else None
            ),
            allowed_production_draft_id=(
                os.getenv("DHL_PRODUCTION_ALLOWED_DRAFT_ID") or None
            ),
        )
        config.assert_environment_safe()
        return config
