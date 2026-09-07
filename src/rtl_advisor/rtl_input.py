from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import shlex
from typing import Any, Iterable

from rtl_advisor.config import ProjectConfig
from rtl_advisor.graph import (
    GraphBuild,
    GraphError,
    _json_hash,
    _normalize_edges,
    _normalize_nodes,
    _normalize_ports,
)
from rtl_advisor.tools import ToolExecutionError, first_output_line, run_command


DESIGN_INPUT_SCHEMA_VERSION = 2
LIVE_GRAPH_SCHEMA_VERSION = 2
LIVE_GRAPH_FLOW_VERSION = "yosys-premap-live-v2"
# Keep command-line defines inside a deliberately small, whitespace-free token
# language.  These values are later passed to Verilator as argv elements and to
# Yosys scripts; accepting arbitrary text after ``=`` would allow a manifest or
# filelist to inject additional Yosys commands.
_DEFINE_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*"
    r"(?:=[A-Za-z0-9_$'.()+*/%?:<>=!&|~^,\[\]{}-]+)?$"
)


class RTLInputError(ValueError):
    """Raised when a live RTL input contract is invalid."""


@dataclass(frozen=True)
class SourceFileV2:
    path: str
    sha256: str


@dataclass(frozen=True)
class DesignInputV2:
    schema_version: int
    top: str
    files: tuple[SourceFileV2, ...]
    include_dirs: tuple[str, ...]
    defines: tuple[str, ...]
    filelists: tuple[str, ...]
    design_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SlangLintResult:
    status: str
    version: str | None
    diagnostics: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ok"] = self.ok
        return payload


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_file(path: str | Path, *, base: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise RTLInputError(f"RTL source file not found: {candidate}")
    return candidate


def _canonical_dir(path: str | Path, *, base: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    candidate = candidate.resolve()
    if not candidate.is_dir():
        raise RTLInputError(f"include directory not found: {candidate}")
    return candidate


def _append_unique(values: list[Any], value: Any) -> None:
    if value not in values:
        values.append(value)


def _filelist_tokens(path: Path) -> list[str]:
    tokens: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            tokens.extend(shlex.split(line, comments=True, posix=True))
    except (OSError, ValueError) as exc:
        raise RTLInputError(f"could not parse filelist {path}: {exc}") from exc
    return tokens


def _parse_filelist(
    path: Path,
    *,
    files: list[Path],
    include_dirs: list[Path],
    defines: list[str],
    filelists: list[Path],
    active: frozenset[Path],
) -> None:
    path = path.resolve()
    if path in active:
        chain = " -> ".join(str(item) for item in (*active, path))
        raise RTLInputError(f"recursive filelist include: {chain}")
    if not path.is_file():
        raise RTLInputError(f"filelist not found: {path}")
    _append_unique(filelists, path)
    tokens = _filelist_tokens(path)
    base = path.parent
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"-f", "-F"}:
            index += 1
            if index >= len(tokens):
                raise RTLInputError(f"{token} requires a path in {path}")
            nested = _canonical_file(tokens[index], base=base)
            _parse_filelist(
                nested,
                files=files,
                include_dirs=include_dirs,
                defines=defines,
                filelists=filelists,
                active=active | {path},
            )
        elif token.startswith("-f") and token != "-f":
            nested = _canonical_file(token[2:], base=base)
            _parse_filelist(
                nested,
                files=files,
                include_dirs=include_dirs,
                defines=defines,
                filelists=filelists,
                active=active | {path},
            )
        elif token == "-I":
            index += 1
            if index >= len(tokens):
                raise RTLInputError(f"-I requires a path in {path}")
            _append_unique(include_dirs, _canonical_dir(tokens[index], base=base))
        elif token.startswith("-I"):
            _append_unique(include_dirs, _canonical_dir(token[2:], base=base))
        elif token.startswith("+incdir+"):
            raw_dirs = [item for item in token[len("+incdir+"):].split("+") if item]
            if not raw_dirs:
                raise RTLInputError(f"empty +incdir+ argument in {path}")
            for raw_dir in raw_dirs:
                _append_unique(include_dirs, _canonical_dir(raw_dir, base=base))
        elif token == "-D":
            index += 1
            if index >= len(tokens):
                raise RTLInputError(f"-D requires a definition in {path}")
            _append_unique(defines, _validate_define(tokens[index]))
        elif token.startswith("-D"):
            _append_unique(defines, _validate_define(token[2:]))
        elif token.startswith("+define+"):
            raw_defines = [item for item in token[len("+define+"):].split("+") if item]
            if not raw_defines:
                raise RTLInputError(f"empty +define+ argument in {path}")
            for raw_define in raw_defines:
                _append_unique(defines, _validate_define(raw_define))
        elif token.startswith(("-", "+")):
            raise RTLInputError(f"unsupported filelist option {token!r} in {path}")
        else:
            _append_unique(files, _canonical_file(token, base=base))
        index += 1


def _validate_define(value: str) -> str:
    if not _DEFINE_PATTERN.fullmatch(value):
        raise RTLInputError(f"invalid preprocessor definition: {value!r}")
    return value


def normalize_design_input(
    *,
    top: str,
    files: Iterable[str | Path] = (),
    filelist: str | Path | None = None,
    include_dirs: Iterable[str | Path] = (),
    defines: Iterable[str] = (),
    base: str | Path | None = None,
) -> DesignInputV2:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", top):
        raise RTLInputError(f"invalid top module name: {top!r}")
    root = Path(base or Path.cwd()).expanduser().resolve()
    resolved_files: list[Path] = []
    resolved_includes: list[Path] = []
    resolved_defines: list[str] = []
    resolved_filelists: list[Path] = []

    for source in files:
        _append_unique(resolved_files, _canonical_file(source, base=root))
    for include_dir in include_dirs:
        _append_unique(resolved_includes, _canonical_dir(include_dir, base=root))
    for define in defines:
        _append_unique(resolved_defines, _validate_define(define))
    if filelist is not None:
        list_path = _canonical_file(filelist, base=root)
        _parse_filelist(
            list_path,
            files=resolved_files,
            include_dirs=resolved_includes,
            defines=resolved_defines,
            filelists=resolved_filelists,
            active=frozenset(),
        )
    if not resolved_files:
        raise RTLInputError("at least one RTL source file is required")

    source_specs = tuple(
        SourceFileV2(path=str(path), sha256=_sha256_file(path))
        for path in resolved_files
    )
    core = {
        "schema_version": DESIGN_INPUT_SCHEMA_VERSION,
        "top": top,
        "files": [asdict(source) for source in source_specs],
        "include_dirs": [str(path) for path in resolved_includes],
        "defines": resolved_defines,
        "filelists": [str(path) for path in resolved_filelists],
    }
    design_hash = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return DesignInputV2(
        schema_version=DESIGN_INPUT_SCHEMA_VERSION,
        top=top,
        files=source_specs,
        include_dirs=tuple(core["include_dirs"]),
        defines=tuple(resolved_defines),
        filelists=tuple(core["filelists"]),
        design_hash=design_hash,
    )


def lint_with_pyslang(design: DesignInputV2) -> SlangLintResult:
    try:
        import pyslang  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RTLInputError(
            "PySlang is required for analyze-rtl; install rtl-advisor[sv]"
        ) from exc

    diagnostics: list[str] = []
    error_count = 0
    try:
        trees = [pyslang.SyntaxTree.fromFile(source.path) for source in design.files]
        compilation = pyslang.Compilation()
        for tree in trees:
            compilation.addSyntaxTree(tree)
        for diagnostic in compilation.getAllDiagnostics():
            is_error = bool(diagnostic.isError())
            error_count += int(is_error)
            diagnostics.append(
                f"{'error' if is_error else 'warning'}: "
                f"{diagnostic.code} at {diagnostic.location}"
            )
    except Exception as exc:  # PySlang raises native binding exception types.
        return SlangLintResult(
            status="failed",
            version=getattr(pyslang, "__version__", None),
            diagnostics=(str(exc),),
        )
    return SlangLintResult(
        status="failed" if error_count else "passed",
        version=getattr(pyslang, "__version__", None),
        diagnostics=tuple(diagnostics),
    )


def _yosys_quote(value: str | Path) -> str:
    raw = str(value)
    if any(character in raw for character in ("\x00", "\r", "\n")):
        raise RTLInputError("Yosys arguments may not contain control characters")
    return '"' + raw.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _normalize_live_graph(
    raw: dict[str, Any],
    *,
    design: DesignInputV2,
    cache_key: str,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    raw_modules = raw.get("modules")
    if not isinstance(raw_modules, dict) or not raw_modules:
        raise GraphError("Yosys JSON contains no modules")
    module_names = set(raw_modules)
    modules = []
    hierarchy_instances = []
    for module_name, raw_module in sorted(raw_modules.items()):
        attributes = raw_module.get("attributes", {})
        ports = _normalize_ports(module_name, raw_module.get("ports", {}))
        nodes = _normalize_nodes(module_name, raw_module.get("cells", {}), module_names)
        edges = _normalize_edges(module_name, ports, nodes)
        instances = [node for node in nodes if node["kind"] == "instance"]
        for instance in instances:
            hierarchy_instances.append(
                {
                    "parent_module": module_name,
                    "instance_id": instance["id"],
                    "instance_name": instance["name"],
                    "child_module": instance["type"],
                    "source": instance["source"],
                    "port_connections": instance["ports"],
                }
            )
        core = {
            "name": module_name,
            "display_name": attributes.get("hdlname", module_name),
            "is_top": module_name == design.top,
            "source": attributes.get("src"),
            "ports": ports,
            "nodes": nodes,
            "edges": edges,
            "instances": [instance["id"] for instance in instances],
        }
        core["module_hash"] = _json_hash(core)
        modules.append(core)
    core_graph = {
        "schema_version": LIVE_GRAPH_SCHEMA_VERSION,
        "flow_version": LIVE_GRAPH_FLOW_VERSION,
        "case_id": f"live_{design.design_hash[:16]}",
        "variant_id": "live",
        "source_sha256": design.design_hash,
        "top": design.top,
        "modules": modules,
        "hierarchy": {
            "top": design.top,
            "instances": sorted(
                hierarchy_instances,
                key=lambda item: (
                    item["parent_module"], item["instance_name"], item["child_module"]
                ),
            ),
        },
    }
    return {
        **core_graph,
        "graph_hash": _json_hash(core_graph),
        "cache_key": cache_key,
        "provenance": provenance,
    }


def build_live_graph(
    config: ProjectConfig,
    design: DesignInputV2,
    *,
    force: bool = False,
) -> GraphBuild:
    try:
        version_result = run_command(
            (config.tools.yosys, "-V"),
            timeout_seconds=config.tools.timeout_seconds,
        )
    except ToolExecutionError as exc:
        raise GraphError(str(exc)) from exc
    if version_result.returncode != 0:
        raise GraphError(version_result.stderr or version_result.stdout)
    yosys_version = first_output_line(version_result) or "unknown"
    cache_key = _json_hash(
        {
            "flow": LIVE_GRAPH_FLOW_VERSION,
            "design_hash": design.design_hash,
            "yosys_version": yosys_version,
        }
    )
    output_dir = config.artifacts_dir / "designs" / design.design_hash / "graph"
    graph_path = output_dir / "graph.json"
    raw_path = output_dir / "yosys.json"
    script_path = output_dir / "graph.ys"
    log_path = output_dir / "graph.log"
    if graph_path.is_file() and raw_path.is_file() and not force:
        try:
            cached = json.loads(graph_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = None
        if isinstance(cached, dict) and cached.get("cache_key") == cache_key:
            return GraphBuild(graph=cached, cached=True, graph_path=graph_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    read_parts = ["read_verilog", "-sv"]
    read_parts.extend(f"-I{_yosys_quote(path)}" for path in design.include_dirs)
    read_parts.extend(f"-D{_yosys_quote(definition)}" for definition in design.defines)
    read_parts.extend(_yosys_quote(source.path) for source in design.files)
    script = "\n".join(
        (
            " ".join(read_parts),
            f"hierarchy -check -top {design.top}",
            "proc",
            "opt_clean",
            f"write_json {_yosys_quote(raw_path)}",
            "",
        )
    )
    script_path.write_text(script, encoding="utf-8")
    command = (config.tools.yosys, "-Q", "-s", str(script_path))
    try:
        completed = run_command(
            command,
            timeout_seconds=config.tools.timeout_seconds,
            cwd=config.root,
        )
    except ToolExecutionError as exc:
        log_path.write_text(f"{exc}\n", encoding="utf-8")
        raise GraphError(str(exc)) from exc
    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    log_path.write_text(combined + ("\n" if combined else ""), encoding="utf-8")
    if completed.returncode != 0:
        raise GraphError(f"Yosys live graph extraction failed; see {log_path}")
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GraphError(f"could not parse Yosys graph {raw_path}: {exc}") from exc
    provenance = {
        "yosys_version": yosys_version,
        "command": list(command),
        "script_path": str(script_path),
        "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
        "log_path": str(log_path),
        "raw_yosys_json": str(raw_path),
    }
    graph = _normalize_live_graph(
        raw,
        design=design,
        cache_key=cache_key,
        provenance=provenance,
    )
    graph_path.write_text(
        json.dumps(graph, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return GraphBuild(graph=graph, cached=False, graph_path=graph_path)
