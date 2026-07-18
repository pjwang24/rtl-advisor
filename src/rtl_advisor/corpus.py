from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any


SCHEMA_VERSION = 1
GENERATOR_VERSION = "rtl-advisor-corpus-v2"
DEFAULT_CASE_ID = "dev_rs_0001"
DEFAULT_ADDER_CASE_ID = "dev_aa_0001"
DEFAULT_PRIORITY_CASE_ID = "dev_pr_0001"
DEFAULT_MUX_PLACEMENT_CASE_ID = "dev_mp_0001"
DEFAULT_DECODE_FACTORING_CASE_ID = "dev_df_0001"
DEFAULT_COMPARATOR_SELECTION_CASE_ID = "dev_cs_0001"
DEFAULT_VARIABLE_SHIFT_CASE_ID = "dev_vs_0001"
DEFAULT_WIDTH_SIGNEDNESS_CASE_ID = "dev_ws_0001"
DEFAULT_POPCOUNT_CASE_ID = "dev_pc_0001"
DEFAULT_WIDTH = 16
DEFAULT_SEED = 5601
DEFAULT_HELDOUT_WIDTH = 17
DEFAULT_HELDOUT_SEED = 105601
RESOURCE_SHARING_FAMILY = "arithmetic_resource_sharing"
ADDER_ASSOCIATION_FAMILY = "adder_reduction_association"
PRIORITY_SELECTION_FAMILY = "priority_selection"
MUX_PLACEMENT_FAMILY = "mux_placement"
DECODE_FACTORING_FAMILY = "decode_factoring"
COMPARATOR_SELECTION_FAMILY = "comparator_selection"
VARIABLE_SHIFT_FAMILY = "variable_shift"
WIDTH_SIGNEDNESS_FAMILY = "width_signedness"
POPCOUNT_SATURATION_FAMILY = "popcount_saturation"
SUPPORTED_SUITES = ("development", "heldout")
_CASE_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


class CorpusError(ValueError):
    """Raised when a generated corpus case is invalid or unsafe to use."""


@dataclass(frozen=True)
class VariantSpec:
    variant_id: str
    role: str
    file: str
    kernel_top: str
    wrapper_top: str
    expected_equivalent: bool
    sha256: str


@dataclass(frozen=True)
class CaseManifest:
    path: Path
    case_id: str
    family: str
    width: int
    seed: int
    baseline_id: str
    variants: tuple[VariantSpec, ...]

    @property
    def root(self) -> Path:
        return self.path.parent

    @property
    def baseline(self) -> VariantSpec:
        return self.variant(self.baseline_id)

    def variant(self, variant_id: str) -> VariantSpec:
        for variant in self.variants:
            if variant.variant_id == variant_id:
                return variant
        raise CorpusError(f"unknown variant {variant_id!r} in case {self.case_id}")

    def variant_path(self, variant: VariantSpec) -> Path:
        path = (self.root / variant.file).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise CorpusError(f"variant path escapes case directory: {variant.file}")
        return path


@dataclass(frozen=True)
class FamilyDefinition:
    family_id: str
    short_code: str
    default_development_case_id: str
    variant_roles: dict[str, str]
    expected_equivalence: dict[str, bool]
    render_variants: Callable[[str, int, int], dict[str, str]]


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _kernel(prefix: str, variant_id: str, width: int, body: str) -> str:
    return f"""module {prefix}_{variant_id}_kernel #(
  parameter integer WIDTH = {width}
) (
  input  logic [WIDTH-1:0] a,
  input  logic [WIDTH-1:0] b,
  input  logic [WIDTH-1:0] c,
  input  logic [WIDTH-1:0] d,
  input  logic             sel,
  output logic [WIDTH:0]   y
);
{body}
endmodule

module {prefix}_{variant_id}_top #(
  parameter integer WIDTH = {width}
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic [WIDTH-1:0] a,
  input  logic [WIDTH-1:0] b,
  input  logic [WIDTH-1:0] c,
  input  logic [WIDTH-1:0] d,
  input  logic             sel,
  output logic [WIDTH:0]   y
);
  logic [WIDTH:0] y_next;

  {prefix}_{variant_id}_kernel #(.WIDTH(WIDTH)) kernel (
    .a(a),
    .b(b),
    .c(c),
    .d(d),
    .sel(sel),
    .y(y_next)
  );

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      y <= '0;
    end else begin
      y <= y_next;
    end
  end
endmodule
"""


def _render_resource_sharing_variants(
    case_id: str,
    width: int,
    seed: int,
) -> dict[str, str]:
    prefix = case_id.replace("-", "_")
    baseline_body = """  logic [WIDTH:0] sum_ab;
  logic [WIDTH:0] sum_cd;

  always_comb begin
    sum_ab = {1'b0, a} + {1'b0, b};
    sum_cd = {1'b0, c} + {1'b0, d};
    y = sel ? sum_ab : sum_cd;
  end"""
    shared_body = """  logic [WIDTH-1:0] lhs;
  logic [WIDTH-1:0] rhs;

  always_comb begin
    lhs = sel ? a : c;
    rhs = sel ? b : d;
    y = {1'b0, lhs} + {1'b0, rhs};
  end"""
    branch_shared_body = """  logic [WIDTH-1:0] lhs;
  logic [WIDTH-1:0] rhs;

  always_comb begin
    if (sel) begin
      lhs = a;
      rhs = b;
    end else begin
      lhs = c;
      rhs = d;
    end
    y = {1'b0, lhs} + {1'b0, rhs};
  end"""
    case_shared_body = """  logic [WIDTH-1:0] lhs;
  logic [WIDTH-1:0] rhs;

  always_comb begin
    case (sel)
      1'b1: begin
        lhs = a;
        rhs = b;
      end
      default: begin
        lhs = c;
        rhs = d;
      end
    endcase
    y = {1'b0, lhs} + {1'b0, rhs};
  end"""
    broken_rhs = "d : b" if seed % 2 else "c : a"
    broken_body = f"""  logic [WIDTH-1:0] lhs;
  logic [WIDTH-1:0] rhs;

  always_comb begin
    lhs = sel ? a : c;
    rhs = sel ? {broken_rhs};
    y = {{1'b0, lhs}} + {{1'b0, rhs}};
  end"""
    return {
        "v0": _kernel(prefix, "v0", width, baseline_body),
        "v1": _kernel(prefix, "v1", width, shared_body),
        "v2": _kernel(prefix, "v2", width, branch_shared_body),
        "v3": _kernel(prefix, "v3", width, case_shared_body),
        "n0": _kernel(prefix, "n0", width, broken_body),
    }


