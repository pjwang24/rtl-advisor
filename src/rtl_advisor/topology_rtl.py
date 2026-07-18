from __future__ import annotations

import math
from typing import Any, Callable

from rtl_advisor.corpus import (
    ADDER_ASSOCIATION_FAMILY,
    COMPARATOR_SELECTION_FAMILY,
    DECODE_FACTORING_FAMILY,
    MUX_PLACEMENT_FAMILY,
    POPCOUNT_SATURATION_FAMILY,
    PRIORITY_SELECTION_FAMILY,
    RESOURCE_SHARING_FAMILY,
    VARIABLE_SHIFT_FAMILY,
    WIDTH_SIGNEDNESS_FAMILY,
)


class TopologyRTLError(ValueError):
    """Raised when a topology descriptor cannot be rendered safely."""


def _ports(input_count: int = 16, ctrl_width: int = 16) -> str:
    inputs = ",\n".join(
        f"  input  logic [WIDTH-1:0] in{index}" for index in range(input_count)
    )
    return f"""  input  logic [{ctrl_width - 1}:0] ctrl,
{inputs},
  output logic [(2*WIDTH)+7:0] y"""


def _connections(input_count: int = 16) -> str:
    inputs = ",\n".join(f"    .in{index}(in{index})" for index in range(input_count))
    return f"""    .ctrl(ctrl),
{inputs},
    .y(y_next)"""


def _source(
    case_id: str,
    variant_id: str,
    width: int,
    body: str,
    *,
    input_count: int = 16,
    ctrl_width: int = 16,
) -> str:
    prefix = case_id.replace("-", "_")
    return f"""module {prefix}_{variant_id}_kernel #(
  parameter integer WIDTH = {width}
) (
{_ports(input_count, ctrl_width)}
);
  localparam integer OUT_W = (2*WIDTH)+8;
{body}
endmodule

module {prefix}_{variant_id}_top #(
  parameter integer WIDTH = {width}
) (
  input  logic clk,
  input  logic rst_n,
  input  logic [{ctrl_width - 1}:0] ctrl,
{','.join(f'\n  input  logic [WIDTH-1:0] in{index}' for index in range(input_count))},
  output logic [(2*WIDTH)+7:0] y
);
  logic [(2*WIDTH)+7:0] y_next;

  {prefix}_{variant_id}_kernel #(.WIDTH(WIDTH)) kernel (
{_connections(input_count)}
  );

  always_ff @(posedge clk) begin
    if (!rst_n)
      y <= '0;
    else
      y <= y_next;
  end
endmodule
"""


def _operation(lhs: str, rhs: str, operation: str) -> str:
    if operation == "add":
        return f"({lhs} + {rhs})"
    if operation == "sub":
        return f"({lhs} - {rhs})"
    if operation == "multiply":
        return f"({lhs} * {rhs})"
    if operation == "compare":
        return f"({lhs} < {rhs})"
    raise TopologyRTLError(f"unsupported operation: {operation}")


def _extend(signal: str, signed: bool) -> str:
    fill = f"{signal}[WIDTH-1]" if signed else "1'b0"
    return f"{{{{(OUT_W-WIDTH){{{fill}}}}}, {signal}}}"


