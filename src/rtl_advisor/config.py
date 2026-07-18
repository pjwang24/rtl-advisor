from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tomllib


DEFAULT_CONFIG_NAME = "rtl-advisor.toml"


class ConfigError(ValueError):
    """Raised when project configuration is invalid."""


@dataclass(frozen=True)
class ToolConfig:
    verilator: str
    yosys: str
    codex: str
    timeout_seconds: int


@dataclass(frozen=True)
class SynthesisConfig:
    driving_cell: str
    output_load_ff: float


@dataclass(frozen=True)
class CodexConfig:
    model: str = "gpt-5.6-sol"
    default_effort: str = "xhigh"
    timeout_seconds: int = 600


@dataclass(frozen=True)
class LibertyConfig:
    name: str
    path: Path
    url: str
    sha256: str
    license_path: Path
    license_url: str
    source_commit: str


@dataclass(frozen=True)
class ProjectConfig:
    config_path: Path
    root: Path
    artifacts_dir: Path
    corpus_dir: Path
    tools: ToolConfig
    synthesis: SynthesisConfig
    liberty: LibertyConfig
    codex: CodexConfig = field(default_factory=CodexConfig)


def _required(mapping: dict[str, object], key: str, section: str) -> object:
    if key not in mapping:
        raise ConfigError(f"missing [{section}].{key}")
    return mapping[key]


def _table(data: dict[str, object], key: str) -> dict[str, object]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"missing or invalid [{key}] table")
    return value


def load_config(path: str | Path = DEFAULT_CONFIG_NAME) -> ProjectConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"configuration file not found: {config_path}")

    try:
        with config_path.open("rb") as stream:
            data = tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    root = config_path.parent
    project = _table(data, "project")
    tools = _table(data, "tools")
    synthesis = _table(data, "synthesis")
    liberty = _table(data, "liberty")
    codex = data.get("codex", {})
    if not isinstance(codex, dict):
        raise ConfigError("invalid [codex] table")

    timeout = int(_required(tools, "timeout_seconds", "tools"))
    if timeout <= 0:
        raise ConfigError("[tools].timeout_seconds must be positive")

    output_load_ff = float(
        _required(synthesis, "output_load_ff", "synthesis")
    )
    if output_load_ff <= 0:
        raise ConfigError("[synthesis].output_load_ff must be positive")

    codex_timeout = int(codex.get("timeout_seconds", 600))
    if codex_timeout <= 0:
        raise ConfigError("[codex].timeout_seconds must be positive")
    codex_effort = str(codex.get("default_effort", "xhigh"))
    if codex_effort not in {"xhigh", "ultra"}:
        raise ConfigError("[codex].default_effort must be 'xhigh' or 'ultra'")

    sha256 = str(_required(liberty, "sha256", "liberty")).lower()
    if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
        raise ConfigError("[liberty].sha256 must be a 64-character hexadecimal digest")

    return ProjectConfig(
        config_path=config_path,
        root=root,
        artifacts_dir=root / str(_required(project, "artifacts_dir", "project")),
        corpus_dir=root / str(_required(project, "corpus_dir", "project")),
        tools=ToolConfig(
            verilator=str(_required(tools, "verilator", "tools")),
            yosys=str(_required(tools, "yosys", "tools")),
            codex=str(_required(tools, "codex", "tools")),
            timeout_seconds=timeout,
        ),
        synthesis=SynthesisConfig(
            driving_cell=str(
                _required(synthesis, "driving_cell", "synthesis")
            ),
            output_load_ff=output_load_ff,
        ),
        liberty=LibertyConfig(
            name=str(_required(liberty, "name", "liberty")),
            path=root / str(_required(liberty, "path", "liberty")),
            url=str(_required(liberty, "url", "liberty")),
            sha256=sha256,
            license_path=root / str(_required(liberty, "license_path", "liberty")),
            license_url=str(_required(liberty, "license_url", "liberty")),
            source_commit=str(_required(liberty, "source_commit", "liberty")),
        ),
        codex=CodexConfig(
            model=str(codex.get("model", "gpt-5.6-sol")),
            default_effort=codex_effort,
            timeout_seconds=codex_timeout,
        ),
    )