def _adder_kernel(prefix: str, variant_id: str, width: int, body: str) -> str:
    return f"""module {prefix}_{variant_id}_kernel #(
  parameter integer WIDTH = {width}
) (
  input  logic [WIDTH-1:0] a,
  input  logic [WIDTH-1:0] b,
  input  logic [WIDTH-1:0] c,
  input  logic [WIDTH-1:0] d,
  output logic [WIDTH+1:0] y
);
{body}
endmodule

module {prefix}_{variant_id}_top #(
  parameter integer WIDTH = {width}
) (
  input  logic               clk,
  input  logic               rst_n,
  input  logic [WIDTH-1:0]   a,
  input  logic [WIDTH-1:0]   b,
  input  logic [WIDTH-1:0]   c,
  input  logic [WIDTH-1:0]   d,
  output logic [WIDTH+1:0]   y
);
  logic [WIDTH+1:0] y_next;

  {prefix}_{variant_id}_kernel #(.WIDTH(WIDTH)) kernel (
    .a(a),
    .b(b),
    .c(c),
    .d(d),
    .y(y_next)
  );

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      y <= '0;
    end else begin
      y <= y_next;
    end
  end
endmodule
"""


def _render_adder_association_variants(
    case_id: str,
    width: int,
    seed: int,
) -> dict[str, str]:
    prefix = case_id.replace("-", "_")
    serial_body = """  logic [WIDTH+1:0] sum_ab;
  logic [WIDTH+1:0] sum_abc;

  always_comb begin
    sum_ab = {2'b0, a} + {2'b0, b};
    sum_abc = sum_ab + {2'b0, c};
    y = sum_abc + {2'b0, d};
  end"""
    balanced_ab_cd = """  logic [WIDTH+1:0] sum_ab;
  logic [WIDTH+1:0] sum_cd;

  always_comb begin
    sum_ab = {2'b0, a} + {2'b0, b};
    sum_cd = {2'b0, c} + {2'b0, d};
    y = sum_ab + sum_cd;
  end"""
    balanced_ac_bd = """  logic [WIDTH+1:0] sum_ac;
  logic [WIDTH+1:0] sum_bd;

  always_comb begin
    sum_ac = {2'b0, a} + {2'b0, c};
    sum_bd = {2'b0, b} + {2'b0, d};
    y = sum_ac + sum_bd;
  end"""
    balanced_ad_bc = """  logic [WIDTH+1:0] sum_ad;
  logic [WIDTH+1:0] sum_bc;

  always_comb begin
    sum_ad = {2'b0, a} + {2'b0, d};
    sum_bc = {2'b0, b} + {2'b0, c};
    y = sum_ad + sum_bc;
  end"""
    wrong_pair = ("c", "c") if seed % 2 else ("d", "d")
    negative_body = f"""  logic [WIDTH+1:0] sum_ab;
  logic [WIDTH+1:0] sum_wrong;

  always_comb begin
    sum_ab = {{2'b0, a}} + {{2'b0, b}};
    sum_wrong = {{2'b0, {wrong_pair[0]}}} + {{2'b0, {wrong_pair[1]}}};
    y = sum_ab + sum_wrong;
  end"""
    bodies = {
        "v0": serial_body,
        "v1": balanced_ab_cd,
        "v2": balanced_ac_bd,
        "v3": balanced_ad_bc,
        "n0": negative_body,
    }
    return {
        variant_id: _adder_kernel(prefix, variant_id, width, body)
        for variant_id, body in bodies.items()
    }


def _priority_kernel(prefix: str, variant_id: str, width: int, body: str) -> str:
    return f"""module {prefix}_{variant_id}_kernel #(
  parameter integer WIDTH = {width}
) (
  input  logic [3:0]       req,
  input  logic [WIDTH-1:0] a,
  input  logic [WIDTH-1:0] b,
  input  logic [WIDTH-1:0] c,
  input  logic [WIDTH-1:0] d,
  output logic [WIDTH-1:0] y,
  output logic             valid
);
{body}
endmodule

module {prefix}_{variant_id}_top #(
  parameter integer WIDTH = {width}
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic [3:0]       req,
  input  logic [WIDTH-1:0] a,
  input  logic [WIDTH-1:0] b,
  input  logic [WIDTH-1:0] c,
  input  logic [WIDTH-1:0] d,
  output logic [WIDTH-1:0] y,
  output logic             valid
);
  logic [WIDTH-1:0] y_next;
  logic             valid_next;

  {prefix}_{variant_id}_kernel #(.WIDTH(WIDTH)) kernel (
    .req(req),
    .a(a),
    .b(b),
    .c(c),
    .d(d),
    .y(y_next),
    .valid(valid_next)
  );

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      y <= '0;
      valid <= 1'b0;
    end else begin
      y <= y_next;
      valid <= valid_next;
    end
  end
endmodule
"""


