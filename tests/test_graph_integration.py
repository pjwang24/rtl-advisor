from pathlib import Path
import shutil

import pytest

from rtl_advisor.config import (
    LibertyConfig,
    ProjectConfig,
    SynthesisConfig,
    ToolConfig,
)
from rtl_advisor.corpus import (
    generate_adder_association_case,
    generate_comparator_selection_case,
    generate_decode_factoring_case,
    generate_mux_placement_case,
    generate_priority_selection_case,
    generate_popcount_saturation_case,
    generate_resource_sharing_case,
    generate_variable_shift_case,
    generate_width_signedness_case,
)
from rtl_advisor.graph import build_graph
from rtl_advisor.rules import analyze_rules


YOSYS = shutil.which("yosys")


def make_config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        config_path=tmp_path / "rtl-advisor.toml",
        root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        corpus_dir=tmp_path / "corpus",
        tools=ToolConfig(
            verilator="verilator",
            yosys=YOSYS or "yosys",
            codex="codex",
            timeout_seconds=30,
        ),
        synthesis=SynthesisConfig(
            driving_cell="BUF_X1",
            output_load_ff=10.0,
        ),
        liberty=LibertyConfig(
            name="unused",
            path=tmp_path / "unused.lib",
            url="unused",
            sha256="0" * 64,
            license_path=tmp_path / "LICENSE",
            license_url="unused",
            source_commit="unused",
        ),
    )


def kernel_module(graph: dict) -> dict:
    return next(module for module in graph["modules"] if not module["is_top"])


@pytest.mark.skipif(YOSYS is None, reason="Yosys is required")
def test_graph_preserves_hierarchy_and_exposes_resource_sharing(tmp_path: Path) -> None:
    manifest_path = generate_resource_sharing_case(
        tmp_path / "corpus/development/dev_rs_graph",
        case_id="dev_rs_graph",
        width=8,
        seed=42,
    )
    config = make_config(tmp_path)

    baseline = build_graph(config, manifest_path, "v0")
    shared = build_graph(config, manifest_path, "v1")
    cached = build_graph(config, manifest_path, "v0")
    rebuilt = build_graph(config, manifest_path, "v0", force=True)

    assert len(baseline.graph["modules"]) == 2
    assert len(baseline.graph["hierarchy"]["instances"]) == 1
    assert baseline.graph["hierarchy"]["top"] == "dev_rs_graph_v0_top"
    assert baseline.graph["hierarchy"]["instances"][0]["parent_module"] == (
        "dev_rs_graph_v0_top"
    )

    baseline_operations = [
        node["operation"] for node in kernel_module(baseline.graph)["nodes"]
    ]
    shared_operations = [
        node["operation"] for node in kernel_module(shared.graph)["nodes"]
    ]
    assert baseline_operations.count("add") == 2
    assert baseline_operations.count("mux") == 1
    assert shared_operations.count("add") == 1
    assert shared_operations.count("mux") == 2

    baseline_analysis = analyze_rules(baseline.graph)
    shared_analysis = analyze_rules(shared.graph)
    assert len(baseline_analysis["findings"]) == 1
    assert baseline_analysis["findings"][0]["rule_id"] == (
        "resource_sharing.output_mux.v1"
    )
    assert baseline_analysis["findings"][0]["evidence"]["operator"] == "add"
    source_location = baseline_analysis["findings"][0]["source"]["locations"][0]
    assert source_location["file"] == "rtl/v0.sv"
    assert source_location["start_line"] == 17
    assert len(shared_analysis["findings"]) == 1
    assert shared_analysis["findings"][0]["rule_id"] == (
        "mux_placement.pre_operation.v1"
    )

    assert cached.cached is True
    assert cached.graph["graph_hash"] == baseline.graph["graph_hash"]
    assert rebuilt.graph["graph_hash"] == baseline.graph["graph_hash"]
    assert baseline.graph_path.is_file()


@pytest.mark.skipif(YOSYS is None, reason="Yosys is required")
def test_serial_adder_rule_distinguishes_balanced_tree(tmp_path: Path) -> None:
    manifest_path = generate_adder_association_case(
        tmp_path / "corpus/development/dev_aa_graph",
        case_id="dev_aa_graph",
        width=8,
        seed=42,
    )
    config = make_config(tmp_path)

    serial = build_graph(config, manifest_path, "v0")
    balanced = build_graph(config, manifest_path, "v1")
    serial_analysis = analyze_rules(serial.graph)
    balanced_analysis = analyze_rules(balanced.graph)

    assert [
        node["operation"] for node in kernel_module(serial.graph)["nodes"]
    ].count("add") == 3
    assert [
        node["operation"] for node in kernel_module(balanced.graph)["nodes"]
    ].count("add") == 3
    assert len(serial_analysis["findings"]) == 1
    finding = serial_analysis["findings"][0]
    assert finding["rule_id"] == "arithmetic.serial_chain.v1"
    assert finding["transformation_id"] == "reassociate_arithmetic_tree"
    assert finding["evidence"]["serial_depth"] == 3
    assert finding["evidence"]["balanced_depth_estimate"] == 2
    assert finding["evidence"]["operand_count_estimate"] == 4
    assert finding["source"]["locations"][0]["start_line"] == 16
    assert balanced_analysis["findings"] == []


