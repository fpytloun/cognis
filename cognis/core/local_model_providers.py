"""Provider host-scope resolution and deterministic local-provider selection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.models.executor_inference import (
    executor_local_inference_configured,
    resolve_executor_local_inference_config,
)
from cognis.models.local_models import (
    LocalModelProviderCandidate,
    LocalModelProviderRecommendationResponse,
    LocalModelSelector,
)
from cognis.ownership import SYSTEM_USER_EMAIL, is_shared_owner_email
from cognis.store.local_models import list_active_executor_rows, lock_local_model_dispatch_guard
from cognis.store.models import ExecutorRow, LLMProvider

MANAGED_LOCAL_METADATA_KEY = "cognis_managed_local"


class LocalModelHostEligibility(Protocol):
    """Seam for WS-A capability-aware managed-host filtering."""

    def eligible(self, executor: ExecutorRow) -> bool:
        """Return whether a host can participate in managed local inference."""


class ManagedOllamaHostEligibility:
    """Require WS-A's effective controller-managed Ollama capability."""

    def eligible(self, executor: ExecutorRow) -> bool:
        config = resolve_executor_local_inference_config(
            executor.config if isinstance(executor.config, dict) else {}
        )
        return executor_local_inference_configured(executor) and config.ollama_management_enabled


def _labels_match(row: ExecutorRow, labels: dict[str, str]) -> bool:
    row_labels = row.labels if isinstance(row.labels, dict) else {}
    return all(
        isinstance(row_labels.get(key), str) and row_labels[key] == value
        for key, value in labels.items()
    )


def _provider_selector(provider: LLMProvider) -> LocalModelSelector | None:
    config = provider.config if isinstance(provider.config, dict) else {}
    executor_id = config.get("executor_id")
    labels = config.get("executor_labels")
    if isinstance(executor_id, str) and executor_id.strip():
        return LocalModelSelector(executor_ids=[executor_id.strip()])
    if isinstance(labels, dict) and labels:
        try:
            return LocalModelSelector(match_labels=labels)
        except ValueError:
            return None
    return None


def _provider_is_ollama(provider: LLMProvider) -> bool:
    config = provider.config if isinstance(provider.config, dict) else {}
    return (
        provider.status == "active"
        and provider.location == "executor"
        and str(config.get("preset") or "").strip().lower() == "ollama"
    )


def _provider_contains_model(provider: LLMProvider, runtime_name: str) -> bool:
    config = provider.config if isinstance(provider.config, dict) else {}
    models = config.get("models")
    return isinstance(models, list) and any(
        isinstance(entry, dict) and entry.get("model_id") == runtime_name for entry in models
    )


def _provider_is_managed_local(provider: LLMProvider) -> bool:
    config = provider.config if isinstance(provider.config, dict) else {}
    metadata = config.get(MANAGED_LOCAL_METADATA_KEY)
    return isinstance(metadata, dict) and metadata.get("managed") is True


@dataclass(frozen=True)
class ResolvedProvider:
    provider: LLMProvider
    hosts: tuple[ExecutorRow, ...]