def _render_priority_selection_variants(
    case_id: str,
    width: int,
    seed: int,
) -> dict[str, str]:
    prefix = case_id.replace("-", "_")
    priority_chain = """  always_comb begin
    valid = 1'b1;
    if (req[0]) begin
      y = a;
    end else if (req[1]) begin
      y = b;
    end else if (req[2]) begin
      y = c;
    end else if (req[3]) begin
      y = d;
    end else begin
      y = '0;
      valid = 1'b0;
    end
  end"""
    case_selection = """  always_comb begin
    valid = 1'b1;
    casez (req)
      4'b???1: y = a;
      4'b??10: y = b;
      4'b?100: y = c;
      4'b1000: y = d;
      default: begin
        y = '0;
        valid = 1'b0;
      end
    endcase
  end"""
    nested_selection = """  always_comb begin
    valid = |req;
    y = req[0] ? a :
        req[1] ? b :
        req[2] ? c :
        req[3] ? d : '0;
  end"""
    decoded_selection = """  logic [3:0] grant;

  always_comb begin
    grant[0] = req[0];
    grant[1] = ~req[0] & req[1];
    grant[2] = ~req[0] & ~req[1] & req[2];
    grant[3] = ~req[0] & ~req[1] & ~req[2] & req[3];
    y = ({WIDTH{grant[0]}} & a) |
        ({WIDTH{grant[1]}} & b) |
        ({WIDTH{grant[2]}} & c) |
        ({WIDTH{grant[3]}} & d);
    valid = |grant;
  end"""
    if seed % 2:
        negative_selection = """  always_comb begin
    valid = 1'b1;
    if (req[3]) begin
      y = d;
    end else if (req[2]) begin
      y = c;
    end else if (req[1]) begin
      y = b;
    end else if (req[0]) begin
      y = a;
    end else begin
      y = '0;
      valid = 1'b0;
    end
  end"""
    else:
        negative_selection = """  always_comb begin
    valid = 1'b1;
    if (req[0]) begin
      y = a;
    end else if (req[2]) begin
      y = c;
    end else if (req[1]) begin
      y = b;
    end else if (req[3]) begin
      y = d;
    end else begin
      y = '0;
      valid = 1'b0;
    end
  end"""
    bodies = {
        "v0": priority_chain,
        "v1": case_selection,
        "v2": nested_selection,
        "v3": decoded_selection,
        "n0": negative_selection,
    }
    return {
        variant_id: _priority_kernel(prefix, variant_id, width, body)
        for variant_id, body in bodies.items()
    }


def _mux_placement_kernel(
    prefix: str,
    variant_id: str,
    width: int,
    body: str,
) -> str:
    return f"""module {prefix}_{variant_id}_kernel #(
  parameter integer WIDTH = {width}
) (
  input  logic [WIDTH-1:0] a,
  input  logic [WIDTH-1:0] b,
  input  logic [WIDTH-1:0] c,
  input  logic             sel,
  output logic [WIDTH:0]   y
);
{body}
endmodule

module {prefix}_{variant_id}_top #(
  parameter integer WIDTH = {width}
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic [WIDTH-1:0] a,
  input  logic [WIDTH-1:0] b,
  input  logic [WIDTH-1:0] c,
  input  logic             sel,
  output logic [WIDTH:0]   y
);
  logic [WIDTH:0] y_next;

  {prefix}_{variant_id}_kernel #(.WIDTH(WIDTH)) kernel (
    .a(a),
    .b(b),
    .c(c),
    .sel(sel),
    .y(y_next)
  );

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      y <= '0;
    end else begin
      y <= y_next;
    end
  end
endmodule
"""


def _render_mux_placement_variants(
    case_id: str,
    width: int,
    seed: int,
) -> dict[str, str]:
    prefix = case_id.replace("-", "_")
    before_operation = """  logic [WIDTH-1:0] selected;

  always_comb begin
    selected = sel ? a : b;
    y = {1'b0, selected} + {1'b0, c};
  end"""
    parallel_results = """  logic [WIDTH:0] sum_ac;
  logic [WIDTH:0] sum_bc;

  always_comb begin
    sum_ac = {1'b0, a} + {1'b0, c};
    sum_bc = {1'b0, b} + {1'b0, c};
    y = sel ? sum_ac : sum_bc;
  end"""
    branch_selection = """  logic [WIDTH-1:0] selected;

  always_comb begin
    if (sel) begin
      selected = a;
    end else begin
      selected = b;
    end
    y = {1'b0, selected} + {1'b0, c};
  end"""
    case_selection = """  logic [WIDTH-1:0] selected;

  always_comb begin
    case (sel)
      1'b1: selected = a;
      default: selected = b;
    endcase
    y = {1'b0, selected} + {1'b0, c};
  end"""
    wrong_selection = "b : a" if seed % 2 else "a : c"
    negative_body = f"""  logic [WIDTH-1:0] selected;

  always_comb begin
    selected = sel ? {wrong_selection};
    y = {{1'b0, selected}} + {{1'b0, c}};
  end"""
    bodies = {
        "v0": parallel_results,
        "v1": before_operation,
        "v2": branch_selection,
        "v3": case_selection,
        "n0": negative_body,
    }
    return {
        variant_id: _mux_placement_kernel(prefix, variant_id, width, body)
        for variant_id, body in bodies.items()
    }


def _decode_factoring_kernel(
    prefix: str,
    variant_id: str,
    width: int,
    body: str,
) -> str:
    return f"""module {prefix}_{variant_id}_kernel #(
  parameter integer WIDTH = {width}
) (
  input  logic [3:0]       opcode,
  input  logic [WIDTH-1:0] a,
  input  logic [WIDTH-1:0] b,
  input  logic [WIDTH-1:0] c,
  output logic [WIDTH-1:0] y,
  output logic             hit
);
{body}
endmodule

module {prefix}_{variant_id}_top #(
  parameter integer WIDTH = {width}
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic [3:0]       opcode,
  input  logic [WIDTH-1:0] a,
  input  logic [WIDTH-1:0] b,
  input  logic [WIDTH-1:0] c,
  output logic [WIDTH-1:0] y,
  output logic             hit
);
  logic [WIDTH-1:0] y_next;
  logic             hit_next;

  {prefix}_{variant_id}_kernel #(.WIDTH(WIDTH)) kernel (
    .opcode(opcode),
    .a(a),
    .b(b),
    .c(c),
    .y(y_next),
    .hit(hit_next)
  );

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      y <= '0;
      hit <= 1'b0;
    end else begin
      y <= y_next;
      hit <= hit_next;
    end
  end
endmodule
"""