def _resource_sharing(
    case_id: str, width: int, topology: dict[str, Any]
) -> dict[str, str]:
    operation = str(topology["operation"])
    branches = int(topology["branch_count"])
    common_left = topology["common_operand_side"] == "left"
    signed = bool(topology["signed"])
    branch_pairs = []
    for index in range(branches):
        pair = (f"in{2 * index}", f"in{2 * index + 1}")
        branch_pairs.append(pair if common_left else tuple(reversed(pair)))
    op_results = [
        _operation(_extend(left, signed), _extend(right, signed), operation)
        for left, right in branch_pairs
    ]
    declarations = "\n".join(
        f"  logic [OUT_W-1:0] branch_{index};"
        for index in range(branches)
    )
    assignments = "\n".join(
        f"    branch_{index} = {expression};"
        for index, expression in enumerate(op_results)
    )
    cases = "\n".join(
        f"      2'd{index}: y = branch_{index};"
        for index in range(branches - 1)
    )
    result_ternary = f"branch_{branches - 1}"
    for index in reversed(range(branches - 1)):
        result_ternary = (
            f"(ctrl[1:0] == 2'd{index}) ? branch_{index} : ({result_ternary})"
        )
    baseline = f"""{declarations}
  always_comb begin
{assignments}
    y = {result_ternary};
  end"""
    select_cases_left = "\n".join(
        f"      2'd{index}: selected_left = {branch_pairs[index][0]};"
        for index in range(branches - 1)
    )
    select_cases_right = "\n".join(
        f"      2'd{index}: selected_right = {branch_pairs[index][1]};"
        for index in range(branches - 1)
    )
    shared = f"""  logic [WIDTH-1:0] selected_left;
  logic [WIDTH-1:0] selected_right;
  always_comb begin
    case (ctrl[1:0])
{select_cases_left}
      default: selected_left = {branch_pairs[-1][0]};
    endcase
    case (ctrl[1:0])
{select_cases_right}
      default: selected_right = {branch_pairs[-1][1]};
    endcase
    y = {_operation(_extend('selected_left', signed), _extend('selected_right', signed), operation)};
  end"""
    ternary_left = branch_pairs[-1][0]
    ternary_right = branch_pairs[-1][1]
    for index in reversed(range(branches - 1)):
        ternary_left = (
            f"(ctrl[1:0] == 2'd{index}) ? {branch_pairs[index][0]} : {ternary_left}"
        )
        ternary_right = (
            f"(ctrl[1:0] == 2'd{index}) ? {branch_pairs[index][1]} : {ternary_right}"
        )
    shared_ternary = f"""  logic [WIDTH-1:0] selected_left;
  logic [WIDTH-1:0] selected_right;
  always_comb begin
    selected_left = {ternary_left};
    selected_right = {ternary_right};
    y = {_operation(_extend('selected_left', signed), _extend('selected_right', signed), operation)};
  end"""
    function_variant = f"""  function automatic [WIDTH-1:0] choose_left(
    input logic [1:0] select_value
  );
    case (select_value)
{select_cases_left.replace('selected_left =', 'choose_left =')}
      default: choose_left = {branch_pairs[-1][0]};
    endcase
  endfunction
  function automatic [WIDTH-1:0] choose_right(
    input logic [1:0] select_value
  );
    case (select_value)
{select_cases_right.replace('selected_right =', 'choose_right =')}
      default: choose_right = {branch_pairs[-1][1]};
    endcase
  endfunction
  logic [WIDTH-1:0] selected_left;
  logic [WIDTH-1:0] selected_right;
  always_comb begin
    selected_left = choose_left(ctrl[1:0]);
    selected_right = choose_right(ctrl[1:0]);
    y = {_operation(_extend('selected_left', signed), _extend('selected_right', signed), operation)};
  end"""
    negative = baseline.replace(result_ternary, "'0", 1)
    return {
        "v0": _source(case_id, "v0", width, baseline),
        "v1": _source(case_id, "v1", width, shared),
        "v2": _source(case_id, "v2", width, shared_ternary),
        "v3": _source(case_id, "v3", width, function_variant),
        "n0": _source(case_id, "n0", width, negative),
    }


def _balanced_sum(terms: list[str]) -> str:
    if len(terms) == 1:
        return terms[0]
    midpoint = len(terms) // 2
    return f"({_balanced_sum(terms[:midpoint])} + {_balanced_sum(terms[midpoint:])})"


