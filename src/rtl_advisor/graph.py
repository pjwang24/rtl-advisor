from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus import CaseManifest, VariantSpec, load_manifest
from rtl_advisor.tools import ToolExecutionError, first_output_line, run_command


GRAPH_SCHEMA_VERSION = 1
GRAPH_FLOW_VERSION = "yosys-hierarchical-graph-v1"
_SOURCE_PATTERN = re.compile(
    r"^(?P<file>.*):(?P<start_line>\d+)\.(?P<start_col>\d+)-"
    r"(?P<end_line>\d+)\.(?P<end_col>\d+)$"
)


class GraphError(RuntimeError):
    """Raised when a hierarchy-preserving RTL graph cannot be created."""


@dataclass(frozen=True)
class GraphBuild:
    graph: dict[str, Any]
    cached: bool
    graph_path: Path


def _json_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _yosys_quote(value: str | Path) -> str:
    raw = str(value)
    if any(character in raw for character in ("\x00", "\r", "\n")):
        raise GraphError("Yosys arguments may not contain control characters")
    return '"' + raw.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _source(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    locations = []
    for part in raw.split("|"):
        match = _SOURCE_PATTERN.match(part)
        if match:
            location: dict[str, Any] = {"file": match.group("file")}
            for field in ("start_line", "start_col", "end_line", "end_col"):
                location[field] = int(match.group(field))
            locations.append(location)
        else:
            locations.append({"raw": part})
    return {"raw": raw, "locations": locations}


def _parameter(value: Any) -> Any:
    if isinstance(value, str) and value and set(value) <= {"0", "1"}:
        return int(value, 2)
    return value


def _bit_key(bit: int | str) -> str:
    return f"n:{bit}" if isinstance(bit, int) else f"c:{bit}"


def _operation(cell_type: str) -> str:
    aliases = {
        "$add": "add",
        "$sub": "subtract",
        "$mul": "multiply",
        "$mux": "mux",
        "$pmux": "priority_mux",
        "$dff": "dff",
        "$sdff": "sync_reset_dff",
        "$adff": "async_reset_dff",
    }
    return aliases.get(cell_type, cell_type.lstrip("$"))


def _cell_kind(cell_type: str, module_names: set[str]) -> str:
    if cell_type in module_names:
        return "instance"
    if cell_type in {"$dff", "$sdff", "$adff", "$dffe", "$sdffe"}:
        return "register"
    if cell_type.startswith("$"):
        return "operator"
    return "primitive"


def _normalize_ports(module_name: str, raw_ports: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": f"{module_name}::port:{name}",
            "name": name,
            "direction": raw["direction"],
            "width": len(raw.get("bits", [])),
            "bits": raw.get("bits", []),
        }
        for name, raw in sorted(raw_ports.items())
    ]


def _normalize_nodes(
    module_name: str,
    raw_cells: dict[str, Any],
    module_names: set[str],
) -> list[dict[str, Any]]:
    nodes = []
    for name, raw in sorted(raw_cells.items()):
        cell_type = raw["type"]
        port_directions = raw.get("port_directions", {})
        connections = raw.get("connections", {})
        ports = {
            port_name: {
                "direction": port_directions.get(port_name, "unknown"),
                "width": len(bits),
                "bits": bits,
            }
            for port_name, bits in sorted(connections.items())
        }
        nodes.append(
            {
                "id": f"{module_name}::cell:{name}",
                "name": name,
                "kind": _cell_kind(cell_type, module_names),
                "type": cell_type,
                "operation": _operation(cell_type),
                "parameters": {
                    key: _parameter(value)
                    for key, value in sorted(raw.get("parameters", {}).items())
                },
                "ports": ports,
                "source": _source(raw.get("attributes", {}).get("src")),
            }
        )
    return nodes