def _render_decode_factoring_variants(
    case_id: str,
    width: int,
    seed: int,
) -> dict[str, str]:
    prefix = case_id.replace("-", "_")
    repeated_decode = """  always_comb begin
    if (opcode == 4'h3) begin
      y = a;
    end else if (opcode == 4'h9) begin
      y = b;
    end else begin
      y = c;
    end
    hit = (opcode == 4'h3) || (opcode == 4'h9);
  end"""
    shared_decode = """  logic is_a;
  logic is_b;

  always_comb begin
    is_a = opcode == 4'h3;
    is_b = opcode == 4'h9;
    if (is_a) begin
      y = a;
    end else if (is_b) begin
      y = b;
    end else begin
      y = c;
    end
    hit = is_a || is_b;
  end"""
    case_decode = """  always_comb begin
    hit = 1'b1;
    case (opcode)
      4'h3: y = a;
      4'h9: y = b;
      default: begin
        y = c;
        hit = 1'b0;
      end
    endcase
  end"""
    masked_decode = """  logic is_a;
  logic is_b;
  logic use_default;

  always_comb begin
    is_a = opcode == 4'h3;
    is_b = opcode == 4'h9;
    use_default = ~is_a & ~is_b;
    y = ({WIDTH{is_a}} & a) |
        ({WIDTH{is_b}} & b) |
        ({WIDTH{use_default}} & c);
    hit = is_a | is_b;
  end"""
    wrong_code = "4'h8" if seed % 2 else "4'ha"
    negative_decode = f"""  always_comb begin
    if (opcode == 4'h3) begin
      y = a;
    end else if (opcode == {wrong_code}) begin
      y = b;
    end else begin
      y = c;
    end
    hit = (opcode == 4'h3) || (opcode == {wrong_code});
  end"""
    bodies = {
        "v0": repeated_decode,
        "v1": shared_decode,
        "v2": case_decode,
        "v3": masked_decode,
        "n0": negative_decode,
    }
    return {
        variant_id: _decode_factoring_kernel(prefix, variant_id, width, body)
        for variant_id, body in bodies.items()
    }


def _comparator_selection_kernel(
    prefix: str,
    variant_id: str,
    width: int,
    body: str,
) -> str:
    return f"""module {prefix}_{variant_id}_kernel #(
  parameter integer WIDTH = {width}
) (
  input  logic [WIDTH-1:0] a,
  input  logic [WIDTH-1:0] b,
  input  logic [WIDTH-1:0] c,
  input  logic [WIDTH-1:0] d,
  input  logic             sel,
  output logic             y
);
{body}
endmodule

module {prefix}_{variant_id}_top #(
  parameter integer WIDTH = {width}
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic [WIDTH-1:0] a,
  input  logic [WIDTH-1:0] b,
  input  logic [WIDTH-1:0] c,
  input  logic [WIDTH-1:0] d,
  input  logic             sel,
  output logic             y
);
  logic y_next;

  {prefix}_{variant_id}_kernel #(.WIDTH(WIDTH)) kernel (
    .a(a),
    .b(b),
    .c(c),
    .d(d),
    .sel(sel),
    .y(y_next)
  );

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      y <= 1'b0;
    end else begin
      y <= y_next;
    end
  end
endmodule
"""


def _render_comparator_selection_variants(
    case_id: str,
    width: int,
    seed: int,
) -> dict[str, str]:
    prefix = case_id.replace("-", "_")
    parallel_comparators = """  logic less_ab;
  logic less_cd;

  always_comb begin
    less_ab = a < b;
    less_cd = c < d;
    y = sel ? less_ab : less_cd;
  end"""
    selected_operands = """  logic [WIDTH-1:0] lhs;
  logic [WIDTH-1:0] rhs;

  always_comb begin
    lhs = sel ? a : c;
    rhs = sel ? b : d;
    y = lhs < rhs;
  end"""
    branch_operands = """  logic [WIDTH-1:0] lhs;
  logic [WIDTH-1:0] rhs;

  always_comb begin
    if (sel) begin
      lhs = a;
      rhs = b;
    end else begin
      lhs = c;
      rhs = d;
    end
    y = lhs < rhs;
  end"""
    packed_operands = """  logic [2*WIDTH-1:0] selected_pair;

  always_comb begin
    selected_pair = sel ? {a, b} : {c, d};
    y = selected_pair[2*WIDTH-1:WIDTH] < selected_pair[WIDTH-1:0];
  end"""
    if seed % 2:
        negative_comparison = """  logic less_ab;
  logic less_cd;

  always_comb begin
    less_ab = a < b;
    less_cd = c > d;
    y = sel ? less_ab : less_cd;
  end"""
    else:
        negative_comparison = """  logic less_ab;
  logic less_cd;

  always_comb begin
    less_ab = a > b;
    less_cd = c < d;
    y = sel ? less_ab : less_cd;
  end"""
    bodies = {
        "v0": parallel_comparators,
        "v1": selected_operands,
        "v2": branch_operands,
        "v3": packed_operands,
        "n0": negative_comparison,
    }
    return {
        variant_id: _comparator_selection_kernel(prefix, variant_id, width, body)
        for variant_id, body in bodies.items()
    }


def _variable_shift_kernel(
    prefix: str,
    variant_id: str,
    width: int,
    body: str,
) -> str:
    return f"""module {prefix}_{variant_id}_kernel #(
  parameter integer WIDTH = {width}
) (
  input  logic [WIDTH-1:0] data,
  input  logic [WIDTH-1:0] shift_amount,
  output logic [WIDTH-1:0] y
);
{body}
endmodule

module {prefix}_{variant_id}_top #(
  parameter integer WIDTH = {width}
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic [WIDTH-1:0] data,
  input  logic [WIDTH-1:0] shift_amount,
  output logic [WIDTH-1:0] y
);
  logic [WIDTH-1:0] y_next;

  {prefix}_{variant_id}_kernel #(.WIDTH(WIDTH)) kernel (
    .data(data),
    .shift_amount(shift_amount),
    .y(y_next)
  );

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      y <= '0;
    end else begin
      y <= y_next;
    end
  end
endmodule
"""