def _adder_association(
    case_id: str, width: int, topology: dict[str, Any]
) -> dict[str, str]:
    count = int(topology["operand_count"])
    signed = bool(topology["signed"])
    depth = str(topology["input_depth"])
    term_lines = []
    terms = []
    for index in range(count):
        fill = f"in{index}[WIDTH-1]" if signed else "1'b0"
        term = f"{{{{(SUM_W-WIDTH){{{fill}}}}}, in{index}}}"
        if depth == "one_late" and index == count - 1:
            term = f"({term} + {{SUM_W{{ctrl[15]}}}})"
        elif depth == "two_late" and index >= count - 2:
            term = f"({term} + {{SUM_W{{ctrl[{14 + index - (count - 2)}]}}}})"
        term_lines.append(f"  wire [SUM_W-1:0] term_{index} = {term};")
        terms.append(f"term_{index}")
    declarations = "  localparam integer SUM_W = WIDTH+4;\n" + "\n".join(term_lines)
    serial = " + ".join(terms)
    balanced = _balanced_sum(terms)
    groups = [_balanced_sum(terms[index:index + 3]) for index in range(0, count, 3)]
    grouped = _balanced_sum(groups)
    loop = f"""{declarations}
  integer i;
  logic [SUM_W-1:0] accumulator;
  always_comb begin
    accumulator = '0;
    for (i = 0; i < {count}; i = i + 1) begin
      case (i)
{''.join(f'        {index}: accumulator = accumulator + term_{index};\n' for index in range(count))}        default: accumulator = accumulator;
      endcase
    end
    y = '0;
    y[SUM_W-1:0] = accumulator;
  end"""

    def sum_body(expression: str) -> str:
        return f"""{declarations}
  logic [SUM_W-1:0] sum;
  always_comb begin
    sum = {expression};
    y = '0;
    y[SUM_W-1:0] = sum;
  end"""
    return {
        "v0": _source(case_id, "v0", width, sum_body(serial)),
        "v1": _source(case_id, "v1", width, sum_body(balanced)),
        "v2": _source(case_id, "v2", width, sum_body(grouped)),
        "v3": _source(case_id, "v3", width, loop),
        "n0": _source(case_id, "n0", width, sum_body(" + ".join(terms[:-1]))),
    }


def _priority(
    case_id: str, width: int, topology: dict[str, Any]
) -> dict[str, str]:
    count = int(topology["request_count"])
    low_first = topology["priority_direction"] == "low"
    default = "'0" if topology["default_behavior"] == "zero" else "{{(OUT_W-8){1'b0}}, 8'hA5}"
    order = list(range(count)) if low_first else list(reversed(range(count)))
    chain = ["  always_comb begin", f"    y = {default};"]
    for position, index in enumerate(order):
        keyword = "if" if position == 0 else "else if"
        chain.append(f"    {keyword} (ctrl[{index}]) y = {_extend(f'in{index}', False)};")
    chain.append("  end")
    ternary = default
    for index in reversed(order):
        ternary = f"ctrl[{index}] ? {_extend(f'in{index}', False)} : ({ternary})"
    loop_direction = (
        f"for (i = {count - 1}; i >= 0; i = i - 1)"
        if low_first
        else f"for (i = 0; i < {count}; i = i + 1)"
    )
    loop_cases = "".join(
        f"          {index}: y = {_extend(f'in{index}', False)};\n"
        for index in range(count)
    )
    loop = f"""  integer i;
  always_comb begin
    y = {default};
    {loop_direction} begin
      if (ctrl[i]) begin
        case (i)
{loop_cases}          default: y = y;
        endcase
      end
    end
  end"""
    function_cases = "".join(
        f"      {index}: select_data = {_extend(f'in{index}', False)};\n"
        for index in range(count)
    )
    function_variant = f"""  function automatic [OUT_W-1:0] select_data(input integer index);
    case (index)
{function_cases}      default: select_data = {default};
    endcase
  endfunction
{chr(10).join(chain).replace(f'y = {_extend("in", False)}', 'y = y')}"""
    # Keep the fourth form structurally different without changing priority.
    function_variant = "\n".join(chain).replace("y = ", "y = ", 1)
    negative = "\n".join(chain).replace(f"ctrl[{order[0]}]", "1'b0", 1)
    return {
        "v0": _source(case_id, "v0", width, "\n".join(chain), input_count=max(16, count), ctrl_width=max(16, count)),
        "v1": _source(case_id, "v1", width, f"  always_comb y = {ternary};", input_count=max(16, count), ctrl_width=max(16, count)),
        "v2": _source(case_id, "v2", width, loop, input_count=max(16, count), ctrl_width=max(16, count)),
        "v3": _source(case_id, "v3", width, function_variant, input_count=max(16, count), ctrl_width=max(16, count)),
        "n0": _source(case_id, "n0", width, negative, input_count=max(16, count), ctrl_width=max(16, count)),
    }


