"""Bounded gauges from the existing monitor's fenced refresh/header observations."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from cognis.api.models import CodexUsageWindowResponse, ProviderUsageResponse
from cognis.core.usage.capture_models import UsageProviderSample
from cognis.core.usage.projector import as_utc

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UsageQuotaDiagnostics:
    """Omitted/rejected observations are not claimed as complete quota history."""

    invalid_windows: int
    limited_reports: int
    rejected_samples: int
    failed_samples: int


class UsageQuotaRecorder:
    """No polling or upstream calls; queue safe gauges after fenced publication."""

    def __init__(self, sink: Callable[[UsageProviderSample], bool]) -> None:
        self.sink = sink
        self.invalid_windows = 0
        self.limited_reports = 0
        self.rejected_samples = 0
        self.failed_samples = 0

    @property
    def diagnostics(self) -> UsageQuotaDiagnostics:
        """Return local content-free coverage diagnostics."""
        return UsageQuotaDiagnostics(
            self.invalid_windows,
            self.limited_reports,
            self.rejected_samples,
            self.failed_samples,
        )

    @staticmethod
    def _timestamp(value: str | None) -> datetime | None:
        if value is None:
            return None
        if len(value) > 64:
            raise ValueError("Quota timestamp exceeds the metadata bound")
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError("Quota source timestamp has no timezone")
        return as_utc(result)

    def observe(
        self,
        provider_id: str,
        provider_owner: str,
        configuration_generation: str,
        observation_id: str,
        observed_at: datetime,
        report: ProviderUsageResponse,
    ) -> None:
        """Never retain headers, URLs, plan text, limit names or response bodies."""
        if not report.ok:
            return
        header = report.source == "anthropic_rate_limit_headers"
        if not header and report.source not in {
            "chatgpt_codex_usage",
            "anthropic_subscription_usage",
            "elevenlabs_subscription_usage",
        }:
            return
        try:
            at = self._timestamp(report.observed_at if header else report.fetched_at)
            if header and at is None:
                raise ValueError("Header observation has no source timestamp")
            at = at or as_utc(observed_at)
            if header:
                observation_id = (
                    "uho_"
                    + hashlib.sha256(
                        f"{provider_id}:{configuration_generation}:{at.isoformat()}".encode()
                    ).hexdigest()
                )
        except (TypeError, ValueError, OverflowError):
            self.invalid_windows += 1
            return
        windows: list[tuple[str, CodexUsageWindowResponse]] = []
        for name, window in (("primary", report.primary), ("secondary", report.secondary)):
            if window is not None:
                windows.append((name, window))
        if len(report.additional_rate_limits) > 3:
            self.limited_reports += 1
        for additional in report.additional_rate_limits[:3]:
            if additional.limit_id is None or len(additional.limit_id) > 160:
                self.invalid_windows += 1
                continue
            identity = hashlib.sha256(additional.limit_id.encode()).hexdigest()
            for name, window in (
                ("primary", additional.primary),
                ("secondary", additional.secondary),
            ):
                if window is not None:
                    windows.append((f"additional:{identity}:{name}", window))
        values: list[
            tuple[str, Decimal | None, Decimal | None, Decimal | None, datetime | None]
        ] = []
        for name, window in windows:
            try:
                values.append(
                    (
                        name,
                        Decimal(window.limit) if window.limit is not None else None,
                        Decimal(window.remaining) if window.remaining is not None else None,
                        Decimal(str(window.used_percent)),
                        self._timestamp(window.resets_at),
                    )
                )
            except (ValueError, TypeError, InvalidOperation, OverflowError):
                self.invalid_windows += 1
        if report.credits is not None and report.credits.balance is not None:
            try:
                if len(str(report.credits.balance)) > 128:
                    raise ValueError("Quota balance exceeds the metadata bound")
                values.append(("credits", None, Decimal(str(report.credits.balance)), None, None))
            except (ValueError, TypeError, InvalidOperation):
                self.invalid_windows += 1
        if len(values) > 8:
            self.limited_reports += 1
        for name, limit_value, remaining, utilization, reset_at in values[:8]:
            try:
                sample = UsageProviderSample(
                    sample_id="ups_"
                    + hashlib.sha256(
                        f"{provider_id}:{configuration_generation}:{observation_id}:{name}".encode()
                    ).hexdigest(),
                    provider_id=provider_id,
                    provider_owner=provider_owner,
                    account_scope_id=provider_id,
                    configuration_generation=configuration_generation,
                    source="response_header" if header else "refresh",
                    observation_id=observation_id,
                    window_key=name,
                    observed_at=at,
                    reset_at=reset_at,
                    limit_value=limit_value,
                    remaining=remaining,
                    utilization=utilization,
                )
            except (ValueError, TypeError, OverflowError):
                self.invalid_windows += 1
                continue
            try:
                if not self.sink(sample):
                    self.rejected_samples += 1
            except Exception as exc:
                self.failed_samples += 1
                logger.warning("Usage quota sink failed: %s", type(exc).__name__)