def _render_variable_shift_variants(
    case_id: str,
    width: int,
    seed: int,
) -> dict[str, str]:
    prefix = case_id.replace("-", "_")
    wide_amount = """  always_comb begin
    y = data << shift_amount;
  end"""
    guarded_amount = """  localparam integer SHIFT_BITS = $clog2(WIDTH);
  logic [SHIFT_BITS-1:0] bounded_amount;

  always_comb begin
    bounded_amount = shift_amount[SHIFT_BITS-1:0];
    if (shift_amount >= WIDTH) begin
      y = '0;
    end else begin
      y = data << bounded_amount;
    end
  end"""
    case_lines = [
        f"      {index}: y = data << {index};"
        for index in range(width)
    ]
    decoded_amount = "\n".join(
        [
            "  always_comb begin",
            "    case (shift_amount)",
            *case_lines,
            "      default: y = '0;",
            "    endcase",
            "  end",
        ]
    )
    staged_amount = """  localparam integer SHIFT_BITS = $clog2(WIDTH);
  logic [WIDTH-1:0] staged;
  integer index;

  always_comb begin
    staged = data;
    for (index = 0; index < SHIFT_BITS; index = index + 1) begin
      if (shift_amount[index]) begin
        staged = staged << (1 << index);
      end
    end
    if (shift_amount >= WIDTH) begin
      y = '0;
    end else begin
      y = staged;
    end
  end"""
    dropped_guard = """  localparam integer SHIFT_BITS = $clog2(WIDTH);
  logic [SHIFT_BITS-1:0] bounded_amount;

  always_comb begin
    bounded_amount = shift_amount[SHIFT_BITS-1:0];
    y = data << bounded_amount;
  end"""
    if not seed % 2:
        dropped_guard = dropped_guard.replace("data <<", "data >>")
    bodies = {
        "v0": wide_amount,
        "v1": guarded_amount,
        "v2": decoded_amount,
        "v3": staged_amount,
        "n0": dropped_guard,
    }
    return {
        variant_id: _variable_shift_kernel(prefix, variant_id, width, body)
        for variant_id, body in bodies.items()
    }


def _width_signedness_kernel(
    prefix: str,
    variant_id: str,
    width: int,
    body: str,
) -> str:
    return f"""module {prefix}_{variant_id}_kernel #(
  parameter integer WIDTH = {width}
) (
  input  logic [WIDTH-1:0] a,
  input  logic [WIDTH-1:0] b,
  output logic             y
);
{body}
endmodule

module {prefix}_{variant_id}_top #(
  parameter integer WIDTH = {width}
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic [WIDTH-1:0] a,
  input  logic [WIDTH-1:0] b,
  output logic             y
);
  logic y_next;

  {prefix}_{variant_id}_kernel #(.WIDTH(WIDTH)) kernel (
    .a(a),
    .b(b),
    .y(y_next)
  );

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      y <= 1'b0;
    end else begin
      y <= y_next;
    end
  end
endmodule
"""


def _render_width_signedness_variants(
    case_id: str,
    width: int,
    seed: int,
) -> dict[str, str]:
    prefix = case_id.replace("-", "_")
    over_wide_signed = """  logic signed [2*WIDTH-1:0] wide_a;
  logic signed [2*WIDTH-1:0] wide_b;

  always_comb begin
    wide_a = {{WIDTH{a[WIDTH-1]}}, a};
    wide_b = {{WIDTH{b[WIDTH-1]}}, b};
    y = wide_a < wide_b;
  end"""
    direct_signed = """  always_comb begin
    y = $signed(a) < $signed(b);
  end"""
    typed_signed = """  logic signed [WIDTH-1:0] signed_a;
  logic signed [WIDTH-1:0] signed_b;

  always_comb begin
    signed_a = a;
    signed_b = b;
    y = signed_a < signed_b;
  end"""
    sign_split = """  always_comb begin
    if (a[WIDTH-1] != b[WIDTH-1]) begin
      y = a[WIDTH-1];
    end else begin
      y = a[WIDTH-2:0] < b[WIDTH-2:0];
    end
  end"""
    negative_comparison = """  always_comb begin
    y = a < b;
  end"""
    if not seed % 2:
        negative_comparison = negative_comparison.replace("a < b", "a > b")
    bodies = {
        "v0": over_wide_signed,
        "v1": direct_signed,
        "v2": typed_signed,
        "v3": sign_split,
        "n0": negative_comparison,
    }
    return {
        variant_id: _width_signedness_kernel(prefix, variant_id, width, body)
        for variant_id, body in bodies.items()
    }


def _popcount_kernel(
    prefix: str,
    variant_id: str,
    width: int,
    body: str,
) -> str:
    return f"""module {prefix}_{variant_id}_kernel #(
  parameter integer WIDTH = {width}
) (
  input  logic [WIDTH-1:0]                 bits,
  output logic [$clog2(WIDTH+1)-1:0]      y
);
  localparam integer COUNT_WIDTH = $clog2(WIDTH+1);
{body}
endmodule

module {prefix}_{variant_id}_top #(
  parameter integer WIDTH = {width}
) (
  input  logic                              clk,
  input  logic                              rst_n,
  input  logic [WIDTH-1:0]                  bits,
  output logic [$clog2(WIDTH+1)-1:0]       y
);
  logic [$clog2(WIDTH+1)-1:0] y_next;

  {prefix}_{variant_id}_kernel #(.WIDTH(WIDTH)) kernel (
    .bits(bits),
    .y(y_next)
  );

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      y <= '0;
    end else begin
      y <= y_next;
    end
  end
endmodule
"""


def _serial_popcount_body(width: int, included: list[int]) -> str:
    accumulation = [
        f"    count = count + bits[{index}];"
        for index in included
    ]
    return "\n".join(
        [
            "  logic [COUNT_WIDTH-1:0] count;",
            "",
            "  always_comb begin",
            "    count = '0;",
            *accumulation,
            "    y = count;",
            "  end",
        ]
    )


def _balanced_popcount_body(width: int) -> str:
    names = [f"level_0_{index}" for index in range(width)]
    declarations = [
        f"  logic [COUNT_WIDTH-1:0] {name};"
        for name in names
    ]
    assignments: list[str] = []
    for index, name in enumerate(names):
        assignments.extend(
            (
                f"    {name} = '0;",
                f"    {name}[0] = bits[{index}];",
            )
        )
    level = 1
    current = names
    while len(current) > 1:
        following = []
        for index in range(0, len(current), 2):
            name = f"level_{level}_{index // 2}"
            following.append(name)
            declarations.append(f"  logic [COUNT_WIDTH-1:0] {name};")
            if index + 1 < len(current):
                assignments.append(
                    f"    {name} = {current[index]} + {current[index + 1]};"
                )
            else:
                assignments.append(f"    {name} = {current[index]};")
        current = following
        level += 1
    return "\n".join(
        [
            *declarations,
            "",
            "  always_comb begin",
            *assignments,
            f"    y = {current[0]};",
            "  end",
        ]
    )


