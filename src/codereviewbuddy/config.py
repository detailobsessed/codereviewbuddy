"""Per-reviewer configuration system.

Loads ``.codereviewbuddy.toml`` from the project root (walking up to ``.git``),
validates with Pydantic, and provides sensible defaults so zero-config still works.
"""

from __future__ import annotations

import logging
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)

CONFIG_FILENAME = ".codereviewbuddy.toml"


class Severity(StrEnum):
    """Comment severity levels, ordered from least to most critical."""

    INFO = "info"
    WARNING = "warning"
    FLAGGED = "flagged"
    BUG = "bug"


# -- Per-reviewer defaults (match current hardcoded adapter behavior) ----------

_REVIEWER_DEFAULTS: dict[str, dict[str, Any]] = {
    "devin": {
        "enabled": True,
        "auto_resolve_stale": False,  # Devin auto-resolves its own bug threads
        "resolve_levels": [Severity.INFO],  # Only allow resolving info-level
    },
    "unblocked": {
        "enabled": True,
        "auto_resolve_stale": True,  # We batch-resolve Unblocked's stale threads
        "resolve_levels": [Severity.INFO, Severity.WARNING, Severity.FLAGGED, Severity.BUG],
        # rereview_message intentionally omitted — None means "use adapter default"
    },
    "coderabbit": {
        "enabled": True,
        "auto_resolve_stale": False,  # CodeRabbit handles its own resolution
        "resolve_levels": [],  # Don't resolve any CodeRabbit threads
    },
}


class ReviewerConfig(BaseModel):
    """Configuration for a single reviewer."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(default=True, description="Whether this reviewer integration is active")
    auto_resolve_stale: bool = Field(
        default=True,
        description="Whether resolve_stale_comments touches this reviewer's threads",
    )
    resolve_levels: list[Severity] = Field(
        default_factory=lambda: list(Severity),
        description="Severity levels that are allowed to be resolved",
    )
    rereview_message: str | None = Field(
        default=None,
        min_length=1,
        description="Custom message to post when triggering a re-review (only for manual-trigger reviewers)",
    )


class PRDescriptionsConfig(BaseModel):
    """Configuration for PR description management tools."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(default=True, description="Whether PR description tools are available")