def _normalize_edges(
    module_name: str,
    ports: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    drivers: dict[str, tuple[str, str]] = {}
    for port in ports:
        if port["direction"] == "input":
            for bit in port["bits"]:
                if isinstance(bit, int):
                    drivers[_bit_key(bit)] = (port["id"], port["name"])
    for node in nodes:
        for port_name, port in node["ports"].items():
            if port["direction"] == "output":
                for bit in port["bits"]:
                    if isinstance(bit, int):
                        drivers[_bit_key(bit)] = (node["id"], port_name)

    grouped: dict[tuple[str, str, str, str], list[int | str]] = {}

    def add_consumers(destination: str, destination_port: str, bits: list[Any]) -> None:
        for bit in bits:
            source = drivers.get(_bit_key(bit))
            if source is None:
                continue
            key = (source[0], source[1], destination, destination_port)
            grouped.setdefault(key, []).append(bit)

    for node in nodes:
        for port_name, port in node["ports"].items():
            if port["direction"] == "input":
                add_consumers(node["id"], port_name, port["bits"])
    for port in ports:
        if port["direction"] == "output":
            add_consumers(port["id"], port["name"], port["bits"])

    return [
        {
            "id": _json_hash(
                {
                    "source": source,
                    "source_port": source_port,
                    "destination": destination,
                    "destination_port": destination_port,
                    "bits": bits,
                }
            )[:16],
            "source": source,
            "source_port": source_port,
            "destination": destination,
            "destination_port": destination_port,
            "width": len(bits),
            "bits": bits,
        }
        for (source, source_port, destination, destination_port), bits in sorted(
            grouped.items()
        )
    ]


def _normalize_yosys_graph(
    raw: dict[str, Any],
    *,
    manifest: CaseManifest,
    variant: VariantSpec,
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
        nodes = _normalize_nodes(
            module_name,
            raw_module.get("cells", {}),
            module_names,
        )
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
            "is_top": module_name == variant.wrapper_top,
            "source": _source(attributes.get("src")),
            "ports": ports,
            "nodes": nodes,
            "edges": edges,
            "instances": [instance["id"] for instance in instances],
        }
        core["module_hash"] = _json_hash(core)
        modules.append(core)

    hierarchy_instances.sort(
        key=lambda item: (
            item["parent_module"],
            item["instance_name"],
            item["child_module"],
        )
    )
    core_graph = {
        "schema_version": GRAPH_SCHEMA_VERSION,
        "flow_version": GRAPH_FLOW_VERSION,
        "case_id": manifest.case_id,
        "variant_id": variant.variant_id,
        "source_sha256": variant.sha256,
        "top": variant.wrapper_top,
        "modules": modules,
        "hierarchy": {
            "top": variant.wrapper_top,
            "instances": hierarchy_instances,
        },
    }
    graph_hash = _json_hash(core_graph)
    return {
        **core_graph,
        "graph_hash": graph_hash,
        "cache_key": cache_key,
        "provenance": provenance,
    }


def _cache_key(variant: VariantSpec, yosys_version: str) -> str:
    return _json_hash(
        {
            "flow_version": GRAPH_FLOW_VERSION,
            "schema_version": GRAPH_SCHEMA_VERSION,
            "source_sha256": variant.sha256,
            "top": variant.wrapper_top,
            "yosys_version": yosys_version,
        }
    )


def build_graph(
    config: ProjectConfig,
    case: str | Path | CaseManifest,
    variant_id: str,
    *,
    force: bool = False,
) -> GraphBuild:
    manifest = case if isinstance(case, CaseManifest) else load_manifest(case)
    variant = manifest.variant(variant_id)
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
    cache_key = _cache_key(variant, yosys_version)

    output_dir = (
        config.artifacts_dir
        / "cases"
        / manifest.case_id
        / "graph"
        / variant.variant_id
    )
    graph_path = output_dir / "graph.json"
    raw_path = output_dir / "yosys.json"
    script_path = output_dir / "graph.ys"
    log_path = output_dir / "graph.log"
    if graph_path.is_file() and raw_path.is_file() and not force:
        try:
            cached_graph = json.loads(graph_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached_graph = None
        if cached_graph is not None and cached_graph.get("cache_key") == cache_key:
            return GraphBuild(graph=cached_graph, cached=True, graph_path=graph_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    relative_source = Path(variant.file)
    script = "\n".join(
        (
            f"read_verilog -sv {_yosys_quote(relative_source)}",
            f"hierarchy -check -top {variant.wrapper_top}",
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
            cwd=manifest.root,
        )
    except ToolExecutionError as exc:
        log_path.write_text(f"{exc}\n", encoding="utf-8")
        raise GraphError(str(exc)) from exc
    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    log_path.write_text(combined + ("\n" if combined else ""), encoding="utf-8")
    if completed.returncode != 0:
        raise GraphError(f"Yosys graph extraction failed; see {log_path}")
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
    graph = _normalize_yosys_graph(
        raw,
        manifest=manifest,
        variant=variant,
        cache_key=cache_key,
        provenance=provenance,
    )
    graph_path.write_text(
        json.dumps(graph, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return GraphBuild(graph=graph, cached=False, graph_path=graph_path)
