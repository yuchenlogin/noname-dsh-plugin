"""Plugin runtime: crystallised capability, never a bypass around the kernel.

A plugin is a *verified crystallisation* of a capability, not a default entry
point for exploration (the vision's core bet: more and more workflows can be
synthesised on the spot, and only proven ones crystallise into plugins).  The
kernel's guarantees are non-negotiable, so a plugin:

- **cannot bypass evidence, approval or the ledger.**  Any tool a plugin
  contributes is registered through the normal :class:`ToolRegistry`, which
  enforces the approval gate.  There is no side channel.
- **declares a manifest** (capabilities, permissions, version compatibility,
  migrations) that is validated before anything loads.
- **binds side effects to a reversible lifecycle.**  Unloading a plugin
  reclaims everything it registered; the kernel's monotonicity tombstones
  still apply, so an unloaded plugin cannot be used to weaken a tool's gate.
- **cannot silently register process-global state.**  Plugin tools default to
  a non-global scope and must explicitly request any wider visibility.

This is the conservative contract.  It does not implement a plugin *market*
or dynamic workflow synthesis -- only the loading, validation, lifecycle and
audit seam that a verified capability crystallises into.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .store import HarnessStore, _now
from .tools import Tool, ToolRegistry

# The harness's current plugin-interface version.  A plugin declares the range
# it is compatible with; loading outside that range is refused.
PLUGIN_INTERFACE_VERSION = 1


class PluginError(Exception):
    """Raised on manifest validation, loading or lifecycle failures."""


@dataclass(frozen=True)
class PluginManifest:
    """What a plugin declares before it is allowed to load."""

    id: str
    version: str
    # Capabilities the plugin provides (free-form names, e.g. "web-search").
    capabilities: tuple[str, ...]
    # The highest permission any contributed tool needs.
    max_permission: str = "read"
    # Inclusive [min, max] harness interface versions the plugin supports.
    min_interface: int = 1
    max_interface: int = PLUGIN_INTERFACE_VERSION
    # Data migrations the plugin may need to run, by target schema version.
    migrations: tuple[int, ...] = ()
    # Whether the plugin requests global visibility for its tools.
    requests_global_scope: bool = False
    # Side effects the plugin may have beyond ToolRegistry tools -- e.g.
    # "network-egress", "billing".  A model-adapter plugin contributes no tools
    # but performs network egress and metered billing; it MUST declare that
    # here so the manifest is honest and the plugin.loaded ledger event records
    # the true posture (max_permission covers only contributed tools).
    side_effects: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise PluginError("plugin id cannot be empty")
        if not self.version.strip():
            raise PluginError("plugin version cannot be empty")
        if self.max_permission not in {"read", "write", "destructive"}:
            raise PluginError(f"invalid max_permission: {self.max_permission}")
        if self.min_interface < 1 or self.max_interface < self.min_interface:
            raise PluginError("invalid interface compatibility range")
        if not self.capabilities:
            raise PluginError("a plugin must declare at least one capability")


@dataclass(frozen=True)
class PluginContribution:
    """A tool a plugin wants to contribute, declared up front."""

    tool: Tool


@dataclass
class Plugin:
    """A plugin = manifest + a factory for its contributions.

    ``build`` is called at load time with no arguments and returns the plugin's
    contributions.  Keeping construction behind a factory means nothing runs --
    and no state is touched -- until the manifest has been validated.

    ``unload`` is an optional hook called during :meth:`PluginRuntime.unload`,
    after the plugin's tools have been reclaimed and before the
    ``plugin.unloaded`` ledger event is written.  It lets a plugin bind
    non-tool side effects (e.g. a generator's network egress) to the
    reversible lifecycle, so a leaked handle cannot outlive the plugin.
    """

    manifest: PluginManifest
    build: Callable[[], list[PluginContribution]]
    unload: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        if not callable(self.build):
            raise PluginError("plugin build must be callable")
        if self.unload is not None and not callable(self.unload):
            raise PluginError("plugin unload must be callable")


@dataclass
class PluginRuntime:
    """Load, validate, audit and unload plugins against the kernel."""

    store: HarnessStore
    registry: ToolRegistry
    _loaded: dict[str, Plugin] = field(default_factory=dict)
    # Tools each plugin registered, so unload can reclaim them reversibly.
    _contributions: dict[str, list[str]] = field(default_factory=dict)

    def load(self, plugin: Plugin) -> dict[str, Any]:
        manifest = plugin.manifest
        if manifest.id in self._loaded:
            raise PluginError(f"plugin already loaded: {manifest.id}")
        self._validate_compatibility(manifest)

        # Build the plugin's contributions.  ``build()`` is arbitrary host
        # code, so the kernel cannot make it safe -- but it *can* make it
        # auditable: the ledger records that plugin code ran (and if it
        # failed), and any exception is normalised into a PluginError rather
        # than leaking a raw traceback through the seam.
        self.store.append_event("system", "plugin.build_started", {"plugin_id": manifest.id})
        try:
            contributions = plugin.build()
        except Exception as exc:  # noqa: BLE001 - normalised at the seam
            self.store.append_event(
                "system",
                "plugin.build_failed",
                {"plugin_id": manifest.id, "error": str(exc)},
            )
            raise PluginError(f"plugin {manifest.id} build() failed: {exc}") from exc
        tools = self._validate_contributions(manifest, contributions)

        # Register each tool through the normal registry -- the approval gate
        # and shadowing monotonicity apply to plugins exactly as to anything
        # else.  The whole load (registration + ledger record) is atomic: on
        # any failure every change is rolled back, including restoring any
        # pre-existing tool a contribution displaced, so a failed load never
        # leaves a zombie or a destroyed host tool behind.
        registered: list[str] = []
        displaced: list[Tool | None] = []
        try:
            for tool in tools:
                outcome = self.registry.register(tool)
                registered.append(tool.schema.name)
                displaced.append(outcome.get("displaced"))
            self.store.append_event(
                "system",
                "plugin.loaded",
                {
                    "plugin_id": manifest.id,
                    "version": manifest.version,
                    "capabilities": list(manifest.capabilities),
                    "tools": registered,
                    "max_permission": manifest.max_permission,
                    "side_effects": list(manifest.side_effects),
                },
            )
        except Exception:
            self._rollback(registered, displaced)
            self.store.append_event(
                "system",
                "plugin.load_failed",
                {"plugin_id": manifest.id, "rolled_back": registered},
            )
            raise

        self._loaded[manifest.id] = plugin
        self._contributions[manifest.id] = registered
        return {"loaded": manifest.id, "version": manifest.version, "tools": registered}

    def _rollback(self, registered: list[str], displaced: list[Tool | None]) -> None:
        """Undo a partial load, restoring any tools that were displaced.

        Rollback is not just ``unregister``: a contribution that shadowed a
        pre-existing tool must put the original back, so a failed load cannot
        destroy host state.  Restoring re-registers the displaced tool, which
        bumps its generation again -- so any approval granted for the transient
        shadow is invalidated too.
        """

        for name, original in zip(reversed(registered), reversed(displaced)):
            self.registry.unregister(name)
            if original is not None:
                # The displaced tool is an exact Tool instance; put it back via
                # the restore path, which is an undo -- the transient shadow may
                # have ratcheted the tombstone, and restoring must not trip it.
                self.registry.register(original, _restore=True)

    def unload(self, plugin_id: str) -> dict[str, Any]:
        plugin = self._loaded.pop(plugin_id, None)
        if plugin is None:
            raise PluginError(f"plugin not loaded: {plugin_id}")
        # Reclaim every contribution.  The registry's tombstones still record
        # the strongest gate each tool name ever had, so unloading cannot be
        # used to smuggle a weaker tool back under the same name.
        reclaimed = []
        for name in self._contributions.pop(plugin_id, []):
            if self.registry.unregister(name):
                reclaimed.append(name)
        # A plugin may bind non-tool side effects to the lifecycle (e.g.
        # deactivating a generator whose only handle escaped).  The hook runs
        # after contributions are reclaimed, as part of the unload.
        if plugin.unload is not None:
            plugin.unload()
        self.store.append_event(
            "system",
            "plugin.unloaded",
            {"plugin_id": plugin_id, "reclaimed_tools": reclaimed},
        )
        return {"unloaded": plugin_id, "reclaimed_tools": reclaimed}

    def loaded_plugins(self) -> list[dict[str, Any]]:
        return [
            {
                "plugin_id": plugin.manifest.id,
                "version": plugin.manifest.version,
                "capabilities": list(plugin.manifest.capabilities),
                "tools": list(self._contributions.get(plugin.manifest.id, [])),
            }
            for plugin in self._loaded.values()
        ]

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------
    def _validate_compatibility(self, manifest: PluginManifest) -> None:
        if not (manifest.min_interface <= PLUGIN_INTERFACE_VERSION <= manifest.max_interface):
            raise PluginError(
                f"plugin {manifest.id} supports interface "
                f"[{manifest.min_interface}, {manifest.max_interface}], "
                f"harness is {PLUGIN_INTERFACE_VERSION}"
            )

    def _validate_contributions(
        self, manifest: PluginManifest, contributions: list[PluginContribution]
    ) -> list[Tool]:
        if not isinstance(contributions, list):
            raise PluginError("plugin build() must return a list of contributions")
        tools: list[Tool] = []
        rank = {"read": 0, "write": 1, "destructive": 2}
        for contribution in contributions:
            if not isinstance(contribution, PluginContribution):
                raise PluginError("every contribution must be a PluginContribution")
            tool = contribution.tool
            # Only exact Tool instances (the registry enforces this too, but
            # fail early with a plugin-specific message).
            if type(tool) is not Tool:
                raise PluginError("plugin contributions must be exact Tool instances")
            # A plugin tool may not exceed the manifest's declared permission.
            if rank[tool.permission] > rank[manifest.max_permission]:
                raise PluginError(
                    f"tool '{tool.schema.name}' permission {tool.permission} "
                    f"exceeds manifest max_permission {manifest.max_permission}"
                )
            # No silent process-global state: a plugin tool must not be global
            # unless the manifest explicitly requested it.
            if tool.scope == "global" and not manifest.requests_global_scope:
                raise PluginError(
                    f"tool '{tool.schema.name}' uses global scope but the manifest "
                    "does not request it; plugin tools default to non-global"
                )
            tools.append(tool)
        return tools