class SelfImprovementConfig(BaseModel):
    """Configuration for agent-driven self-improvement feedback loop."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(default=False, description="Whether agents should file issues for server gaps they encounter")
    repo: str = Field(default="", description="Repository to file issues against (e.g. 'owner/codereviewbuddy')")

    @model_validator(mode="after")
    def _validate_repo_when_enabled(self) -> SelfImprovementConfig:
        if self.enabled and not self.repo.strip():
            msg = "[self_improvement] enabled=true requires a non-empty 'repo' field"
            raise ValueError(msg)
        return self


class DiagnosticsConfig(BaseModel):
    """Configuration for diagnostic and debugging features."""

    model_config = ConfigDict(extra="ignore")

    io_tap: bool = Field(default=False, description="Enable stdin/stdout logging for transport debugging (#65)")
    tool_call_heartbeat: bool = Field(
        default=False,
        description="Emit periodic in-flight heartbeat entries while tool calls are pending",
    )
    heartbeat_interval_ms: int = Field(
        default=5000,
        ge=100,
        description="Heartbeat interval in milliseconds for in-flight tool calls",
    )
    include_args_fingerprint: bool = Field(
        default=True,
        description="Include argument payload fingerprint and size metadata in tool call logs",
    )


class Config(BaseModel):
    """Top-level codereviewbuddy configuration."""

    model_config = ConfigDict(extra="ignore")

    reviewers: dict[str, ReviewerConfig] = Field(
        default_factory=dict,
        description="Per-reviewer configuration sections",
    )
    pr_descriptions: PRDescriptionsConfig = Field(
        default_factory=PRDescriptionsConfig,
        description="PR description management settings",
    )
    self_improvement: SelfImprovementConfig = Field(
        default_factory=SelfImprovementConfig,
        description="Agent self-improvement feedback loop settings",
    )
    diagnostics: DiagnosticsConfig = Field(
        default_factory=DiagnosticsConfig,
        description="Diagnostic and debugging settings",
    )

    @model_validator(mode="after")
    def _apply_reviewer_defaults(self) -> Config:
        """Fill in missing reviewers with their hardcoded defaults.

        For partially-specified reviewers, merge unset fields from
        ``_REVIEWER_DEFAULTS`` so that e.g. ``[reviewers.devin]\\nenabled = false``
        still gets ``auto_resolve_stale=False`` (Devin's safe default) rather
        than the generic ``ReviewerConfig`` field default (``True``).
        """
        for name, defaults in _REVIEWER_DEFAULTS.items():
            if name not in self.reviewers:
                self.reviewers[name] = ReviewerConfig(**defaults)
            else:
                rc = self.reviewers[name]
                for field_name, default_value in defaults.items():
                    if field_name not in rc.model_fields_set:
                        setattr(rc, field_name, default_value)
        return self

    def get_reviewer(self, name: str) -> ReviewerConfig:
        """Get config for a reviewer, falling back to permissive defaults for unknown reviewers."""
        if name in self.reviewers:
            return self.reviewers[name]
        # Unknown reviewer: enabled, all levels resolvable, auto-resolve on
        return ReviewerConfig()

    def can_resolve(self, reviewer_name: str, severity: Severity) -> tuple[bool, str]:
        """Check if resolving a thread is allowed by config.

        Args:
            reviewer_name: Name of the reviewer that posted the thread.
            severity: Severity of the thread (from the adapter's ``classify_severity``).

        Returns:
            (allowed, reason) — if not allowed, reason explains why.
        """
        rc = self.get_reviewer(reviewer_name)
        if not rc.enabled:
            return False, f"Reviewer '{reviewer_name}' is disabled in config"
        if severity not in rc.resolve_levels:
            return False, (
                f"Config blocks resolving {severity}-level threads from {reviewer_name}. "
                f"Allowed levels: {[s.value for s in rc.resolve_levels]}"
            )
        return True, ""


def _get_dict_value_model(annotation: Any) -> type[BaseModel] | None:
    """Extract the value model from ``dict[str, SomeModel]`` type annotations.

    Returns the model class if annotation is ``dict[str, <BaseModel subclass>]``,
    otherwise ``None``.
    """
    import typing  # noqa: PLC0415

    args = typing.get_args(annotation)
    if len(args) == 2 and isinstance(args[1], type) and issubclass(args[1], BaseModel):  # noqa: PLR2004
        return args[1]
    return None


def _collect_unknown_keys(
    data: dict[str, Any],
    model_cls: type[BaseModel],
    prefix: str = "",
) -> list[str]:
    """Recursively find keys in *data* that don't match any field in *model_cls*.

    Unknown reviewer *names* under ``[reviewers.*]`` are intentionally allowed
    (forward-compat for new reviewers).  Only unknown *keys within* known
    sections are flagged.

    Returns dotted key paths like ``pr_descriptions.require_review``.
    """
    known = set(model_cls.model_fields)
    unknown: list[str] = []

    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if key not in known:
            unknown.append(dotted)
            continue
        field_info = model_cls.model_fields[key]
        annotation = field_info.annotation
        # Recurse into sub-models
        if isinstance(annotation, type) and issubclass(annotation, BaseModel) and isinstance(value, dict):
            unknown.extend(_collect_unknown_keys(value, annotation, prefix=f"{dotted}."))
            continue
        # Recurse into dict[str, SubModel] (e.g. reviewers: dict[str, ReviewerConfig]).
        # Unknown dict *keys* are allowed (forward-compat for new reviewer names),
        # but unknown fields *within* each value are flagged.
        value_model = _get_dict_value_model(annotation)
        if value_model is not None and isinstance(value, dict):
            for sub_key, sub_value in value.items():
                if isinstance(sub_value, dict):
                    unknown.extend(_collect_unknown_keys(sub_value, value_model, prefix=f"{dotted}.{sub_key}."))

    return unknown


def _find_config_file(start: Path) -> Path | None:
    """Walk up from *start* looking for ``.codereviewbuddy.toml``, stopping at ``.git`` root."""
    current = start.resolve()
    while True:
        candidate = current / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
        # Stop at filesystem root
        if current.parent == current:
            return None
        # Stop if we just checked a directory that contains .git
        if (current / ".git").exists():
            return None
        current = current.parent


def load_config(cwd: str | Path | None = None) -> tuple[Config, Path | None]:
    """Load configuration from ``.codereviewbuddy.toml``.

    Walks up from *cwd* (defaulting to the current directory) looking for the
    config file.  If not found, returns a ``Config`` with all defaults.

    Returns:
        (config, config_path) — the parsed config and the file path (or None
        if no config file was found).  The path is needed by ``set_config``
        to enable mtime-based hot-reload.

    Raises ``ValueError`` on invalid TOML or validation errors so the server
    can refuse to start with a broken config.
    """
    start = Path(cwd) if cwd else Path.cwd()
    config_path = _find_config_file(start)

    if config_path is None:
        logger.info("No %s found, using defaults", CONFIG_FILENAME)
        return Config(), None

    logger.info("Loading config from %s", config_path)
    try:
        raw = config_path.read_text(encoding="utf-8")
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        msg = f"Invalid TOML in {config_path}: {exc}"
        raise ValueError(msg) from exc

    try:
        config = Config.model_validate(data)
    except Exception as exc:
        msg = f"Invalid config in {config_path}: {exc}"
        raise ValueError(msg) from exc

    unknown = _collect_unknown_keys(data, Config)
    for key in unknown:
        logger.warning(
            "Unknown config key '%s' in %s — run 'codereviewbuddy config --update' to clean up",
            key,
            config_path,
        )

    return config, config_path


# -- Hot-reloading config with mtime cache ------------------------------------


class _ConfigState:
    """Tracks the active config, its file path, and mtime for hot-reload."""

    __slots__ = ("_on_reload", "config", "mtime", "path")

    def __init__(self) -> None:
        self.config: Config = Config()
        self.path: Path | None = None
        self.mtime: float | None = None
        self._on_reload: list[_ReloadCallback] = []

    def register_reload_callback(self, callback: _ReloadCallback) -> None:
        """Register a callback to invoke after config hot-reload."""
        self._on_reload.append(callback)

    def _fire_reload_callbacks(self, config: Config) -> None:
        for cb in self._on_reload:
            try:
                cb(config)
            except Exception:
                logger.exception("Config reload callback failed")


_ReloadCallback = Any  # Callable[[Config], None] — avoid typing complexity

_state = _ConfigState()


def get_config() -> Config:
    """Return the active configuration, hot-reloading if the file changed.

    Checks the config file's mtime on each call (~microseconds).  If the file
    was modified, re-reads and re-validates it.  If deleted, falls back to
    defaults.  If invalid, logs a warning and keeps the last good config.
    """
    path = _state.path
    if path is None:
        # No config file was found at startup — nothing to hot-reload
        return _state.config

    try:
        current_mtime = path.stat().st_mtime
    except OSError:
        # File was deleted — fall back to defaults
        if _state.mtime is not None:
            logger.warning("%s deleted — falling back to defaults", path.name)
            _state.config = Config()
            _state.mtime = None
            _state._fire_reload_callbacks(_state.config)
        return _state.config

    if current_mtime == _state.mtime:
        return _state.config

    # File changed — reload
    logger.info("Config file changed (mtime %.0f → %.0f), reloading", _state.mtime or 0, current_mtime)
    try:
        raw = path.read_text(encoding="utf-8")
        data = tomllib.loads(raw)
        new_config = Config.model_validate(data)
    except (tomllib.TOMLDecodeError, Exception) as exc:
        logger.warning("Invalid config after edit — keeping last good config: %s", exc)
        _state.mtime = current_mtime  # Don't re-check until next change
        return _state.config

    _state.config = new_config
    _state.mtime = current_mtime
    _state._fire_reload_callbacks(new_config)
    logger.info("Config hot-reloaded successfully")
    return new_config


def set_config(config: Config, *, config_path: Path | None = None) -> None:
    """Set the active configuration (called during server startup).

    If *config_path* is provided, enables hot-reload on subsequent
    ``get_config()`` calls by tracking the file's mtime.
    """
    _state.config = config
    _state.path = config_path
    try:
        _state.mtime = config_path.stat().st_mtime if config_path else None
    except OSError:
        _state.mtime = None


def get_config_path() -> Path | None:
    """Return the path to the active config file, or None if using defaults."""
    return _state.path


def register_reload_callback(callback: Any) -> None:
    """Register a callable to invoke after config hot-reload.

    The callback receives the new ``Config`` instance.  Used by ``server.py``
    to re-apply adapter config and middleware diagnostics when the file changes.
    """
    _state.register_reload_callback(callback)


# -- Template sections for ``codereviewbuddy config`` --------------------------

# Each section is a (header_pattern, text_block) pair. The header_pattern is
# used to check whether the section already exists in the user's config file.
# The text_block is what gets written for --init or appended for --update.

_TEMPLATE_HEADER = """\
# .codereviewbuddy.toml — Per-reviewer configuration for codereviewbuddy
# All settings are optional. Omitted values use sensible defaults.
# Place this file in your project root (next to .git/).
#
# Severity levels used by resolve_levels:
#   bug      — 🔴 critical issues, must fix before merge
#   flagged  — 🚩 likely needs a code change
#   warning  — 🟡 worth addressing but not blocking
#   info     — 📝 informational, no action required
"""

_TEMPLATE_SECTIONS: list[tuple[str, str]] = [
    (
        "[reviewers.devin]",
        """\
[reviewers.devin]
enabled = true                    # Set to false to ignore Devin comments entirely
auto_resolve_stale = false        # Devin auto-resolves its own bug threads; we skip them
resolve_levels = ["info"]         # Only allow resolving info-level threads from Devin
""",
    ),
    (
        "[reviewers.unblocked]",
        """\
[reviewers.unblocked]
enabled = true
auto_resolve_stale = true         # We batch-resolve Unblocked's stale threads
resolve_levels = ["info", "warning", "flagged", "bug"]  # All levels allowed
# rereview_message = "@unblocked please re-review"  # Message posted to trigger re-review
""",
    ),
    (
        "[reviewers.coderabbit]",
        """\
[reviewers.coderabbit]
enabled = true
auto_resolve_stale = false        # CodeRabbit handles its own resolution
resolve_levels = []               # Don't resolve any CodeRabbit threads
""",
    ),
    (
        "[pr_descriptions]",
        """\
[pr_descriptions]
enabled = true                    # Set to false to disable PR description review tool
""",
    ),
    (
        "[self_improvement]",
        """\
[self_improvement]
enabled = true                    # Agents file issues when they encounter server gaps
repo = "detailobsessed/codereviewbuddy"  # Repository to file issues against
""",
    ),
    (
        "[diagnostics]",
        """\
[diagnostics]
io_tap = true                     # Log stdin/stdout for transport debugging (#65)
tool_call_heartbeat = false       # Emit heartbeat entries for long-running tool calls
heartbeat_interval_ms = 5000      # Heartbeat cadence when enabled
include_args_fingerprint = true   # Log args hash/size (no raw args)
""",
    ),
]

DEFAULT_CONFIG_TEMPLATE = _TEMPLATE_HEADER + "\n".join(block for _, block in _TEMPLATE_SECTIONS)


def init_config(cwd: Path | None = None) -> Path:
    """Create a new ``.codereviewbuddy.toml`` in the given directory.

    Raises ``SystemExit(1)`` if the file already exists.

    Returns:
        Path to the created file.
    """
    target = (cwd or Path.cwd()) / CONFIG_FILENAME
    if target.exists():
        print(f"Error: {CONFIG_FILENAME} already exists in {target.parent}")  # noqa: T201
        print("Hint: use 'codereviewbuddy config --update' to add new sections")  # noqa: T201
        raise SystemExit(1)
    target.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    print(f"Created {target}")  # noqa: T201
    return target


def _comment_out_unknown_keys(target: Path) -> list[str]:
    """Comment out unknown keys in a config file using tomlkit (style-preserving).

    Returns list of dotted key paths that were commented out.
    """
    import tomlkit  # noqa: PLC0415

    raw = target.read_text(encoding="utf-8")
    data = tomllib.loads(raw)
    unknown = _collect_unknown_keys(data, Config)
    if not unknown:
        return []

    doc = tomlkit.loads(raw)
    for dotted in unknown:
        parts = dotted.split(".")
        container = doc
        for part in parts[:-1]:
            container = container[part]  # type: ignore[not-subscriptable]
        key = parts[-1]
        value = container[key]  # type: ignore[not-subscriptable]
        del container[key]  # type: ignore[not-subscriptable]
        # Serialize the value safely — tables/dicts can't use the simple split trick
        try:
            if isinstance(value, dict):
                serialized = tomlkit.inline_table()
                serialized.update(value)
                value_str = str(serialized)
            else:
                value_str = tomlkit.dumps({"_": value}).split("= ", 1)[1].strip()
                if "\n" in value_str:
                    value_str = repr(value)
        except Exception:
            value_str = repr(value)
        container.add(tomlkit.comment(f"DEPRECATED: {key} = {value_str}"))  # type: ignore[possibly-missing-attribute]

    target.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return unknown


def _remove_unknown_keys(target: Path) -> list[str]:
    """Remove unknown keys from a config file using tomlkit (style-preserving).

    Returns list of dotted key paths that were removed.
    """
    import tomlkit  # noqa: PLC0415

    raw = target.read_text(encoding="utf-8")
    data = tomllib.loads(raw)
    unknown = _collect_unknown_keys(data, Config)
    if not unknown:
        return []

    doc = tomlkit.loads(raw)
    for dotted in unknown:
        parts = dotted.split(".")
        container = doc
        for part in parts[:-1]:
            container = container[part]  # type: ignore[not-subscriptable]
        del container[parts[-1]]  # type: ignore[not-subscriptable]

    target.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return unknown


def update_config(cwd: Path | None = None) -> tuple[Path, list[str], list[str]]:
    """Append missing sections and comment out deprecated keys.

    Reads the current config file, checks which template sections are
    missing, appends them, and comments out any unrecognized keys.

    Raises ``SystemExit(1)`` if the config file doesn't exist.

    Returns:
        Tuple of (config path, list of added section headers, list of deprecated keys commented out).
    """
    target = (cwd or Path.cwd()) / CONFIG_FILENAME
    if not target.exists():
        print(f"Error: {CONFIG_FILENAME} not found in {target.parent}")  # noqa: T201
        print("Hint: use 'codereviewbuddy config --init' to create one")  # noqa: T201
        raise SystemExit(1)

    # Comment out deprecated keys first
    deprecated = _comment_out_unknown_keys(target)
    if deprecated:
        print(f"Commented out {len(deprecated)} deprecated key(s):")  # noqa: T201
        for d in deprecated:
            print(f"  # {d}")  # noqa: T201

    existing = target.read_text(encoding="utf-8")
    added: list[str] = []

    for header, _block in _TEMPLATE_SECTIONS:
        if header not in existing:
            added.append(header)

    if added:
        # Ensure file ends with a newline before appending
        appendix = "" if existing.endswith("\n") else "\n"
        appendix += "\n# --- New sections added by 'codereviewbuddy config --update' ---\n\n"
        for header, block in _TEMPLATE_SECTIONS:
            if header in added:
                appendix += block + "\n"

        target.write_text(existing + appendix, encoding="utf-8")
        print(f"Added {len(added)} section(s):")  # noqa: T201
        for h in added:
            print(f"  + {h}")  # noqa: T201

    if not added and not deprecated:
        print(f"{CONFIG_FILENAME} is up to date — nothing to change")  # noqa: T201

    return target, added, deprecated


def clean_config(cwd: Path | None = None) -> tuple[Path, list[str]]:
    """Remove deprecated keys from an existing ``.codereviewbuddy.toml``.

    Unlike ``update_config`` which comments out deprecated keys, this
    removes them entirely for a tidy config file.

    Raises ``SystemExit(1)`` if the config file doesn't exist.

    Returns:
        Tuple of (config path, list of removed key paths).
    """
    target = (cwd or Path.cwd()) / CONFIG_FILENAME
    if not target.exists():
        print(f"Error: {CONFIG_FILENAME} not found in {target.parent}")  # noqa: T201
        print("Hint: use 'codereviewbuddy config --init' to create one")  # noqa: T201
        raise SystemExit(1)

    removed = _remove_unknown_keys(target)
    if removed:
        print(f"Removed {len(removed)} deprecated key(s) from {target}:")  # noqa: T201
        for r in removed:
            print(f"  - {r}")  # noqa: T201
    else:
        print(f"{CONFIG_FILENAME} is clean — no deprecated keys found")  # noqa: T201

    return target, removed