@pytest.mark.skipif(YOSYS is None, reason="Yosys is required")
def test_priority_mux_rule_distinguishes_decoded_selection(tmp_path: Path) -> None:
    manifest_path = generate_priority_selection_case(
        tmp_path / "corpus/development/dev_pr_graph",
        case_id="dev_pr_graph",
        width=8,
        seed=41,
    )
    config = make_config(tmp_path)

    chained = build_graph(config, manifest_path, "v0")
    case_encoded = build_graph(config, manifest_path, "v1")
    decoded = build_graph(config, manifest_path, "v3")
    chained_analysis = analyze_rules(chained.graph)
    case_analysis = analyze_rules(case_encoded.graph)
    decoded_analysis = analyze_rules(decoded.graph)

    assert len(chained_analysis["findings"]) == 1
    finding = chained_analysis["findings"][0]
    assert finding["rule_id"] == "priority_selection.mux_depth.v1"
    assert finding["transformation_id"] == "balance_priority_selection"
    assert finding["evidence"]["serial_depth"] >= 4
    assert finding["evidence"]["maximum_width"] == 8
    assert "priority_selection.mux_depth.v1" not in {
        finding["rule_id"] for finding in case_analysis["findings"]
    }
    assert "priority_selection.mux_depth.v1" not in {
        finding["rule_id"] for finding in decoded_analysis["findings"]
    }


@pytest.mark.skipif(YOSYS is None, reason="Yosys is required")
def test_mux_placement_rules_describe_both_tradeoff_directions(
    tmp_path: Path,
) -> None:
    manifest_path = generate_mux_placement_case(
        tmp_path / "corpus/development/dev_mp_graph",
        case_id="dev_mp_graph",
        width=8,
        seed=41,
    )
    config = make_config(tmp_path)

    after_operation = build_graph(config, manifest_path, "v0")
    before_operation = build_graph(config, manifest_path, "v1")
    after_analysis = analyze_rules(after_operation.graph)
    before_analysis = analyze_rules(before_operation.graph)

    assert len(after_analysis["findings"]) == 1
    after_finding = after_analysis["findings"][0]
    assert after_finding["rule_id"] == "mux_placement.post_operation.v1"
    assert after_finding["evidence"]["operator"] == "add"
    assert len(after_finding["evidence"]["common_operands"]) == 1
    assert after_finding["transformation_id"] == "move_mux_across_operation"

    assert len(before_analysis["findings"]) == 1
    before_finding = before_analysis["findings"][0]
    assert before_finding["rule_id"] == "mux_placement.pre_operation.v1"
    assert before_finding["evidence"]["operator"] == "add"
    assert before_finding["evidence"]["selected_input_count"] == 1


@pytest.mark.skipif(YOSYS is None, reason="Yosys is required")
def test_repeated_decode_rule_distinguishes_shared_comparisons(
    tmp_path: Path,
) -> None:
    manifest_path = generate_decode_factoring_case(
        tmp_path / "corpus/development/dev_df_graph",
        case_id="dev_df_graph",
        width=8,
        seed=41,
    )
    config = make_config(tmp_path)

    repeated = build_graph(config, manifest_path, "v0")
    shared = build_graph(config, manifest_path, "v1")
    repeated_analysis = analyze_rules(repeated.graph)
    shared_analysis = analyze_rules(shared.graph)

    assert len(repeated_analysis["findings"]) == 1
    finding = repeated_analysis["findings"][0]
    assert finding["rule_id"] == "decode.repeated_compare.v1"
    assert finding["transformation_id"] == "factor_repeated_decode"
    assert len(finding["evidence"]["duplicate_groups"]) == 2
    assert finding["evidence"]["redundant_node_count"] == 2
    assert finding["source"]["locations"][0]["start_line"] == 19
    assert shared_analysis["findings"] == []