def _chunked_popcount_body(width: int) -> str:
    chunks = [list(range(start, min(start + 4, width))) for start in range(0, width, 4)]
    declarations = [
        f"  logic [COUNT_WIDTH-1:0] chunk_{index};"
        for index in range(len(chunks))
    ]
    assignments = []
    for chunk_index, bit_indices in enumerate(chunks):
        assignments.append(f"    chunk_{chunk_index} = '0;")
        assignments.extend(
            f"    chunk_{chunk_index} = chunk_{chunk_index} + bits[{bit_index}];"
            for bit_index in bit_indices
        )
    total = " + ".join(f"chunk_{index}" for index in range(len(chunks)))
    return "\n".join(
        [
            *declarations,
            "",
            "  always_comb begin",
            *assignments,
            f"    y = {total};",
            "  end",
        ]
    )


def _paired_popcount_body(width: int) -> str:
    pairs = [list(range(start, min(start + 2, width))) for start in range(0, width, 2)]
    declarations = [
        f"  logic [COUNT_WIDTH-1:0] pair_{index};"
        for index in range(len(pairs))
    ]
    assignments = []
    for pair_index, bit_indices in enumerate(pairs):
        assignments.append(f"    pair_{pair_index} = '0;")
        assignments.extend(
            f"    pair_{pair_index} = pair_{pair_index} + bits[{bit_index}];"
            for bit_index in bit_indices
        )
    total = " + ".join(f"pair_{index}" for index in range(len(pairs)))
    return "\n".join(
        [
            *declarations,
            "",
            "  always_comb begin",
            *assignments,
            f"    y = {total};",
            "  end",
        ]
    )


def _render_popcount_saturation_variants(
    case_id: str,
    width: int,
    seed: int,
) -> dict[str, str]:
    prefix = case_id.replace("-", "_")
    omitted = seed % width
    bodies = {
        "v0": _serial_popcount_body(width, list(range(width))),
        "v1": _balanced_popcount_body(width),
        "v2": _chunked_popcount_body(width),
        "v3": _paired_popcount_body(width),
        "n0": _serial_popcount_body(
            width,
            [index for index in range(width) if index != omitted],
        ),
    }
    return {
        variant_id: _popcount_kernel(prefix, variant_id, width, body)
        for variant_id, body in bodies.items()
    }


FAMILY_REGISTRY: dict[str, FamilyDefinition] = {
    RESOURCE_SHARING_FAMILY: FamilyDefinition(
        family_id=RESOURCE_SHARING_FAMILY,
        short_code="rs",
        default_development_case_id=DEFAULT_CASE_ID,
        variant_roles={
            "v0": "baseline",
            "v1": "candidate",
            "v2": "candidate",
            "v3": "candidate",
            "n0": "negative_control",
        },
        expected_equivalence={
            "v0": True,
            "v1": True,
            "v2": True,
            "v3": True,
            "n0": False,
        },
        render_variants=_render_resource_sharing_variants,
    ),
    ADDER_ASSOCIATION_FAMILY: FamilyDefinition(
        family_id=ADDER_ASSOCIATION_FAMILY,
        short_code="aa",
        default_development_case_id=DEFAULT_ADDER_CASE_ID,
        variant_roles={
            "v0": "baseline",
            "v1": "candidate",
            "v2": "candidate",
            "v3": "candidate",
            "n0": "negative_control",
        },
        expected_equivalence={
            "v0": True,
            "v1": True,
            "v2": True,
            "v3": True,
            "n0": False,
        },
        render_variants=_render_adder_association_variants,
    ),
    PRIORITY_SELECTION_FAMILY: FamilyDefinition(
        family_id=PRIORITY_SELECTION_FAMILY,
        short_code="pr",
        default_development_case_id=DEFAULT_PRIORITY_CASE_ID,
        variant_roles={
            "v0": "baseline",
            "v1": "candidate",
            "v2": "candidate",
            "v3": "candidate",
            "n0": "negative_control",
        },
        expected_equivalence={
            "v0": True,
            "v1": True,
            "v2": True,
            "v3": True,
            "n0": False,
        },
        render_variants=_render_priority_selection_variants,
    ),
    MUX_PLACEMENT_FAMILY: FamilyDefinition(
        family_id=MUX_PLACEMENT_FAMILY,
        short_code="mp",
        default_development_case_id=DEFAULT_MUX_PLACEMENT_CASE_ID,
        variant_roles={
            "v0": "baseline",
            "v1": "candidate",
            "v2": "candidate",
            "v3": "candidate",
            "n0": "negative_control",
        },
        expected_equivalence={
            "v0": True,
            "v1": True,
            "v2": True,
            "v3": True,
            "n0": False,
        },
        render_variants=_render_mux_placement_variants,
    ),
    DECODE_FACTORING_FAMILY: FamilyDefinition(
        family_id=DECODE_FACTORING_FAMILY,
        short_code="df",
        default_development_case_id=DEFAULT_DECODE_FACTORING_CASE_ID,
        variant_roles={
            "v0": "baseline",
            "v1": "candidate",
            "v2": "candidate",
            "v3": "candidate",
            "n0": "negative_control",
        },
        expected_equivalence={
            "v0": True,
            "v1": True,
            "v2": True,
            "v3": True,
            "n0": False,
        },
        render_variants=_render_decode_factoring_variants,
    ),
    COMPARATOR_SELECTION_FAMILY: FamilyDefinition(
        family_id=COMPARATOR_SELECTION_FAMILY,
        short_code="cs",
        default_development_case_id=DEFAULT_COMPARATOR_SELECTION_CASE_ID,
        variant_roles={
            "v0": "baseline",
            "v1": "candidate",
            "v2": "candidate",
            "v3": "candidate",
            "n0": "negative_control",
        },
        expected_equivalence={
            "v0": True,
            "v1": True,
            "v2": True,
            "v3": True,
            "n0": False,
        },
        render_variants=_render_comparator_selection_variants,
    ),
    VARIABLE_SHIFT_FAMILY: FamilyDefinition(
        family_id=VARIABLE_SHIFT_FAMILY,
        short_code="vs",
        default_development_case_id=DEFAULT_VARIABLE_SHIFT_CASE_ID,
        variant_roles={
            "v0": "baseline",
            "v1": "candidate",
            "v2": "candidate",
            "v3": "candidate",
            "n0": "negative_control",
        },
        expected_equivalence={
            "v0": True,
            "v1": True,
            "v2": True,
            "v3": True,
            "n0": False,
        },
        render_variants=_render_variable_shift_variants,
    ),
    WIDTH_SIGNEDNESS_FAMILY: FamilyDefinition(
        family_id=WIDTH_SIGNEDNESS_FAMILY,
        short_code="ws",
        default_development_case_id=DEFAULT_WIDTH_SIGNEDNESS_CASE_ID,
        variant_roles={
            "v0": "baseline",
            "v1": "candidate",
            "v2": "candidate",
            "v3": "candidate",
            "n0": "negative_control",
        },
        expected_equivalence={
            "v0": True,
            "v1": True,
            "v2": True,
            "v3": True,
            "n0": False,
        },
        render_variants=_render_width_signedness_variants,
    ),
    POPCOUNT_SATURATION_FAMILY: FamilyDefinition(
        family_id=POPCOUNT_SATURATION_FAMILY,
        short_code="pc",
        default_development_case_id=DEFAULT_POPCOUNT_CASE_ID,
        variant_roles={
            "v0": "baseline",
            "v1": "candidate",
            "v2": "candidate",
            "v3": "candidate",
            "n0": "negative_control",
        },
        expected_equivalence={
            "v0": True,
            "v1": True,
            "v2": True,
            "v3": True,
            "n0": False,
        },
        render_variants=_render_popcount_saturation_variants,
    ),
}


