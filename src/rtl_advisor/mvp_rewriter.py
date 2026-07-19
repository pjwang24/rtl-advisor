from __future__ import annotations

from dataclasses import dataclass
import difflib
import importlib.util
from pathlib import Path
import re
import shutil
from typing import Any, Mapping, Sequence

from rtl_advisor.config import ProjectConfig
from rtl_advisor.mvp_schema import (
    RUN_SCHEMA_ID,
    RUN_SCHEMA_VERSION,
    TRANSFORMATION_ID,
    TRANSFORMATION_VERSION,
    MVPSchemaError,
    compile_context_snapshot,
    compile_contexts_compatible,
    file_sha256,
    read_hashed_json,
    stable_hash,
    write_hashed_json,
)
from rtl_advisor.rtl_input import (
    DesignInputV2,
    RTLInputError,
    SourceFileV2,
    lint_with_pyslang,
)
from rtl_advisor.tools import ToolExecutionError, first_output_line, run_command


CANDIDATE_DOCUMENT_TYPE = "rtl-advisor.candidate"
FORMAL_DOCUMENT_TYPE = "rtl-advisor.formal-result"
FORMAL_BACKEND = "yosys-combinational-equiv-v1"
FORMAL_SEMANTICS = (
    "Yosys two-state bit-vector RTL equivalence for a self-contained "
    "combinational top; X/Z behavior is not proved"
)