class LocalModelProviderResolver:
    """Resolve authorized provider host scopes without runtime capability coupling."""

    def __init__(self, eligibility: LocalModelHostEligibility | None = None) -> None:
        self._eligibility = eligibility or ManagedOllamaHostEligibility()

    def eligible(self, executor: ExecutorRow) -> bool:
        """Expose the shared eligibility decision to provider creation."""

        return self._eligibility.eligible(executor)

    async def resolve(
        self,
        session: AsyncSession,
        provider: LLMProvider,
        *,
        actor_email: str,
        actor_role: str,
        shared: bool,
    ) -> ResolvedProvider | None:
        if not _provider_is_ollama(provider):
            return None
        owner_email = provider.owner_email or SYSTEM_USER_EMAIL
        if shared:
            if owner_email != SYSTEM_USER_EMAIL or actor_role != "admin":
                return None
        elif owner_email not in {actor_email, SYSTEM_USER_EMAIL} or (
            owner_email == SYSTEM_USER_EMAIL and actor_role != "admin"
        ):
            return None

        selector = _provider_selector(provider)
        if selector is None:
            return None
        rows = await list_active_executor_rows(session)
        authorized = [
            row
            for row in rows
            if self.eligible(row)
            and (
                is_shared_owner_email(row.owner_email)
                if owner_email == SYSTEM_USER_EMAIL
                else row.owner_email == owner_email
            )
        ]
        by_id = {row.executor_id: row for row in authorized}
        selected_ids = {
            executor_id for executor_id in selector.executor_ids if executor_id in by_id
        }
        if selector.match_labels:
            selected_ids.update(
                row.executor_id for row in authorized if _labels_match(row, selector.match_labels)
            )
        hosts = tuple(by_id[executor_id] for executor_id in sorted(selected_ids))
        return ResolvedProvider(provider=provider, hosts=hosts) if hosts else None