def available_families() -> tuple[str, ...]:
    return tuple(FAMILY_REGISTRY)


def default_case_id(
    family: str,
    suite: str,
    *,
    width: int,
    seed: int,
) -> str:
    try:
        definition = FAMILY_REGISTRY[family]
    except KeyError as exc:
        raise CorpusError(f"unknown corpus family: {family}") from exc
    if suite not in SUPPORTED_SUITES:
        raise CorpusError(f"unsupported corpus suite: {suite}")
    if suite == "development":
        return definition.default_development_case_id
    opaque_key = _sha256_text(
        json.dumps(
            {
                "generator": GENERATOR_VERSION,
                "family": family,
                "width": width,
                "seed": seed,
            },
            sort_keys=True,
        )
    )[:12]
    return f"h_{opaque_key}"


def default_suite_parameters(suite: str) -> tuple[int, int]:
    if suite == "development":
        return DEFAULT_WIDTH, DEFAULT_SEED
    if suite == "heldout":
        return DEFAULT_HELDOUT_WIDTH, DEFAULT_HELDOUT_SEED
    raise CorpusError(f"unsupported corpus suite: {suite}")


def _manifest_payload(
    *,
    family: str,
    definition: FamilyDefinition,
    case_id: str,
    width: int,
    seed: int,
    rendered: dict[str, str],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prefix = case_id.replace("-", "_")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "family": family,
        "width": width,
        "seed": seed,
        "baseline_id": "v0",
        "variants": [
            {
                "id": variant_id,
                "role": definition.variant_roles[variant_id],
                "file": f"rtl/{variant_id}.sv",
                "kernel_top": f"{prefix}_{variant_id}_kernel",
                "wrapper_top": f"{prefix}_{variant_id}_top",
                "expected_equivalent": definition.expected_equivalence[variant_id],
                "sha256": _sha256_text(rendered[variant_id]),
            }
            for variant_id in rendered
        ],
    }
    if metadata is not None:
        payload["metadata"] = metadata
    return payload


def _render_family(
    family: str,
    case_id: str,
    width: int,
    seed: int,
) -> dict[str, str]:
    try:
        renderer = FAMILY_REGISTRY[family].render_variants
    except KeyError as exc:
        raise CorpusError(f"unknown corpus family: {family}") from exc
    return renderer(case_id, width, seed)


def generate_case(
    output_dir: Path,
    *,
    family: str = RESOURCE_SHARING_FAMILY,
    suite: str = "development",
    case_id: str | None = None,
    width: int | None = None,
    seed: int | None = None,
    metadata: dict[str, Any] | None = None,
    rendered_override: dict[str, str] | None = None,
    force: bool = False,
) -> Path:
    try:
        definition = FAMILY_REGISTRY[family]
    except KeyError as exc:
        raise CorpusError(f"unknown corpus family: {family}") from exc
    default_width, default_seed = default_suite_parameters(suite)
    resolved_width = width if width is not None else default_width
    resolved_seed = seed if seed is not None else default_seed
    resolved_case_id = case_id or default_case_id(
        family,
        suite,
        width=resolved_width,
        seed=resolved_seed,
    )
    if suite not in SUPPORTED_SUITES:
        raise CorpusError(f"unsupported corpus suite: {suite}")
    if not _CASE_ID_PATTERN.fullmatch(resolved_case_id):
        raise CorpusError(
            "case_id must begin with a letter and contain only letters, digits, _ or -"
        )
    if resolved_width < 1 or resolved_width > 256:
        raise CorpusError("width must be between 1 and 256 bits")
    if resolved_seed < 0:
        raise CorpusError("seed must be non-negative")

    rendered = (
        rendered_override
        if rendered_override is not None
        else _render_family(
            family,
            resolved_case_id,
            resolved_width,
            resolved_seed,
        )
    )
    if set(rendered) != set(definition.variant_roles):
        raise CorpusError(f"family renderer returned unexpected variants for {family}")
    manifest = _manifest_payload(
        family=family,
        definition=definition,
        case_id=resolved_case_id,
        width=resolved_width,
        seed=resolved_seed,
        rendered=rendered,
        metadata=metadata,
    )
    expected_files = {
        output_dir / "rtl" / f"{variant_id}.sv": content
        for variant_id, content in rendered.items()
    }
    manifest_path = output_dir / "manifest.json"
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    expected_files[manifest_path] = manifest_text

    existing = [path for path in expected_files if path.exists()]
    if existing and not force:
        mismatched = [
            path
            for path, content in expected_files.items()
            if not path.is_file() or path.read_text(encoding="utf-8") != content
        ]
        if mismatched:
            raise CorpusError(
                f"case already exists with different content: {output_dir}; use --force"
            )
        return manifest_path

    for path, content in expected_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return manifest_path


def generate_resource_sharing_case(
    output_dir: Path,
    *,
    case_id: str = DEFAULT_CASE_ID,
    width: int = DEFAULT_WIDTH,
    seed: int = DEFAULT_SEED,
    force: bool = False,
) -> Path:
    return generate_case(
        output_dir,
        family=RESOURCE_SHARING_FAMILY,
        suite="development",
        case_id=case_id,
        width=width,
        seed=seed,
        force=force,
    )