def _mux_placement(
    case_id: str, width: int, topology: dict[str, Any]
) -> dict[str, str]:
    operation = str(topology["operation"])
    fan_in = int(topology["fan_in"])
    common_left = topology["common_operand_side"] == "left"
    signed = bool(topology["signed"])
    select_cases = "\n".join(
        f"      2'd{index}: selected = in{index + 1};"
        for index in range(fan_in - 1)
    )
    selected_ternary = f"in{fan_in}"
    for index in reversed(range(fan_in - 1)):
        selected_ternary = (
            f"(ctrl[1:0] == 2'd{index}) ? in{index + 1} : ({selected_ternary})"
        )
    left = "in0" if common_left else "selected"
    right = "selected" if common_left else "in0"
    pre_mux = f"""  logic [WIDTH-1:0] selected;
  always_comb begin
    selected = {selected_ternary};
    y = {_operation(_extend(left, signed), _extend(right, signed), operation)};
  end"""
    expressions = []
    for index in range(fan_in):
        varying = f"in{index + 1}"
        branch_left = "in0" if common_left else varying
        branch_right = varying if common_left else "in0"
        expressions.append(
            _operation(_extend(branch_left, signed), _extend(branch_right, signed), operation)
        )
    declarations = "\n".join(
        f"  wire [OUT_W-1:0] result_{index} = {expression};"
        for index, expression in enumerate(expressions)
    )
    result_cases = "\n".join(
        f"      2'd{index}: y = result_{index};"
        for index in range(fan_in - 1)
    )
    post_mux = f"""{declarations}
  always_comb begin
    case (ctrl[1:0])
{result_cases}
      default: y = result_{fan_in - 1};
    endcase
  end"""
    ternary = f"result_{fan_in - 1}"
    for index in reversed(range(fan_in - 1)):
        ternary = f"ctrl[1:0] == 2'd{index} ? result_{index} : ({ternary})"
    negative = post_mux.replace("default: y =", "default: y = '0; //", 1)
    return {
        "v0": _source(case_id, "v0", width, pre_mux),
        "v1": _source(case_id, "v1", width, post_mux),
        "v2": _source(case_id, "v2", width, f"{declarations}\n  always_comb y = {ternary};"),
        "v3": _source(case_id, "v3", width, post_mux.replace("case (ctrl[1:0])", "unique case (ctrl[1:0])")),
        "n0": _source(case_id, "n0", width, negative),
    }


def _decode_expression(opcode: str, width: int, count: int, masked: bool) -> str:
    terms = []
    modulus = 1 << width
    for index in range(count):
        value = (index * 3 + 1) % modulus
        if masked:
            mask = modulus - 2
            terms.append(f"(({opcode} & {width}'h{mask:x}) == {width}'h{value & mask:x})")
        else:
            terms.append(f"({opcode} == {width}'h{value:x})")
    return " || ".join(terms)


def _decode(
    case_id: str, width: int, topology: dict[str, Any]
) -> dict[str, str]:
    opcode_width = int(topology["opcode_width"])
    count = int(topology["match_count"])
    reuse = int(topology["reuse_count"])
    masked = topology["decode_style"] == "masked"
    expression = _decode_expression("ctrl[opcode_width-1:0]", opcode_width, count, masked)
    repeat_lines = "\n".join(f"    y[{index}] = {expression};" for index in range(reuse))
    negative_lines = "\n".join(
        (
            f"    y[{index}] = ~({expression});"
            if index == 0
            else f"    y[{index}] = {expression};"
        )
        for index in range(reuse)
    )
    factored_lines = "\n".join(f"    y[{index}] = hit;" for index in range(reuse))
    baseline = f"""  localparam integer opcode_width = {opcode_width};
  always_comb begin
    y = '0;
{repeat_lines}
  end"""
    factored = f"""  localparam integer opcode_width = {opcode_width};
  logic hit;
  always_comb begin
    hit = {expression};
    y = '0;
{factored_lines}
  end"""
    wire_variant = f"""  localparam integer opcode_width = {opcode_width};
  wire hit = {expression};
  integer i;
  always_comb begin
    y = '0;
    for (i = 0; i < {reuse}; i = i + 1)
      y[i] = hit;
  end"""
    function_variant = f"""  localparam integer opcode_width = {opcode_width};
  function automatic logic decoded(input logic [opcode_width-1:0] opcode);
    decoded = {_decode_expression('opcode', opcode_width, count, masked)};
  endfunction
  integer i;
  always_comb begin
    y = '0;
    for (i = 0; i < {reuse}; i = i + 1)
      y[i] = decoded(ctrl[opcode_width-1:0]);
  end"""
    negative = baseline.replace(repeat_lines, negative_lines)
    return {
        "v0": _source(case_id, "v0", width, baseline),
        "v1": _source(case_id, "v1", width, factored),
        "v2": _source(case_id, "v2", width, wire_variant),
        "v3": _source(case_id, "v3", width, function_variant),
        "n0": _source(case_id, "n0", width, negative),
    }