@pytest.mark.skipif(YOSYS is None, reason="Yosys is required")
def test_comparator_selection_rule_distinguishes_shared_comparator(
    tmp_path: Path,
) -> None:
    manifest_path = generate_comparator_selection_case(
        tmp_path / "corpus/development/dev_cs_graph",
        case_id="dev_cs_graph",
        width=8,
        seed=41,
    )
    config = make_config(tmp_path)

    parallel = build_graph(config, manifest_path, "v0")
    shared = build_graph(config, manifest_path, "v1")
    parallel_analysis = analyze_rules(parallel.graph)
    shared_analysis = analyze_rules(shared.graph)

    parallel_operations = [
        node["operation"] for node in kernel_module(parallel.graph)["nodes"]
    ]
    shared_operations = [
        node["operation"] for node in kernel_module(shared.graph)["nodes"]
    ]
    assert parallel_operations.count("lt") == 2
    assert shared_operations.count("lt") == 1
    assert len(parallel_analysis["findings"]) == 1
    finding = parallel_analysis["findings"][0]
    assert finding["rule_id"] == "comparator_selection.output_mux.v1"
    assert finding["transformation_id"] == "factor_comparator_selection"
    assert finding["evidence"]["comparison"] == "lt"
    assert finding["evidence"]["comparator_count"] == 2
    assert finding["evidence"]["operand_widths"] == [8]
    assert finding["predicted_effect"] == {
        "area": "degrade",
        "cell_count": "uncertain",
        "delay": "degrade",
    }
    assert len(shared_analysis["findings"]) == 1
    reverse = shared_analysis["findings"][0]
    assert reverse["rule_id"] == "mux_placement.pre_operation.v1"
    assert reverse["transformation_id"] == "move_mux_across_operation"
    assert reverse["evidence"]["operator"] == "lt"


@pytest.mark.skipif(YOSYS is None, reason="Yosys is required")
def test_variable_shift_rule_distinguishes_bounded_amount(tmp_path: Path) -> None:
    manifest_path = generate_variable_shift_case(
        tmp_path / "corpus/development/dev_vs_graph",
        case_id="dev_vs_graph",
        width=8,
        seed=41,
    )
    config = make_config(tmp_path)

    wide = build_graph(config, manifest_path, "v0")
    bounded = build_graph(config, manifest_path, "v1")
    wide_analysis = analyze_rules(wide.graph)
    bounded_analysis = analyze_rules(bounded.graph)

    assert len(wide_analysis["findings"]) == 1
    finding = wide_analysis["findings"][0]
    assert finding["rule_id"] == "variable_shift.wide_amount.v1"
    assert finding["transformation_id"] == "bound_variable_shift"
    assert finding["evidence"]["operation"] == "shl"
    assert finding["evidence"]["data_width"] == 8
    assert finding["evidence"]["amount_width"] == 8
    assert finding["evidence"]["required_index_bits"] == 3
    assert finding["evidence"]["excess_amount_bits"] == 5
    assert bounded_analysis["findings"] == []


@pytest.mark.skipif(YOSYS is None, reason="Yosys is required")
def test_width_signedness_rule_finds_redundant_sign_extension(
    tmp_path: Path,
) -> None:
    manifest_path = generate_width_signedness_case(
        tmp_path / "corpus/development/dev_ws_graph",
        case_id="dev_ws_graph",
        width=8,
        seed=41,
    )
    config = make_config(tmp_path)

    wide = build_graph(config, manifest_path, "v0")
    natural = build_graph(config, manifest_path, "v1")
    wide_analysis = analyze_rules(wide.graph)
    natural_analysis = analyze_rules(natural.graph)

    assert len(wide_analysis["findings"]) == 1
    finding = wide_analysis["findings"][0]
    assert finding["rule_id"] == (
        "width_signedness.redundant_sign_extension.v1"
    )
    assert finding["transformation_id"] == "narrow_intermediate_width"
    assert finding["evidence"]["operation"] == "lt"
    assert finding["evidence"]["signed"] is True
    assert finding["evidence"]["operand_widths"] == {"A": 16, "B": 16}
    assert finding["evidence"]["inferred_source_widths"] == {"A": 8, "B": 8}
    assert finding["evidence"]["redundant_sign_bits"] == {"A": 8, "B": 8}
    assert finding["predicted_effect"] == {
        "area": "neutral",
        "cell_count": "neutral",
        "delay": "neutral",
    }
    assert natural_analysis["findings"] == []


@pytest.mark.skipif(YOSYS is None, reason="Yosys is required")
def test_popcount_rule_distinguishes_balanced_tree(tmp_path: Path) -> None:
    manifest_path = generate_popcount_saturation_case(
        tmp_path / "corpus/development/dev_pc_graph",
        case_id="dev_pc_graph",
        width=8,
        seed=41,
    )
    config = make_config(tmp_path)

    serial = build_graph(config, manifest_path, "v0")
    balanced = build_graph(config, manifest_path, "v1")
    serial_analysis = analyze_rules(serial.graph)
    balanced_analysis = analyze_rules(balanced.graph)

    assert len(serial_analysis["findings"]) == 1
    finding = serial_analysis["findings"][0]
    assert finding["rule_id"] == "popcount.serial_accumulation.v1"
    assert finding["transformation_id"] == "restructure_popcount_or_saturation"
    assert finding["evidence"]["serial_depth"] == 8
    assert finding["evidence"]["balanced_depth_estimate"] == 3
    assert finding["evidence"]["input_bit_terms_estimate"] == 8
    assert finding["evidence"]["count_width"] == 4
    assert finding["predicted_effect"] == {
        "area": "degrade",
        "cell_count": "uncertain",
        "delay": "improve",
    }
    assert balanced_analysis["findings"] == []