class MVPRewriteError(RuntimeError):
    """Raised when a narrow MVP rewrite cannot be prepared or trusted."""

    def __init__(self, message: str, *, code: str = "mvp_rewrite_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Token:
    text: str
    start: int
    end: int
    kind: str


@dataclass(frozen=True)
class _Signal:
    name: str
    width: int
    signed: bool
    direction: str | None


@dataclass(frozen=True)
class _Expr:
    name: str | None = None
    left: "_Expr | None" = None
    right: "_Expr | None" = None

    @property
    def is_name(self) -> bool:
        return self.name is not None


@dataclass(frozen=True)
class _Assignment:
    target: str | None
    expression: _Expr | None
    expression_tokens: tuple[_Token, ...]


_TOKEN_RE = re.compile(
    r"(?P<identifier>[A-Za-z_][A-Za-z0-9_$]*)"
    r"|(?P<number>(?:[0-9][0-9_]*)?'[sS]?[bBoOdDhH][0-9a-fA-F_xXzZ?]+|[0-9][0-9_]*)"
    r"|(?P<symbol>.)",
    re.DOTALL,
)
_PROHIBITED_MODULE_WORDS = {
    "always",
    "always_comb",
    "always_ff",
    "always_latch",
    "initial",
    "final",
    "generate",
    "endgenerate",
    "function",
    "endfunction",
    "task",
    "endtask",
    "class",
    "interface",
    "clocking",
    "specify",
    "property",
    "sequence",
}
_DECLARATION_TYPES = {"wire", "logic", "bit"}
_FORMAL_SUCCESS_MARKER = "Equivalence successfully proven!"


def _read_utf8_exact(path: Path) -> str:
    """Decode UTF-8 without universal-newline translation."""

    return path.read_bytes().decode("utf-8")


def _mask_comments_and_strings(text: str) -> str:
    """Mask non-code characters without changing source offsets or newlines."""

    chars = list(text)
    index = 0
    state = "code"
    while index < len(chars):
        current = chars[index]
        following = chars[index + 1] if index + 1 < len(chars) else ""
        if state == "code":
            if current == "/" and following == "/":
                chars[index] = chars[index + 1] = " "
                index += 2
                state = "line_comment"
                continue
            if current == "/" and following == "*":
                chars[index] = chars[index + 1] = " "
                index += 2
                state = "block_comment"
                continue
            if current == '"':
                chars[index] = " "
                index += 1
                state = "string"
                continue
        elif state == "line_comment":
            if current == "\n":
                state = "code"
            else:
                chars[index] = " "
        elif state == "block_comment":
            if current == "*" and following == "/":
                chars[index] = chars[index + 1] = " "
                index += 2
                state = "code"
                continue
            if current != "\n":
                chars[index] = " "
        elif state == "string":
            if current == "\\" and following:
                chars[index] = " "
                if following != "\n":
                    chars[index + 1] = " "
                index += 2
                continue
            if current == '"':
                state = "code"
            if current != "\n":
                chars[index] = " "
        index += 1
    return "".join(chars)


def _tokens(masked: str, *, start: int = 0, end: int | None = None) -> list[_Token]:
    result: list[_Token] = []
    limit = len(masked) if end is None else end
    for match in _TOKEN_RE.finditer(masked, start, limit):
        text = match.group()
        if text.isspace():
            continue
        result.append(
            _Token(
                text=text,
                start=match.start(),
                end=match.end(),
                kind=str(match.lastgroup),
            )
        )
    return result


def _design_core(design: DesignInputV2) -> dict[str, Any]:
    return {
        "schema_version": design.schema_version,
        "top": design.top,
        "files": [
            {"path": source.path, "sha256": source.sha256}
            for source in design.files
        ],
        "include_dirs": list(design.include_dirs),
        "defines": list(design.defines),
        "filelists": list(design.filelists),
    }


def _computed_design_hash(design: DesignInputV2) -> str:
    return stable_hash(_design_core(design))


def _design_integrity(
    design: DesignInputV2,
    expected_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    mismatches: list[dict[str, str | None]] = []
    for source in design.files:
        path = Path(source.path)
        actual = file_sha256(path) if path.is_file() else None
        if actual != source.sha256:
            mismatches.append(
                {
                    "path": source.path,
                    "expected_sha256": source.sha256,
                    "actual_sha256": actual,
                }
            )
    computed_hash = _computed_design_hash(design)
    if computed_hash != design.design_hash:
        mismatches.append(
            {
                "path": "<compile-context>",
                "expected_sha256": design.design_hash,
                "actual_sha256": computed_hash,
            }
        )
    try:
        actual_context = compile_context_snapshot(design)
    except MVPSchemaError as exc:
        mismatches.append(
            {
                "path": "<compile-context>",
                "expected_sha256": (
                    str(expected_context.get("compile_context_hash"))
                    if expected_context is not None
                    else None
                ),
                "actual_sha256": None,
            }
        )
        return {
            "ok": False,
            "mismatches": mismatches,
            "compile_context": None,
            "detail": str(exc),
        }
    if expected_context is not None and actual_context != dict(expected_context):
        mismatches.append(
            {
                "path": "<compile-context>",
                "expected_sha256": str(expected_context.get("compile_context_hash")),
                "actual_sha256": str(actual_context.get("compile_context_hash")),
            }
        )
    return {
        "ok": not mismatches,
        "mismatches": mismatches,
        "compile_context": actual_context,
    }


def _require_current_design(design: DesignInputV2) -> None:
    integrity = _design_integrity(design)
    if not integrity["ok"]:
        raise MVPRewriteError(
            "design input is stale; normalize and review the current sources again",
            code="stale_source",
        )


def _matching_close(tokens: Sequence[_Token], opening: int, left: str, right: str) -> int:
    depth = 0
    for index in range(opening, len(tokens)):
        if tokens[index].text == left:
            depth += 1
        elif tokens[index].text == right:
            depth -= 1
            if depth == 0:
                return index
    raise MVPRewriteError(f"unmatched {left!r} in top module", code="unsupported_rtl")


def _split_at_top_level(tokens: Sequence[_Token], separator: str) -> list[list[_Token]]:
    groups: list[list[_Token]] = []
    current: list[_Token] = []
    depths = {"(": 0, "[": 0, "{": 0}
    closing = {")": "(", "]": "[", "}": "{"}
    for token in tokens:
        if token.text in depths:
            depths[token.text] += 1
        elif token.text in closing:
            depths[closing[token.text]] -= 1
            if depths[closing[token.text]] < 0:
                raise MVPRewriteError("unbalanced expression", code="unsupported_rtl")
        if token.text == separator and all(depth == 0 for depth in depths.values()):
            groups.append(current)
            current = []
        else:
            current.append(token)
    if any(depth != 0 for depth in depths.values()):
        raise MVPRewriteError("unbalanced expression", code="unsupported_rtl")
    groups.append(current)
    return groups


def _fixed_width(tokens: Sequence[_Token], index: int) -> tuple[int, int]:
    if index >= len(tokens) or tokens[index].text != "[":
        return 1, index
    if index + 4 >= len(tokens):
        raise MVPRewriteError("incomplete packed width", code="unsupported_rtl")
    expected = ("number", ":", "number", "]")
    actual = (
        tokens[index + 1].kind,
        tokens[index + 2].text,
        tokens[index + 3].kind,
        tokens[index + 4].text,
    )
    if actual != expected:
        raise MVPRewriteError(
            "only fixed numeric one-dimensional packed widths are supported",
            code="unsupported_rtl",
        )
    left_text = tokens[index + 1].text.replace("_", "")
    right_text = tokens[index + 3].text.replace("_", "")
    if "'" in left_text or "'" in right_text:
        raise MVPRewriteError(
            "sized values are not supported in packed ranges",
            code="unsupported_rtl",
        )
    width = abs(int(left_text, 10) - int(right_text, 10)) + 1
    return width, index + 5


def _parse_decl_segment(
    segment: Sequence[_Token],
    sticky: tuple[str | None, int, bool] | None,
) -> tuple[_Signal, tuple[str | None, int, bool]]:
    if not segment:
        raise MVPRewriteError("empty declaration segment", code="unsupported_rtl")
    index = 0
    direction: str | None
    width: int
    signed: bool
    if segment[index].text in {"input", "output", "inout"}:
        direction = segment[index].text
        if direction == "inout":
            raise MVPRewriteError("inout ports are unsupported", code="unsupported_rtl")
        index += 1
        if index < len(segment) and segment[index].text in {"var", *_DECLARATION_TYPES}:
            index += 1
        signed = False
        if index < len(segment) and segment[index].text in {"signed", "unsigned"}:
            signed = segment[index].text == "signed"
            index += 1
        width, index = _fixed_width(segment, index)
    elif sticky is not None:
        direction, width, signed = sticky
    else:
        direction = None
        if segment[index].text in _DECLARATION_TYPES:
            index += 1
        elif segment[index].text == "reg":
            raise MVPRewriteError("reg declarations are unsupported", code="unsupported_rtl")
        signed = False
        if index < len(segment) and segment[index].text in {"signed", "unsigned"}:
            signed = segment[index].text == "signed"
            index += 1
        width, index = _fixed_width(segment, index)
    if index >= len(segment) or segment[index].kind != "identifier":
        raise MVPRewriteError("unsupported declaration", code="unsupported_rtl")
    name = segment[index].text
    index += 1
    if index != len(segment):
        raise MVPRewriteError(
            f"unsupported declaration syntax for {name}", code="unsupported_rtl"
        )
    state = (direction, width, signed)
    return _Signal(name=name, width=width, signed=signed, direction=direction), state


def _parse_declaration(tokens: Sequence[_Token]) -> list[_Signal]:
    groups = _split_at_top_level(tokens, ",")
    signals: list[_Signal] = []
    sticky: tuple[str | None, int, bool] | None = None
    for group in groups:
        signal, sticky = _parse_decl_segment(group, sticky)
        signals.append(signal)
    return signals


class _ExpressionParser:
    def __init__(self, tokens: Sequence[_Token]) -> None:
        self.tokens = tokens
        self.index = 0

    def parse(self) -> _Expr:
        expression = self._sum()
        if self.index != len(self.tokens):
            raise MVPRewriteError("expression is not a pure addition chain", code="unsupported_site")
        return expression

    def _sum(self) -> _Expr:
        expression = self._primary()
        while self.index < len(self.tokens) and self.tokens[self.index].text == "+":
            self.index += 1
            expression = _Expr(left=expression, right=self._primary())
        return expression

    def _primary(self) -> _Expr:
        if self.index >= len(self.tokens):
            raise MVPRewriteError("incomplete addition expression", code="unsupported_site")
        token = self.tokens[self.index]
        if token.kind == "identifier":
            self.index += 1
            return _Expr(name=token.text)
        if token.text == "(":
            self.index += 1
            expression = self._sum()
            if self.index >= len(self.tokens) or self.tokens[self.index].text != ")":
                raise MVPRewriteError("unbalanced addition expression", code="unsupported_site")
            self.index += 1
            return expression
        raise MVPRewriteError("addition operands must be signal names", code="unsupported_site")


def _flatten(expression: _Expr) -> list[str]:
    if expression.is_name:
        return [str(expression.name)]
    assert expression.left is not None and expression.right is not None
    return [*_flatten(expression.left), *_flatten(expression.right)]


def _balanced_expression(names: Sequence[str]) -> str:
    if len(names) == 1:
        return names[0]
    midpoint = len(names) // 2
    return f"({_balanced_expression(names[:midpoint])} + {_balanced_expression(names[midpoint:])})"


def _balanced_tree(names: Sequence[str]) -> _Expr:
    if len(names) == 1:
        return _Expr(name=names[0])
    midpoint = len(names) // 2
    return _Expr(
        left=_balanced_tree(names[:midpoint]),
        right=_balanced_tree(names[midpoint:]),
    )


def _parse_assign(statement: Sequence[_Token]) -> _Assignment:
    if len(statement) < 4 or statement[0].text != "assign":
        return _Assignment(None, None, ())
    if statement[1].kind != "identifier" or statement[2].text != "=":
        return _Assignment(None, None, ())
    target = statement[1].text
    expression_tokens = tuple(statement[3:])
    try:
        expression = _ExpressionParser(expression_tokens).parse()
    except MVPRewriteError:
        expression = None
    return _Assignment(target, expression, expression_tokens)


def _body_statements(tokens: Sequence[_Token]) -> list[list[_Token]]:
    statements = _split_at_top_level(tokens, ";")
    if statements and not statements[-1]:
        statements.pop()
    return statements


def _module_for_top(
    design: DesignInputV2,
) -> tuple[Path, str, str, list[_Token], int, str]:
    matches: list[tuple[Path, str, str, list[_Token], int, str]] = []
    for source in design.files:
        path = Path(source.path)
        text = _read_utf8_exact(path)
        masked = _mask_comments_and_strings(text)
        tokens = _tokens(masked)
        index = 0
        while index < len(tokens):
            if tokens[index].text != "module":
                index += 1
                continue
            module_index = index
            index += 1
            if index < len(tokens) and tokens[index].text in {"automatic", "static"}:
                index += 1
            if index >= len(tokens) or tokens[index].kind != "identifier":
                raise MVPRewriteError("could not parse module declaration", code="unsupported_rtl")
            module_name = tokens[index].text
            name_index = index
            end_index = next(
                (cursor for cursor in range(index + 1, len(tokens)) if tokens[cursor].text == "endmodule"),
                None,
            )
            if end_index is None:
                raise MVPRewriteError("module has no endmodule", code="unsupported_rtl")
            if module_name == design.top:
                matches.append(
                    (
                        path,
                        text,
                        masked,
                        tokens[module_index : end_index + 1],
                        name_index - module_index,
                        source.sha256,
                    )
                )
            index = end_index + 1
    if len(matches) != 1:
        raise MVPRewriteError(
            f"expected exactly one definition of top {design.top!r}; found {len(matches)}",
            code="ambiguous_top" if matches else "missing_top",
        )
    return matches[0]


def _exclusion(
    *,
    path: Path,
    source_hash: str,
    reason_code: str,
    detail: str,
    tokens: Sequence[_Token] = (),
    target: str | None = None,
) -> dict[str, Any]:
    source: dict[str, Any] = {"file": str(path), "sha256": source_hash}
    if tokens:
        start = tokens[0].start
        end = tokens[-1].end
        source.update({"start_offset": start, "end_offset": end})
    core = {
        "reason_code": reason_code,
        "source_sha256": source_hash,
        "start_offset": source.get("start_offset"),
        "end_offset": source.get("end_offset"),
        "target": target,
    }
    return {
        "exclusion_id": f"addexclude_{stable_hash(core)[:16]}",
        "status": "excluded",
        "transformation_id": TRANSFORMATION_ID,
        "transformation_version": TRANSFORMATION_VERSION,
        "reason_code": reason_code,
        "detail": detail,
        "source": source,
        **({"target": target} if target is not None else {}),
    }


def _parse_top(
    design: DesignInputV2,
) -> tuple[Path, str, str, list[dict[str, Any]], list[dict[str, Any]]]:
    path, text, masked, tokens, name_index, source_hash = _module_for_top(design)

    def reject(code: str, detail: str) -> tuple[
        Path, str, str, list[dict[str, Any]], list[dict[str, Any]]
    ]:
        return path, text, source_hash, [], [
            _exclusion(
                path=path,
                source_hash=source_hash,
                reason_code=code,
                detail=detail,
                tokens=tokens,
            )
        ]

    cursor = name_index + 1
    if cursor < len(tokens) and tokens[cursor].text == "#":
        return reject("parameterized_top", "parameterized top modules are outside MVP scope")
    header_tokens: list[_Token] = []
    if cursor < len(tokens) and tokens[cursor].text == "(":
        close = _matching_close(tokens, cursor, "(", ")")
        header_tokens = list(tokens[cursor + 1 : close])
        cursor = close + 1
    if cursor >= len(tokens) or tokens[cursor].text != ";":
        return reject("unsupported_module_header", "the top module header is ambiguous")
    body_start = cursor + 1
    body_tokens = list(tokens[body_start:-1])
    module_start = tokens[0].start
    module_end = tokens[-1].end
    module_masked = masked[module_start:module_end]
    if "`" in module_masked:
        return reject("preprocessor_source", "macro-expanded source spans are not rewritten")
    if any(token.text in _PROHIBITED_MODULE_WORDS for token in body_tokens):
        return reject(
            "procedural_or_generated_rtl",
            "procedural, generated, function, or task constructs are outside MVP scope",
        )
    if any(token.text in {"#", "@", "->"} for token in body_tokens):
        return reject("timed_or_event_rtl", "timing and event constructs are outside MVP scope")

    signals: dict[str, _Signal] = {}
    header_names: set[str] = set()
    ansi_header = any(token.text in {"input", "output", "inout"} for token in header_tokens)
    try:
        if header_tokens:
            if ansi_header:
                for signal in _parse_declaration(header_tokens):
                    if signal.name in signals:
                        return reject("duplicate_declaration", "a signal is declared more than once")
                    signals[signal.name] = signal
            else:
                for group in _split_at_top_level(header_tokens, ","):
                    if len(group) != 1 or group[0].kind != "identifier":
                        return reject("unsupported_module_header", "non-ANSI port list is ambiguous")
                    header_names.add(group[0].text)
    except MVPRewriteError as exc:
        return reject("unsupported_declaration", str(exc))

    assignments: list[_Assignment] = []
    try:
        for statement in _body_statements(body_tokens):
            if not statement:
                continue
            first = statement[0].text
            if first in {"input", "output", "inout", *_DECLARATION_TYPES}:
                declared = _parse_declaration(statement)
                for signal in declared:
                    if signal.name in signals:
                        return reject("duplicate_declaration", "a signal is declared more than once")
                    if header_names and signal.direction is not None and signal.name not in header_names:
                        return reject("port_declaration_mismatch", "port declarations do not match the header")
                    signals[signal.name] = signal
                continue
            if first == "assign":
                assignment = _parse_assign(statement)
                if assignment.target is None or assignment.expression is None:
                    return path, text, source_hash, [], [
                        _exclusion(
                            path=path,
                            source_hash=source_hash,
                            reason_code="unsupported_assignment_expression",
                            detail=(
                                "a direct assignment could not be parsed as a pure, "
                                "side-effect-free addition expression"
                            ),
                            tokens=statement,
                            target=assignment.target,
                        )
                    ]
                assignments.append(assignment)
                continue
            # An unexpected top-level construct may be an instance, parameter,
            # alias, or procedural syntax. The MVP deliberately fails closed.
            return reject("unsupported_module_statement", "the module contains an unsupported statement")
    except MVPRewriteError as exc:
        return reject("unsupported_declaration", str(exc))

    if header_names:
        declared_ports = {
            name for name, signal in signals.items() if signal.direction is not None
        }
        if header_names != declared_ports:
            return reject("port_declaration_mismatch", "port declarations do not match the header")

    target_counts: dict[str, int] = {}
    for assignment in assignments:
        if assignment.target is not None:
            target_counts[assignment.target] = target_counts.get(assignment.target, 0) + 1

    duplicate_targets = sorted(
        target for target, count in target_counts.items() if count != 1
    )
    if duplicate_targets:
        return path, text, source_hash, [], [
            _exclusion(
                path=path,
                source_hash=source_hash,
                reason_code="multiple_driver",
                detail="the direct-assignment target has multiple drivers",
                tokens=assignment.expression_tokens,
                target=str(assignment.target),
            )
            for assignment in assignments
            if assignment.target in duplicate_targets
        ]

    findings: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    for assignment in assignments:
        assert assignment.target is not None and assignment.expression is not None
        operands = _flatten(assignment.expression)
        if len(operands) < 3:
            exclusions.append(
                _exclusion(
                    path=path,
                    source_hash=source_hash,
                    reason_code="fewer_than_three_addends",
                    detail="the assignment has fewer than three addition operands",
                    tokens=assignment.expression_tokens,
                    target=assignment.target,
                )
            )
            continue
        target = signals.get(assignment.target)
        operand_signals = [signals.get(name) for name in operands]
        if target is None or target.direction != "output" or target.signed:
            exclusions.append(
                _exclusion(
                    path=path,
                    source_hash=source_hash,
                    reason_code="unsupported_target_type",
                    detail="the target must be one unsigned output with an explicit fixed width",
                    tokens=assignment.expression_tokens,
                    target=assignment.target,
                )
            )
            continue
        if any(signal is None for signal in operand_signals):
            exclusions.append(
                _exclusion(
                    path=path,
                    source_hash=source_hash,
                    reason_code="unresolved_operand",
                    detail="one or more operands do not have an unambiguous declaration",
                    tokens=assignment.expression_tokens,
                    target=assignment.target,
                )
            )
            continue
        typed_operands = [signal for signal in operand_signals if signal is not None]
        if any(signal.direction != "input" or signal.signed for signal in typed_operands):
            exclusions.append(
                _exclusion(
                    path=path,
                    source_hash=source_hash,
                    reason_code="unsupported_operand_type",
                    detail="all operands must be unsigned fixed-width inputs",
                    tokens=assignment.expression_tokens,
                    target=assignment.target,
                )
            )
            continue
        if assignment.target in operands:
            exclusions.append(
                _exclusion(
                    path=path,
                    source_hash=source_hash,
                    reason_code="self_referential_assignment",
                    detail="the assignment target also appears as an operand",
                    tokens=assignment.expression_tokens,
                    target=assignment.target,
                )
            )
            continue
        widths = {signal.width for signal in typed_operands}
        if widths != {target.width}:
            exclusions.append(
                _exclusion(
                    path=path,
                    source_hash=source_hash,
                    reason_code="width_or_truncation_risk",
                    detail="operand and target widths are not identical",
                    tokens=assignment.expression_tokens,
                    target=assignment.target,
                )
            )
            continue
        expression_tokens = assignment.expression_tokens
        expression_start = expression_tokens[0].start
        expression_end = expression_tokens[-1].end
        replacement = _balanced_expression(operands)
        original = text[expression_start:expression_end]
        if assignment.expression == _balanced_tree(operands):
            exclusions.append(
                _exclusion(
                    path=path,
                    source_hash=source_hash,
                    reason_code="already_balanced",
                    detail="the addition expression already has the deterministic balanced shape",
                    tokens=assignment.expression_tokens,
                    target=assignment.target,
                )
            )
            continue
        site_core = {
            "transformation_id": TRANSFORMATION_ID,
            "transformation_version": TRANSFORMATION_VERSION,
            "top": design.top,
            "source_sha256": source_hash,
            "target": assignment.target,
            "operands": operands,
            "start_offset": expression_start,
            "end_offset": expression_end,
        }
        finding_id = f"addsite_{stable_hash(site_core)[:16]}"
        prefix = text[:expression_start]
        line = prefix.count("\n") + 1
        column = expression_start - prefix.rfind("\n")
        end_prefix = text[:expression_end]
        end_line = end_prefix.count("\n") + 1
        end_column = expression_end - end_prefix.rfind("\n")
        findings.append(
            {
                "finding_id": finding_id,
                "status": "candidate_available",
                "transformation_id": TRANSFORMATION_ID,
                "transformation_version": TRANSFORMATION_VERSION,
                "top": design.top,
                "source": {
                    "file": str(path),
                    "sha256": source_hash,
                    "line": line,
                    "column": column,
                    "end_line": end_line,
                    "end_column": end_column,
                    "start_offset": expression_start,
                    "end_offset": expression_end,
                },
                "target": {"name": assignment.target, "width": target.width},
                "operands": [
                    {"name": name, "width": target.width, "signed": False}
                    for name in operands
                ],
                "original_expression": original,
                "replacement_expression": replacement,
                "reason": (
                    "unsigned equal-width combinational addition chain can be "
                    "rewritten as a deterministic balanced tree"
                ),
                "limitations": [
                    "candidate is unproven until current Yosys formal verification passes",
                    "only two-state bit-vector equivalence is supported in this MVP",
                ],
            }
        )
    order = lambda item: (
        str(item["source"]["file"]),
        int(item["source"].get("start_offset", -1)),
        str(item.get("reason_code", "")),
    )
    return (
        path,
        text,
        source_hash,
        sorted(findings, key=order),
        sorted(exclusions, key=order),
    )


def scan_addition_sites(design: DesignInputV2) -> list[dict[str, Any]]:
    """Return conservative source-linked sites for the one released MVP rewrite."""

    _require_current_design(design)
    try:
        _, _, _, findings, _ = _parse_top(design)
    except (OSError, UnicodeError) as exc:
        raise MVPRewriteError(f"could not read RTL source: {exc}", code="source_read_error") from exc
    return findings


def scan_addition_analysis(design: DesignInputV2) -> dict[str, list[dict[str, Any]]]:
    """Return deterministic candidate findings and structured exclusions."""

    _require_current_design(design)
    try:
        _, _, _, findings, exclusions = _parse_top(design)
    except (OSError, UnicodeError) as exc:
        raise MVPRewriteError(f"could not read RTL source: {exc}", code="source_read_error") from exc
    return {"findings": findings, "exclusions": exclusions}


def _copy_design(
    design: DesignInputV2,
    destination: Path,
    *,
    replacement_path: Path,
    replacement_text: str,
) -> DesignInputV2:
    copied_includes: list[str] = []
    for index, raw_include in enumerate(design.include_dirs):
        include = Path(raw_include).resolve()
        target = destination / "include_dirs" / f"{index:04d}" / include.name
        if (
            target.resolve() == include
            or destination.is_relative_to(include)
            or include.is_relative_to(destination)
        ):
            raise MVPRewriteError(
                "artifact workspace overlaps an include directory",
                code="unsafe_artifact_path",
            )
        shutil.copytree(include, target, dirs_exist_ok=True)
        copied_includes.append(str(target))

    copied_files: list[SourceFileV2] = []
    for index, source in enumerate(design.files):
        original = Path(source.path).resolve()
        target = destination / "sources" / f"{index:04d}" / original.name
        if target.resolve() == original:
            raise MVPRewriteError(
                "candidate path would overwrite an original source",
                code="unsafe_artifact_path",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)
        if original == replacement_path:
            target.write_bytes(replacement_text.encode("utf-8"))
        copied_files.append(SourceFileV2(path=str(target), sha256=file_sha256(target)))

    copied_filelists: list[str] = []
    for index, raw_filelist in enumerate(design.filelists):
        original = Path(raw_filelist).resolve()
        target = destination / "filelists" / f"{index:04d}" / original.name
        if target.resolve() == original:
            raise MVPRewriteError(
                "candidate path would overwrite an original filelist",
                code="unsafe_artifact_path",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)
        copied_filelists.append(str(target))

    candidate = DesignInputV2(
        schema_version=design.schema_version,
        top=design.top,
        files=tuple(copied_files),
        include_dirs=tuple(copied_includes),
        defines=design.defines,
        filelists=tuple(copied_filelists),
        design_hash="",
    )
    return DesignInputV2(
        schema_version=candidate.schema_version,
        top=candidate.top,
        files=candidate.files,
        include_dirs=candidate.include_dirs,
        defines=candidate.defines,
        filelists=candidate.filelists,
        design_hash=_computed_design_hash(candidate),
    )


def _replace_expression(text: str, finding: Mapping[str, Any]) -> str:
    source = finding["source"]
    start = int(source["start_offset"])
    end = int(source["end_offset"])
    original = str(finding["original_expression"])
    if text[start:end] != original:
        raise MVPRewriteError(
            "finding source span is stale or ambiguous",
            code="stale_finding",
        )
    return text[:start] + str(finding["replacement_expression"]) + text[end:]


def _write_diff(
    baseline: DesignInputV2,
    candidate: DesignInputV2,
    path: Path,
) -> None:
    chunks: list[str] = []
    for original, rewritten in zip(baseline.files, candidate.files, strict=True):
        original_path = Path(original.path)
        rewritten_path = Path(rewritten.path)
        chunks.extend(
            difflib.unified_diff(
                _read_utf8_exact(original_path).splitlines(keepends=True),
                _read_utf8_exact(rewritten_path).splitlines(keepends=True),
                fromfile=original.path,
                tofile=rewritten.path,
            )
        )
    path.write_text("".join(chunks), encoding="utf-8")


def prepare_addition_candidate(
    design: DesignInputV2,
    finding_id: str,
    artifact_root: str | Path,
) -> dict[str, Any]:
    """Create an isolated, still-unproven candidate for a current finding."""

    _require_current_design(design)
    baseline_context = compile_context_snapshot(design)
    findings = scan_addition_sites(design)
    matches = [finding for finding in findings if finding["finding_id"] == finding_id]
    if len(matches) != 1:
        raise MVPRewriteError(
            f"finding {finding_id!r} is unavailable for the current source",
            code="stale_or_unknown_finding",
        )
    finding = matches[0]
    source_path = Path(str(finding["source"]["file"])).resolve()
    original_text = _read_utf8_exact(source_path)
    replacement_text = _replace_expression(original_text, finding)
    candidate_id = f"addcand_{stable_hash({'design_hash': design.design_hash, 'finding_id': finding_id, 'replacement': finding['replacement_expression']})[:16]}"
    root = Path(artifact_root).expanduser().resolve()
    candidate_dir = root / candidate_id
    # Keep the low-level immutable rewrite record separate from the public
    # Agent V2 stage record stored as ``candidate.json`` by mvp_agent.
    record_path = candidate_dir / "candidate-core.json"
    if record_path.is_file():
        cached = read_hashed_json(
            record_path,
            document_type=CANDIDATE_DOCUMENT_TYPE,
            schema_version=RUN_SCHEMA_VERSION,
        )
        if cached.get("baseline_design_hash") != design.design_hash:
            raise MVPRewriteError(
                "candidate ID collides with a different design",
                code="candidate_collision",
            )
        if cached.get("baseline_compile_context") != baseline_context:
            raise MVPRewriteError(
                "cached candidate baseline compile context is stale",
                code="stale_candidate",
            )
        candidate_design_from_record(cached)
        cached_diff = Path(str(cached.get("diff_path", "")))
        if not cached_diff.is_file() or file_sha256(cached_diff) != cached.get(
            "diff_sha256"
        ):
            raise MVPRewriteError(
                "cached candidate diff is stale",
                code="stale_candidate",
            )
        return cached
    if candidate_dir.exists() and any(candidate_dir.iterdir()):
        raise MVPRewriteError(
            f"incomplete candidate workspace already exists: {candidate_dir}",
            code="incomplete_candidate_artifact",
        )

    design_dir = candidate_dir / "design"
    design_dir.mkdir(parents=True, exist_ok=True)
    candidate_design = _copy_design(
        design,
        design_dir,
        replacement_path=source_path,
        replacement_text=replacement_text,
    )
    diff_path = candidate_dir / "candidate.diff"
    _write_diff(design, candidate_design, diff_path)
    after_copy_integrity = _design_integrity(design, baseline_context)
    if not after_copy_integrity["ok"]:
        raise MVPRewriteError(
            "original source changed while preparing the candidate",
            code="source_changed_during_prepare",
        )
    candidate_integrity = _design_integrity(candidate_design)
    if not candidate_integrity["ok"]:
        raise MVPRewriteError(
            "isolated candidate failed its source-integrity check",
            code="candidate_copy_error",
        )
    candidate_context = candidate_integrity["compile_context"]
    assert isinstance(candidate_context, Mapping)
    if not compile_contexts_compatible(baseline_context, candidate_context):
        raise MVPRewriteError(
            "isolated candidate compile context does not match the baseline",
            code="candidate_copy_error",
        )
    record = {
        "document_type": CANDIDATE_DOCUMENT_TYPE,
        "schema_version": RUN_SCHEMA_VERSION,
        "run_schema": RUN_SCHEMA_ID,
        "status": "candidate_prepared",
        "candidate_id": candidate_id,
        "finding_id": finding_id,
        "transformation_id": TRANSFORMATION_ID,
        "transformation_version": TRANSFORMATION_VERSION,
        "baseline_design_hash": design.design_hash,
        "candidate_design_hash": candidate_design.design_hash,
        "baseline_design": design.to_dict(),
        "candidate_design": candidate_design.to_dict(),
        "baseline_compile_context": baseline_context,
        "candidate_compile_context": candidate_context,
        "finding": finding,
        "artifact_dir": str(candidate_dir),
        "record_path": str(record_path),
        "diff_path": str(diff_path),
        "diff_sha256": file_sha256(diff_path),
        "source_integrity": {
            "original": after_copy_integrity,
            "candidate": candidate_integrity,
        },
        "lint": {"status": "not_run"},
        "formal": {"status": "not_run", "safe": False},
        "limitations": [
            "candidate is isolated but unproven",
            FORMAL_SEMANTICS,
        ],
    }
    write_hashed_json(record_path, record)
    return read_hashed_json(
        record_path,
        document_type=CANDIDATE_DOCUMENT_TYPE,
        schema_version=RUN_SCHEMA_VERSION,
    )


def _design_from_mapping(raw: Mapping[str, Any]) -> DesignInputV2:
    try:
        design = DesignInputV2(
            schema_version=int(raw["schema_version"]),
            top=str(raw["top"]),
            files=tuple(
                SourceFileV2(path=str(item["path"]), sha256=str(item["sha256"]))
                for item in raw["files"]
            ),
            include_dirs=tuple(str(item) for item in raw["include_dirs"]),
            defines=tuple(str(item) for item in raw["defines"]),
            filelists=tuple(str(item) for item in raw["filelists"]),
            design_hash=str(raw["design_hash"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MVPRewriteError("invalid design in candidate record", code="invalid_candidate") from exc
    return design


def candidate_design_from_record(candidate_record: Mapping[str, Any]) -> DesignInputV2:
    """Reconstruct and validate the isolated design needed by measurement."""

    _record_integrity(candidate_record)
    raw = candidate_record.get("candidate_design")
    if not isinstance(raw, Mapping):
        raise MVPRewriteError("candidate record has no candidate design", code="invalid_candidate")
    design = _design_from_mapping(raw)
    expected_context = candidate_record.get("candidate_compile_context")
    if not isinstance(expected_context, Mapping):
        raise MVPRewriteError(
            "candidate record has no compile-context snapshot",
            code="invalid_candidate",
        )
    integrity = _design_integrity(design, expected_context)
    if not integrity["ok"]:
        raise MVPRewriteError(
            "candidate design is stale",
            code="stale_candidate",
        )
    return design


def _record_integrity(candidate_record: Mapping[str, Any]) -> None:
    semantic_hash = candidate_record.get("semantic_hash")
    if not isinstance(semantic_hash, str):
        raise MVPRewriteError("candidate record has no semantic hash", code="invalid_candidate")
    core = {key: value for key, value in candidate_record.items() if key != "semantic_hash"}
    if stable_hash(core) != semantic_hash:
        raise MVPRewriteError("candidate record semantic hash mismatch", code="artifact_hash_mismatch")
    if candidate_record.get("document_type") != CANDIDATE_DOCUMENT_TYPE:
        raise MVPRewriteError("unexpected candidate document type", code="invalid_candidate")
    if candidate_record.get("schema_version") != RUN_SCHEMA_VERSION:
        raise MVPRewriteError("unsupported candidate schema", code="unsupported_schema")


def _yosys_quote(value: str | Path) -> str:
    raw = str(value)
    if any(character in raw for character in ("\x00", "\r", "\n")):
        raise MVPRewriteError(
            "Yosys arguments may not contain control characters",
            code="unsafe_compile_context",
        )
    return '"' + raw.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _yosys_read(design: DesignInputV2) -> str:
    parts = ["read_verilog", "-sv"]
    parts.extend(f"-I{_yosys_quote(path)}" for path in design.include_dirs)
    parts.extend(f"-D{_yosys_quote(definition)}" for definition in design.defines)
    parts.extend(_yosys_quote(source.path) for source in design.files)
    return " ".join(parts)


def _verilator_identity(config: ProjectConfig) -> dict[str, str]:
    configured = Path(config.tools.verilator).expanduser()
    if configured.is_absolute() or "/" in config.tools.verilator:
        resolved = configured.resolve()
    else:
        discovered = shutil.which(config.tools.verilator)
        resolved = Path(discovered).resolve() if discovered else configured
    if not resolved.is_file():
        raise MVPRewriteError(
            "Verilator executable could not be content-hashed",
            code="verilator_unavailable",
        )
    try:
        result = run_command(
            (config.tools.verilator, "--version"),
            timeout_seconds=config.tools.timeout_seconds,
        )
    except ToolExecutionError as exc:
        raise MVPRewriteError(str(exc), code="verilator_unavailable") from exc
    version = first_output_line(result)
    if (
        result.returncode != 0
        or version is None
        or re.match(r"^Verilator\s+[0-9]+(?:\.[0-9]+)+\b", version) is None
    ):
        raise MVPRewriteError(
            result.stderr or result.stdout or "unrecognized Verilator version response",
            code="verilator_identity_mismatch",
        )
    return {
        "version": version,
        "path": str(resolved),
        "sha256": file_sha256(resolved),
    }


def _verilator_lint(
    config: ProjectConfig,
    design: DesignInputV2,
    output_dir: Path,
    label: str,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / f"{label}-verilator.log"
    try:
        identity_before = _verilator_identity(config)
    except MVPRewriteError as exc:
        log_path.write_text(f"{exc}\n", encoding="utf-8")
        return {
            "status": "inconclusive",
            "returncode": None,
            "command": [],
            "log_path": str(log_path),
            "log_sha256": file_sha256(log_path),
            "identity": None,
            "identity_error": {"code": exc.code, "detail": str(exc)},
            "detail": str(exc),
        }
    command = (
        config.tools.verilator,
        "--lint-only",
        "--language",
        "1800-2017",
        "--Wall",
        "--Wno-fatal",
        "--Wno-DECLFILENAME",
        "--top-module",
        design.top,
        *(f"-I{path}" for path in design.include_dirs),
        *(f"-D{definition}" for definition in design.defines),
        *(source.path for source in design.files),
    )
    try:
        completed = run_command(
            command,
            timeout_seconds=config.tools.timeout_seconds,
            cwd=config.root,
        )
    except ToolExecutionError as exc:
        log_path.write_text(f"{exc}\n", encoding="utf-8")
        return {
            "status": "inconclusive",
            "returncode": None,
            "command": list(command),
            "log_path": str(log_path),
            "log_sha256": file_sha256(log_path),
            "identity": identity_before,
            "detail": str(exc),
        }
    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    log_path.write_text(combined + ("\n" if combined else ""), encoding="utf-8")
    try:
        identity_after = _verilator_identity(config)
    except MVPRewriteError as exc:
        identity_after = None
        identity_error: dict[str, str] | None = {
            "code": exc.code,
            "detail": str(exc),
        }
    else:
        identity_error = None
    identity_current = identity_after == identity_before
    blocking_warnings = [
        line.strip()
        for line in combined.splitlines()
        if re.search(r"%Warning-(?:MULTIDRIVEN|WIDTH[A-Z0-9_-]*)\b", line)
    ]
    status = (
        "inconclusive"
        if not identity_current
        else "failed"
        if completed.returncode != 0 or blocking_warnings
        else "passed"
    )
    return {
        "status": status,
        "returncode": completed.returncode,
        "command": list(command),
        "log_path": str(log_path),
        "log_sha256": file_sha256(log_path),
        "identity": identity_before if identity_current else None,
        "identity_before": identity_before,
        "identity_after": identity_after,
        "identity_error": identity_error,
        "blocking_warnings": blocking_warnings,
        "detail": (
            None
            if status == "passed"
            else "Verilator identity changed or became unavailable during lint"
            if not identity_current
            else combined
        ),
    }


def _pyslang_lint(design: DesignInputV2) -> dict[str, Any]:
    if design.include_dirs or design.defines:
        return {
            "status": "not_run",
            "detail": "PySlang helper does not receive the normalized include/define context",
        }
    if importlib.util.find_spec("pyslang") is None:
        return {"status": "unavailable", "detail": "optional PySlang package is not installed"}
    try:
        return lint_with_pyslang(design).to_dict()
    except RTLInputError as exc:
        return {"status": "unavailable", "detail": str(exc)}


def _formal_script(
    baseline: DesignInputV2,
    candidate: DesignInputV2,
) -> str:
    return "\n".join(
        (
            _yosys_read(baseline),
            f"prep -top {baseline.top}",
            "design -stash baseline_design",
            "design -reset",
            _yosys_read(candidate),
            f"prep -top {candidate.top}",
            "design -stash candidate_design",
            "design -reset",
            f"design -copy-from baseline_design -as gold {baseline.top}",
            f"design -copy-from candidate_design -as gate {candidate.top}",
            "equiv_make gold gate equiv",
            "hierarchy -check -top equiv",
            "equiv_simple",
            "equiv_status -assert",
            "",
        )
    )


def _prove(
    config: ProjectConfig,
    baseline: DesignInputV2,
    candidate: DesignInputV2,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    script_path = output_dir / "equivalence.ys"
    log_path = output_dir / "equivalence.log"
    script = _formal_script(baseline, candidate)
    script_path.write_text(script, encoding="utf-8")
    command = (config.tools.yosys, "-Q", "-s", str(script_path))
    from rtl_advisor.mvp_measure import MVPMeasurementError, _yosys_identity

    try:
        tool_identity_before = _yosys_identity(config)
    except MVPMeasurementError as exc:
        log_path.write_text(f"{exc}\n", encoding="utf-8")
        return {
            "backend": FORMAL_BACKEND,
            "semantics": FORMAL_SEMANTICS,
            "status": "inconclusive",
            "returncode": None,
            "command": list(command),
            "script_path": str(script_path),
            "script_sha256": file_sha256(script_path),
            "log_path": str(log_path),
            "log_sha256": file_sha256(log_path),
            "tool_identity": None,
            "tool_identity_error": {"code": exc.code, "detail": str(exc)},
            "detail": f"formal Yosys identity could not be trusted: {exc}",
        }
    try:
        completed = run_command(
            command,
            timeout_seconds=config.tools.timeout_seconds,
            cwd=config.root,
        )
    except ToolExecutionError as exc:
        log_path.write_text(f"{exc}\n", encoding="utf-8")
        return {
            "backend": FORMAL_BACKEND,
            "semantics": FORMAL_SEMANTICS,
            "status": "inconclusive",
            "returncode": None,
            "command": list(command),
            "script_path": str(script_path),
            "script_sha256": file_sha256(script_path),
            "log_path": str(log_path),
            "log_sha256": file_sha256(log_path),
            "tool_identity": tool_identity_before,
            "detail": str(exc),
        }
    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    log_path.write_text(combined + ("\n" if combined else ""), encoding="utf-8")
    try:
        tool_identity_after = _yosys_identity(config)
    except MVPMeasurementError as exc:
        tool_identity_after = None
        identity_error: dict[str, Any] | None = {"code": exc.code, "detail": str(exc)}
    else:
        identity_error = None
    identity_current = tool_identity_after == tool_identity_before
    if not identity_current:
        status = "inconclusive"
        detail = "formal Yosys identity changed or became unavailable during proof"
    elif completed.returncode == 0 and _FORMAL_SUCCESS_MARKER in combined:
        status = "passed"
        detail = None
    elif completed.returncode == 0:
        status = "inconclusive"
        detail = "Yosys exited successfully without the equivalence success marker"
    elif "unproven $equiv" in combined or "Found unproven $equiv" in combined:
        status = "failed"
        detail = "Yosys left one or more equivalence cells unproven"
    else:
        status = "inconclusive"
        detail = combined or f"Yosys exited {completed.returncode}"
    return {
        "backend": FORMAL_BACKEND,
        "semantics": FORMAL_SEMANTICS,
        "status": status,
        "returncode": completed.returncode,
        "command": list(command),
        "script_path": str(script_path),
        "script_sha256": file_sha256(script_path),
        "log_path": str(log_path),
        "log_sha256": file_sha256(log_path),
        "tool_identity": tool_identity_before if identity_current else None,
        "tool_identity_before": tool_identity_before,
        "tool_identity_after": tool_identity_after,
        "tool_identity_error": identity_error,
        "success_marker_seen": _FORMAL_SUCCESS_MARKER in combined,
        "detail": detail,
    }


def _tool_version(command: tuple[str, ...], timeout_seconds: int) -> str | None:
    try:
        result = run_command(command, timeout_seconds=timeout_seconds)
    except ToolExecutionError:
        return None
    return first_output_line(result) if result.returncode == 0 else None


def verify_addition_candidate(
    config: ProjectConfig,
    candidate_record: Mapping[str, Any],
    artifact_root: str | Path,
) -> dict[str, Any]:
    """Lint and formally prove a hash-current isolated candidate."""

    _record_integrity(candidate_record)
    candidate_id = str(candidate_record.get("candidate_id", ""))
    if not candidate_id:
        raise MVPRewriteError("candidate record has no ID", code="invalid_candidate")
    expected_dir = Path(artifact_root).expanduser().resolve() / candidate_id
    recorded_dir = Path(str(candidate_record.get("artifact_dir", ""))).expanduser().resolve()
    if recorded_dir != expected_dir:
        raise MVPRewriteError(
            "candidate record does not belong to the requested artifact root",
            code="artifact_root_mismatch",
        )
    baseline_raw = candidate_record.get("baseline_design")
    if not isinstance(baseline_raw, Mapping):
        raise MVPRewriteError("candidate record has no baseline design", code="invalid_candidate")
    baseline = _design_from_mapping(baseline_raw)
    candidate = candidate_design_from_record(candidate_record)
    baseline_context = candidate_record.get("baseline_compile_context")
    candidate_context = candidate_record.get("candidate_compile_context")
    if not isinstance(baseline_context, Mapping) or not isinstance(
        candidate_context, Mapping
    ):
        raise MVPRewriteError(
            "candidate record has no compile-context snapshots",
            code="invalid_candidate",
        )
    before = {
        "original": _design_integrity(baseline, baseline_context),
        "candidate": _design_integrity(candidate, candidate_context),
    }
    diff_path = Path(str(candidate_record.get("diff_path", "")))
    diff_ok = diff_path.is_file() and file_sha256(diff_path) == candidate_record.get("diff_sha256")
    before["diff"] = {"ok": diff_ok}
    if not all(item["ok"] for item in before.values()):
        raise MVPRewriteError(
            "candidate, baseline, or diff is stale",
            code="stale_candidate",
        )

    output_dir = expected_dir / "formal"
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / "formal.json"
    if result_path.is_file():
        return read_hashed_json(
            result_path,
            document_type=FORMAL_DOCUMENT_TYPE,
            schema_version=RUN_SCHEMA_VERSION,
        )

    baseline_verilator = _verilator_lint(config, baseline, output_dir, "baseline")
    candidate_verilator = _verilator_lint(config, candidate, output_dir, "candidate")
    pyslang_result = {
        "baseline": _pyslang_lint(baseline),
        "candidate": _pyslang_lint(candidate),
    }
    verilator_statuses = {
        baseline_verilator["status"],
        candidate_verilator["status"],
    }
    pyslang_statuses = {
        pyslang_result["baseline"]["status"],
        pyslang_result["candidate"]["status"],
    }
    if "failed" in verilator_statuses or "failed" in pyslang_statuses:
        formal = {
            "backend": FORMAL_BACKEND,
            "semantics": FORMAL_SEMANTICS,
            "status": "inconclusive",
            "detail": (
                "baseline or candidate failed RTL compile/lint; "
                "the equivalence proof was not run"
            ),
        }
    elif "inconclusive" in verilator_statuses:
        formal = {
            "backend": FORMAL_BACKEND,
            "semantics": FORMAL_SEMANTICS,
            "status": "inconclusive",
            "detail": "Verilator lint could not be completed",
        }
    else:
        formal = _prove(config, baseline, candidate, output_dir)

    after = {
        "original": _design_integrity(baseline, baseline_context),
        "candidate": _design_integrity(candidate, candidate_context),
        "diff": {
            "ok": diff_path.is_file()
            and file_sha256(diff_path) == candidate_record.get("diff_sha256")
        },
    }
    current = all(item["ok"] for item in after.values())
    if not current:
        formal = {
            **formal,
            "status": "inconclusive",
            "detail": "source integrity changed during verification",
        }
    formal_status = formal["status"]
    status = {
        "passed": "formal_passed",
        "failed": "formal_failed",
        "inconclusive": "formal_inconclusive",
    }[formal_status]
    result = {
        "document_type": FORMAL_DOCUMENT_TYPE,
        "schema_version": RUN_SCHEMA_VERSION,
        "run_schema": RUN_SCHEMA_ID,
        "status": status,
        "safe": formal_status == "passed" and current,
        "candidate_id": candidate_id,
        "finding_id": candidate_record.get("finding_id"),
        "baseline_design_hash": baseline.design_hash,
        "candidate_design_hash": candidate.design_hash,
        "compile_context": {
            "baseline": after["original"].get("compile_context"),
            "candidate": after["candidate"].get("compile_context"),
        },
        "source_integrity": after,
        "lint": {
            "status": (
                "passed"
                if verilator_statuses == {"passed"} and "failed" not in pyslang_statuses
                else "failed"
                if "failed" in verilator_statuses or "failed" in pyslang_statuses
                else "inconclusive"
            ),
            "verilator": {
                "version": _tool_version(
                    (config.tools.verilator, "--version"),
                    config.tools.timeout_seconds,
                ),
                "baseline": baseline_verilator,
                "candidate": candidate_verilator,
            },
            "pyslang": pyslang_result,
        },
        "formal": {
            **formal,
            "yosys_version": (
                (formal.get("tool_identity") or {}).get("yosys_version")
                if isinstance(formal.get("tool_identity"), Mapping)
                else None
            ),
            "yosys_path": (
                (formal.get("tool_identity") or {}).get("yosys_path")
                if isinstance(formal.get("tool_identity"), Mapping)
                else None
            ),
            "yosys_sha256": (
                (formal.get("tool_identity") or {}).get("yosys_sha256")
                if isinstance(formal.get("tool_identity"), Mapping)
                else None
            ),
        },
        "artifact_dir": str(output_dir),
        "record_path": str(result_path),
        "limitations": [FORMAL_SEMANTICS],
    }
    write_hashed_json(result_path, result)
    return read_hashed_json(
        result_path,
        document_type=FORMAL_DOCUMENT_TYPE,
        schema_version=RUN_SCHEMA_VERSION,
    )