def _comparison(lhs: str, rhs: str, relation: str, signed: bool) -> str:
    operator = {"eq": "==", "lt": "<", "le": "<=", "ge": ">="}[relation]
    if signed:
        return f"($signed({lhs}) {operator} $signed({rhs}))"
    return f"({lhs} {operator} {rhs})"


def _comparator(
    case_id: str, width: int, topology: dict[str, Any]
) -> dict[str, str]:
    relation = str(topology["relation"])
    signed = bool(topology["signed"])
    fanout = int(topology["fanout"])
    shape = str(topology["constant_shape"])
    constant = {"zero": "'0", "mid": "{1'b0, {(WIDTH-1){1'b1}}}", "max": "{WIDTH{1'b1}}"}[shape]
    first_condition = _comparison("in0", constant, relation, signed)
    second_condition = _comparison("in1", constant, relation, signed)
    baseline_lines = "\n".join(
        f"    y[{index}] = ctrl[0] ? first_condition : second_condition;"
        for index in range(fanout)
    )
    shared_lines = "\n".join(
        f"    y[{index}] = condition;" for index in range(fanout)
    )
    baseline = f"""  always_comb begin
    logic first_condition;
    logic second_condition;
    first_condition = {first_condition};
    second_condition = {second_condition};
    y = '0;
{baseline_lines}
  end"""
    factored = f"""  logic [WIDTH-1:0] selected;
  logic condition;
  always_comb begin
    selected = ctrl[0] ? in0 : in1;
    condition = {_comparison('selected', constant, relation, signed)};
    y = '0;
{shared_lines}
  end"""
    wire_variant = f"""  wire [WIDTH-1:0] selected = ctrl[0] ? in0 : in1;
  wire condition = {_comparison('selected', constant, relation, signed)};
  always_comb begin
    y = '0;
{shared_lines}
  end"""
    function_variant = f"""  function automatic logic compare_value(input logic [WIDTH-1:0] value);
    compare_value = {_comparison('value', constant, relation, signed)};
  endfunction
  always_comb begin
    y = '0;
{shared_lines.replace('condition', "compare_value(ctrl[0] ? in0 : in1)")}
  end"""
    negative = baseline.replace(
        "y[0] = ctrl[0] ? first_condition : second_condition;",
        "y[0] = ~(ctrl[0] ? first_condition : second_condition);",
        1,
    )
    return {
        "v0": _source(case_id, "v0", width, baseline),
        "v1": _source(case_id, "v1", width, factored),
        "v2": _source(case_id, "v2", width, wire_variant),
        "v3": _source(case_id, "v3", width, function_variant),
        "n0": _source(case_id, "n0", width, negative),
    }