def generate_adder_association_case(
    output_dir: Path,
    *,
    case_id: str = DEFAULT_ADDER_CASE_ID,
    width: int = DEFAULT_WIDTH,
    seed: int = DEFAULT_SEED,
    force: bool = False,
) -> Path:
    return generate_case(
        output_dir,
        family=ADDER_ASSOCIATION_FAMILY,
        suite="development",
        case_id=case_id,
        width=width,
        seed=seed,
        force=force,
    )


def generate_priority_selection_case(
    output_dir: Path,
    *,
    case_id: str = DEFAULT_PRIORITY_CASE_ID,
    width: int = DEFAULT_WIDTH,
    seed: int = DEFAULT_SEED,
    force: bool = False,
) -> Path:
    return generate_case(
        output_dir,
        family=PRIORITY_SELECTION_FAMILY,
        suite="development",
        case_id=case_id,
        width=width,
        seed=seed,
        force=force,
    )


def generate_mux_placement_case(
    output_dir: Path,
    *,
    case_id: str = DEFAULT_MUX_PLACEMENT_CASE_ID,
    width: int = DEFAULT_WIDTH,
    seed: int = DEFAULT_SEED,
    force: bool = False,
) -> Path:
    return generate_case(
        output_dir,
        family=MUX_PLACEMENT_FAMILY,
        suite="development",
        case_id=case_id,
        width=width,
        seed=seed,
        force=force,
    )


def generate_decode_factoring_case(
    output_dir: Path,
    *,
    case_id: str = DEFAULT_DECODE_FACTORING_CASE_ID,
    width: int = DEFAULT_WIDTH,
    seed: int = DEFAULT_SEED,
    force: bool = False,
) -> Path:
    return generate_case(
        output_dir,
        family=DECODE_FACTORING_FAMILY,
        suite="development",
        case_id=case_id,
        width=width,
        seed=seed,
        force=force,
    )


def generate_comparator_selection_case(
    output_dir: Path,
    *,
    case_id: str = DEFAULT_COMPARATOR_SELECTION_CASE_ID,
    width: int = DEFAULT_WIDTH,
    seed: int = DEFAULT_SEED,
    force: bool = False,
) -> Path:
    return generate_case(
        output_dir,
        family=COMPARATOR_SELECTION_FAMILY,
        suite="development",
        case_id=case_id,
        width=width,
        seed=seed,
        force=force,
    )


def generate_variable_shift_case(
    output_dir: Path,
    *,
    case_id: str = DEFAULT_VARIABLE_SHIFT_CASE_ID,
    width: int = DEFAULT_WIDTH,
    seed: int = DEFAULT_SEED,
    force: bool = False,
) -> Path:
    return generate_case(
        output_dir,
        family=VARIABLE_SHIFT_FAMILY,
        suite="development",
        case_id=case_id,
        width=width,
        seed=seed,
        force=force,
    )


def generate_width_signedness_case(
    output_dir: Path,
    *,
    case_id: str = DEFAULT_WIDTH_SIGNEDNESS_CASE_ID,
    width: int = DEFAULT_WIDTH,
    seed: int = DEFAULT_SEED,
    force: bool = False,
) -> Path:
    return generate_case(
        output_dir,
        family=WIDTH_SIGNEDNESS_FAMILY,
        suite="development",
        case_id=case_id,
        width=width,
        seed=seed,
        force=force,
    )


def generate_popcount_saturation_case(
    output_dir: Path,
    *,
    case_id: str = DEFAULT_POPCOUNT_CASE_ID,
    width: int = DEFAULT_WIDTH,
    seed: int = DEFAULT_SEED,
    force: bool = False,
) -> Path:
    return generate_case(
        output_dir,
        family=POPCOUNT_SATURATION_FAMILY,
        suite="development",
        case_id=case_id,
        width=width,
        seed=seed,
        force=force,
    )


def _require_type(value: object, expected: type, field: str) -> Any:
    if not isinstance(value, expected):
        raise CorpusError(f"manifest field {field!r} must be {expected.__name__}")
    return value


def load_manifest(path: str | Path) -> CaseManifest:
    candidate = Path(path).expanduser().resolve()
    manifest_path = candidate / "manifest.json" if candidate.is_dir() else candidate
    if not manifest_path.is_file():
        raise CorpusError(f"case manifest not found: {manifest_path}")

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise CorpusError(f"could not read manifest {manifest_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CorpusError("case manifest root must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise CorpusError(f"unsupported manifest schema: {payload.get('schema_version')}")

    raw_variants = _require_type(payload.get("variants"), list, "variants")
    variants: list[VariantSpec] = []
    for index, raw in enumerate(raw_variants):
        if not isinstance(raw, dict):
            raise CorpusError(f"variant {index} must be an object")
        variants.append(
            VariantSpec(
                variant_id=str(raw["id"]),
                role=str(raw["role"]),
                file=str(raw["file"]),
                kernel_top=str(raw["kernel_top"]),
                wrapper_top=str(raw["wrapper_top"]),
                expected_equivalent=bool(raw["expected_equivalent"]),
                sha256=str(raw["sha256"]),
            )
        )

    manifest = CaseManifest(
        path=manifest_path,
        case_id=str(payload["case_id"]),
        family=str(payload["family"]),
        width=int(payload["width"]),
        seed=int(payload["seed"]),
        baseline_id=str(payload["baseline_id"]),
        variants=tuple(variants),
    )
    if len({variant.variant_id for variant in variants}) != len(variants):
        raise CorpusError("variant IDs must be unique")
    baseline = manifest.baseline
    if baseline.role != "baseline":
        raise CorpusError("baseline_id must reference the baseline variant")

    for variant in variants:
        variant_path = manifest.variant_path(variant)
        if not variant_path.is_file():
            raise CorpusError(f"variant file not found: {variant_path}")
        actual_sha256 = hashlib.sha256(variant_path.read_bytes()).hexdigest()
        if actual_sha256 != variant.sha256:
            raise CorpusError(
                f"variant checksum mismatch for {variant.variant_id}: "
                f"expected {variant.sha256}, got {actual_sha256}"
            )
    return manifest