class LocalModelProviderService:
    """Recommend and idempotently create reusable executor-routed Ollama providers."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        actor_email: str,
        actor_role: str,
        resolver: LocalModelProviderResolver | None = None,
    ) -> None:
        self.session = session
        self.actor_email = actor_email
        self.actor_role = actor_role
        self.resolver = resolver or LocalModelProviderResolver()

    async def recommend(
        self,
        *,
        runtime_name: str,
        selector: LocalModelSelector | None,
        shared: bool,
    ) -> LocalModelProviderRecommendationResponse:
        providers = list(
            (
                await self.session.execute(
                    select(LLMProvider).order_by(LLMProvider.provider_id.asc())
                )
            )
            .scalars()
            .all()
        )
        selected_ids: set[str] | None = None
        if selector is not None:
            selected_ids = {
                row.executor_id
                for row in await self._resolve_requested_hosts(selector, shared=shared)
            }
        ranked: list[tuple[tuple[object, ...], LocalModelProviderCandidate]] = []
        for provider in providers:
            resolved = await self.resolver.resolve(
                self.session,
                provider,
                actor_email=self.actor_email,
                actor_role=self.actor_role,
                shared=shared,
            )
            if resolved is None:
                continue
            host_ids = {row.executor_id for row in resolved.hosts}
            if selected_ids is not None and not selected_ids.issubset(host_ids):
                continue
            contains_model = _provider_contains_model(provider, runtime_name)
            managed_local = _provider_is_managed_local(provider)
            healthy_count = sum(
                row.runtime_state in {"online", "ready", "active"} for row in resolved.hosts
            )
            owner_email = provider.owner_email or SYSTEM_USER_EMAIL
            user_owned = owner_email == self.actor_email
            reasons = ["compatible_ollama_provider", "target_subset"]
            if contains_model:
                reasons.append("model_already_configured")
            if healthy_count:
                reasons.append("healthy_hosts")
            if user_owned and not shared:
                reasons.append("user_owned")
            if managed_local:
                reasons.append("managed_local_reusable")
            candidate = LocalModelProviderCandidate(
                provider_id=provider.provider_id,
                display_name=provider.display_name,
                owner_email=owner_email,
                executor_ids=sorted(host_ids),
                contains_model=contains_model,
                managed_local=managed_local,
                healthy_host_count=healthy_count,
                reason_codes=reasons,
            )
            rank = (
                not contains_model,
                -healthy_count,
                not (user_owned and not shared),
                not managed_local,
                -len(host_ids),
                provider.provider_id,
            )
            ranked.append((rank, candidate))
        candidates = [candidate for _rank, candidate in sorted(ranked, key=lambda item: item[0])]
        return LocalModelProviderRecommendationResponse(
            requested_ref=runtime_name,
            runtime_name=runtime_name,
            recommended_provider_id=candidates[0].provider_id if candidates else None,
            candidates=candidates,
        )

    async def find_or_create(
        self,
        *,
        runtime_name: str,
        selector: LocalModelSelector,
        shared: bool,
        force_create: bool,
    ) -> tuple[LLMProvider, bool, str]:
        if shared and self.actor_role != "admin":
            raise PermissionError("only admins can create shared local providers")
        await lock_local_model_dispatch_guard(self.session)
        recommendation = await self.recommend(
            runtime_name=runtime_name,
            selector=selector,
            shared=shared,
        )
        if recommendation.candidates and not force_create:
            provider = await self.session.get(
                LLMProvider,
                recommendation.candidates[0].provider_id,
            )
            assert provider is not None
            return provider, False, "reused_eligible_provider"

        hosts = await self._resolve_requested_hosts(selector, shared=shared)
        selector_document = selector.model_dump(mode="json")
        owner_email = SYSTEM_USER_EMAIL if shared else self.actor_email
        key_document = {
            "owner_email": owner_email,
            "runtime": "ollama",
            "selector": selector_document,
        }
        managed_key = hashlib.sha256(
            json.dumps(key_document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        existing = (
            await self.session.execute(
                select(LLMProvider).where(LLMProvider.managed_local_key == managed_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False, "reused_managed_provider_key"

        provider_id = f"ollama-local-{managed_key[:16]}"
        routing: dict[str, object]
        if selector.executor_ids:
            host_label = ", ".join(row.name for row in hosts)
            routing = {"executor_id": selector.executor_ids[0]}
            if len(selector.executor_ids) > 1:
                raise ValueError("managed providers support one exact executor or a label selector")
        else:
            host_label = ", ".join(
                f"{key}={value}" for key, value in sorted(selector.match_labels.items())
            )
            routing = {"executor_labels": dict(sorted(selector.match_labels.items()))}
        provider = LLMProvider(
            provider_id=provider_id,
            display_name=f"Ollama ({host_label})",
            location="executor",
            backend="litellm",
            owner_email=owner_email,
            config={
                "preset": "ollama",
                "scope": "system" if shared else "user",
                **routing,
                "models": [],
                MANAGED_LOCAL_METADATA_KEY: {
                    "managed": True,
                    "runtime": "ollama",
                    "selector": selector_document,
                },
            },
            managed_local_key=managed_key,
            status="active",
        )
        self.session.add(provider)
        await self.session.flush()
        return provider, True, "created_managed_provider"

    async def _resolve_requested_hosts(
        self,
        selector: LocalModelSelector,
        *,
        shared: bool,
    ) -> list[ExecutorRow]:
        rows = await list_active_executor_rows(self.session)
        authorized = [
            row
            for row in rows
            if self.resolver.eligible(row)
            and (
                is_shared_owner_email(row.owner_email)
                if shared
                else row.owner_email == self.actor_email
            )
        ]
        by_id = {row.executor_id: row for row in authorized}
        selected: set[str] = set()
        for executor_id in selector.executor_ids:
            if executor_id not in by_id:
                visible = next(
                    (
                        row
                        for row in rows
                        if row.executor_id == executor_id
                        and (
                            is_shared_owner_email(row.owner_email)
                            if shared
                            else row.owner_email == self.actor_email
                        )
                    ),
                    None,
                )
                if visible is not None:
                    raise ValueError(
                        f"executor {executor_id!r} is not eligible for managed Ollama; "
                        "enable Local inference and Allow Cognis to manage Ollama models "
                        "in executor settings, then reconnect it"
                    )
                raise LookupError(f"executor {executor_id!r} not found")
            selected.add(executor_id)
        if selector.match_labels:
            selected.update(
                row.executor_id for row in authorized if _labels_match(row, selector.match_labels)
            )
        if not selected:
            raise ValueError("selector matched no eligible managed hosts")
        return [by_id[executor_id] for executor_id in sorted(selected)]