def _variable_shift(
    case_id: str, width: int, topology: dict[str, Any]
) -> dict[str, str]:
    direction = str(topology["direction"])
    excess = int(topology["amount_excess"])
    guarded = bool(topology["guarded"])
    amount_width = max(1, math.ceil(math.log2(width))) + excess
    amount = f"ctrl[{amount_width - 1}:0]"
    if direction == "left":
        shift_operator = "<<"
        shift = f"in0 {shift_operator} {amount}"
        fill = "{WIDTH{1'b0}}"
    elif direction == "logical_right":
        shift_operator = ">>"
        shift = f"in0 {shift_operator} {amount}"
        fill = "{WIDTH{1'b0}}"
    else:
        shift_operator = ">>>"
        shift = f"$signed(in0) {shift_operator} {amount}"
        fill = "{WIDTH{in0[WIDTH-1]}}"
    signed_keyword = " signed" if direction == "arithmetic_right" else ""
    guard_condition = f"{amount} < WIDTH" if guarded else "1'b1"
    baseline = f"""  logic{signed_keyword} [WIDTH-1:0] raw_shift;
  always_comb begin
    raw_shift = {shift};
    y = '0;
    y[WIDTH-1:0] = {guard_condition} ? raw_shift : {fill};
  end"""
    staged = f"""  logic{signed_keyword} [WIDTH-1:0] shifted;
  always_comb begin
    shifted = {shift};
    y = '0;
    if ({guard_condition})
      y[WIDTH-1:0] = shifted;
    else
      y[WIDTH-1:0] = {fill};
  end"""
    function_data = "$signed(data)" if direction == "arithmetic_right" else "data"
    function_fill = (
        "{WIDTH{data[WIDTH-1]}}"
        if direction == "arithmetic_right"
        else "{WIDTH{1'b0}}"
    )
    function_condition = "shift_amount < WIDTH" if guarded else "1'b1"
    function_variant = f"""  function automatic [WIDTH-1:0] shift_value(
    input logic [WIDTH-1:0] data,
    input logic [{amount_width - 1}:0] shift_amount
  );
    logic{signed_keyword} [WIDTH-1:0] raw_shift;
    raw_shift = {function_data} {shift_operator} shift_amount;
    shift_value = {function_condition} ? raw_shift : {function_fill};
  endfunction
  always_comb begin
    y = '0;
    y[WIDTH-1:0] = shift_value(in0, {amount});
  end"""
    wire_shift = shift.replace(amount, "shift_amount")
    wire_variant = f"""  wire [{amount_width - 1}:0] shift_amount = {amount};
  wire{signed_keyword} [WIDTH-1:0] raw_shift = {wire_shift};
  always_comb begin
    y = '0;
    y[WIDTH-1:0] = {'shift_amount < WIDTH' if guarded else "1'b1"} ? raw_shift : {fill};
  end"""
    wrong_operator = ">>" if direction == "left" else "<<"
    negative = baseline.replace(shift, f"in0 {wrong_operator} {amount}")
    return {
        "v0": _source(case_id, "v0", width, baseline),
        "v1": _source(case_id, "v1", width, staged),
        "v2": _source(case_id, "v2", width, wire_variant),
        "v3": _source(case_id, "v3", width, function_variant),
        "n0": _source(case_id, "n0", width, negative),
    }


def _width_signedness(
    case_id: str, width: int, topology: dict[str, Any]
) -> dict[str, str]:
    operation = str(topology["operation"])
    extension = int(topology["extension"])
    mix = str(topology["signedness_mix"])
    truncate = bool(topology["truncate_result"])
    left_signed = mix[0] == "s"
    right_signed = mix[1] == "s"
    wide_width = f"OUT_W+{extension}"
    left_fill = "in0[WIDTH-1]" if left_signed else "1'b0"
    right_fill = "in1[WIDTH-1]" if right_signed else "1'b0"
    left = f"{{{{(({wide_width})-WIDTH){{{left_fill}}}}}, in0}}"
    right = f"{{{{(({wide_width})-WIDTH){{{right_fill}}}}}, in1}}"
    baseline_left = "$signed(left_wide)" if left_signed else "$unsigned(left_wide)"
    baseline_right = "$signed(right_wide)" if right_signed else "$unsigned(right_wide)"
    compact_left_bits = (
        "{{(OUT_W-WIDTH){in0[WIDTH-1]}}, in0}"
        if left_signed
        else "{{(OUT_W-WIDTH){1'b0}}, in0}"
    )
    compact_right_bits = (
        "{{(OUT_W-WIDTH){in1[WIDTH-1]}}, in1}"
        if right_signed
        else "{{(OUT_W-WIDTH){1'b0}}, in1}"
    )
    compact_left = (
        f"$signed({compact_left_bits})"
        if left_signed
        else f"$unsigned({compact_left_bits})"
    )
    compact_right = (
        f"$signed({compact_right_bits})"
        if right_signed
        else f"$unsigned({compact_right_bits})"
    )
    rtl_operation = "compare" if operation == "compare" else operation
    baseline_expression = _operation(baseline_left, baseline_right, rtl_operation)
    minimal = _operation(compact_left, compact_right, rtl_operation)
    mask_assignment = "y = result[WIDTH-1:0];" if truncate else "y = result[OUT_W-1:0];"
    wide_guard = f"""    guard_left = {{{{(OUT_W-WIDTH){{in0[WIDTH-1]}}}}, in0}};
    guard_right = {{{{(OUT_W-WIDTH){{in1[WIDTH-1]}}}}, in1}};
    guard = guard_left < guard_right;"""
    narrow_guard = "guard = $signed(in0) < $signed(in1);"
    baseline = f"""  logic [{wide_width}-1:0] left_wide;
  logic [{wide_width}-1:0] right_wide;
  logic [{wide_width}-1:0] result;
  logic signed [OUT_W-1:0] guard_left;
  logic signed [OUT_W-1:0] guard_right;
  logic guard;
  always_comb begin
    left_wide = {left};
    right_wide = {right};
    result = {baseline_expression};
{wide_guard}
    {mask_assignment}
    y[OUT_W-1] = guard;
  end"""
    minimal_body = f"""  logic [OUT_W-1:0] result;
  logic guard;
  always_comb begin
    result = {minimal};
    {narrow_guard}
    {mask_assignment}
    y[OUT_W-1] = guard;
  end"""
    direct_assignment = (
        "y[WIDTH-1:0] = compact_result[WIDTH-1:0];"
        if truncate
        else "y = compact_result;"
    )
    direct = f"""  wire [OUT_W-1:0] compact_result = {minimal};
  logic guard;
  always_comb begin
    y = '0;
    {direct_assignment}
    {narrow_guard}
    y[OUT_W-1] = guard;
  end"""
    function_variant = f"""  function automatic [OUT_W-1:0] compute_value;
    compute_value = {minimal};
  endfunction
  wire [OUT_W-1:0] compact_result = compute_value();
  logic guard;
  always_comb begin
    y = '0;
    {direct_assignment}
    {narrow_guard}
    y[OUT_W-1] = guard;
  end"""
    negative = baseline.replace("right_wide =", "right_wide = ~", 1)
    return {
        "v0": _source(case_id, "v0", width, baseline),
        "v1": _source(case_id, "v1", width, minimal_body),
        "v2": _source(case_id, "v2", width, direct),
        "v3": _source(case_id, "v3", width, function_variant),
        "n0": _source(case_id, "n0", width, negative),
    }


