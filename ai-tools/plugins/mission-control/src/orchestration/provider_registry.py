from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .executors import ClaudeExecutor, CursorExecutor, LocalExecutor
from .failures import FailureCode, WorkflowError
from .review import ClaudeReviewRunner


class ProviderRegistry:
    """Creates approved adapters from validated provider manifests."""

    def __init__(
        self,
        root: Path,
        providers: Mapping[str, Mapping[str, Any]],
        binary_overrides: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.root = root
        self.providers = providers
        self.binary_overrides = dict(binary_overrides or {})
        unknown = set(self.binary_overrides) - set(providers)
        if unknown:
            raise WorkflowError(
                FailureCode.CONFIGURATION,
                f"binary override references unknown providers: {sorted(unknown)}",
                False,
            )

    def create_executors(self, enabled: Any) -> Dict[str, Any]:
        executors: Dict[str, Any] = {}
        for provider_id in enabled:
            manifest = self.providers[provider_id]
            adapter = manifest["adapter"]
            binary = self.binary_overrides.get(provider_id, manifest.get("binary"))
            if adapter == "local":
                executors[provider_id] = LocalExecutor(name=provider_id)
            elif adapter == "cursor-cli":
                executors[provider_id] = CursorExecutor(
                    self.root, binary or "cursor-agent", name=provider_id
                )
            elif adapter == "claude-cli":
                executors[provider_id] = ClaudeExecutor(
                    self.root, binary or "claude", name=provider_id,
                    allowed_bash_rules=manifest.get("allowed_bash_rules", []),
                )
            else:
                raise WorkflowError(
                    FailureCode.CONFIGURATION,
                    f"provider {provider_id} uses unsupported adapter {adapter}",
                    False,
                )
        return executors

    def create_reviewer(self, profile_name: str, profiles: Mapping[str, Any]) -> Any:
        profile = profiles["profiles"][profile_name]
        provider_id = profile["executor"]
        manifest = self.providers[provider_id]
        if manifest["adapter"] != "claude-cli":
            raise WorkflowError(
                FailureCode.CONFIGURATION,
                f"provider {provider_id} does not implement semantic review",
                False,
            )
        binary = self.binary_overrides.get(provider_id, manifest.get("binary")) or "claude"
        return ClaudeReviewRunner(
            self.root,
            binary,
            model=profile.get("model") or "opus",
            effort=profile.get("effort") or "",
            timeout_seconds=profile["timeout_seconds"],
        )

    def binaries(self) -> Dict[str, Optional[str]]:
        return {
            provider_id: self.binary_overrides.get(provider_id, manifest.get("binary"))
            for provider_id, manifest in self.providers.items()
        }