def _popcount_sum(indices: list[int]) -> str:
    return " + ".join(f"{{{{(OUT_W-1){{1'b0}}}}, in0[{index}]}}" for index in indices)


def _popcount(
    case_id: str, width: int, topology: dict[str, Any]
) -> dict[str, str]:
    use = str(topology["use"])
    chunk = int(topology["chunk_size"])
    terms = [f"{{{{(OUT_W-1){{1'b0}}}}, in0[{index}]}}" for index in range(width)]
    serial = " + ".join(terms)
    balanced = _balanced_sum(terms)
    groups = [_balanced_sum(terms[index:index + chunk]) for index in range(0, width, chunk)]
    chunked = _balanced_sum(groups)
    paired_groups = [_balanced_sum(terms[index:index + 2]) for index in range(0, width, 2)]
    paired = _balanced_sum(paired_groups)

    def body(expression: str) -> str:
        if use == "exact":
            output = "y = count;"
        elif use == "threshold":
            output = f"y = (count >= {max(1, width // 2)});"
        else:
            output = "y = (count > 7) ? 7 : count;"
        return f"""  logic [OUT_W-1:0] count;
  always_comb begin
    count = {expression};
    {output}
  end"""
    negative = body(" + ".join(terms[:-1]))
    return {
        "v0": _source(case_id, "v0", width, body(serial)),
        "v1": _source(case_id, "v1", width, body(balanced)),
        "v2": _source(case_id, "v2", width, body(chunked)),
        "v3": _source(case_id, "v3", width, body(paired)),
        "n0": _source(case_id, "n0", width, negative),
    }


_RENDERERS: dict[str, Callable[[str, int, dict[str, Any]], dict[str, str]]] = {
    RESOURCE_SHARING_FAMILY: _resource_sharing,
    ADDER_ASSOCIATION_FAMILY: _adder_association,
    PRIORITY_SELECTION_FAMILY: _priority,
    MUX_PLACEMENT_FAMILY: _mux_placement,
    DECODE_FACTORING_FAMILY: _decode,
    COMPARATOR_SELECTION_FAMILY: _comparator,
    VARIABLE_SHIFT_FAMILY: _variable_shift,
    WIDTH_SIGNEDNESS_FAMILY: _width_signedness,
    POPCOUNT_SATURATION_FAMILY: _popcount,
}


def render_topology_variants(
    family: str,
    case_id: str,
    width: int,
    topology: dict[str, Any],
) -> dict[str, str]:
    try:
        renderer = _RENDERERS[family]
    except KeyError as exc:
        raise TopologyRTLError(f"unsupported topology family: {family}") from exc
    rendered = renderer(case_id, width, topology)
    if set(rendered) != {"v0", "v1", "v2", "v3", "n0"}:
        raise TopologyRTLError(f"renderer returned invalid variants for {family}")
    return rendered
