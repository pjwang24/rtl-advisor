#!/bin/sh
set -eu

yosys -V
verilator --version
yosys-abc -q 'version; quit'
python --version
uv --version

case "$(yosys -V)" in
  "Yosys 0.63"*) ;;
  *) echo "expected Yosys 0.63.x" >&2; exit 1 ;;
esac
case "$(verilator --version)" in
  "Verilator 5."*) ;;
  *) echo "unrecognized Verilator version" >&2; exit 1 ;;
esac
test "$(yosys-abc -q 'version; quit' | awk '{print $4}')" = "1.01"
test "$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" = "3.13"
test "$(uv --version | awk '{print $2}')" = "0.11.5"

uv run --frozen pytest \
  tests/test_mvp_rewriter.py::test_formal_accepts_rewrite_and_rejects_intentional_logic_error

uv run --frozen rtl-advisor agent review examples/mvp/adder_chain.sv \
  --top adder_chain --objective balanced --schema-version 2 --json \
  > /tmp/review.json
run_id="$(uv run --frozen python -c 'import json; print(json.load(open("/tmp/review.json"))["run_id"])')"
finding_id="$(uv run --frozen python -c 'import json; print(json.load(open("/tmp/review.json"))["findings"][0]["finding_id"])')"

uv run --frozen rtl-advisor agent candidate "$run_id" \
  --finding "$finding_id" --schema-version 2 --json \
  > /tmp/candidate.json
candidate_id="$(uv run --frozen python -c 'import json; print(json.load(open("/tmp/candidate.json"))["candidate_id"])')"

uv run --frozen rtl-advisor agent verify "$run_id" \
  --candidate "$candidate_id" --schema-version 2 --json \
  > /tmp/verification.json
uv run --frozen python -c \
  'import json; p=json.load(open("/tmp/verification.json")); assert p["decision"] == "formal_passed" and p["safe"] is True'

uv run --frozen rtl-advisor agent measure "$run_id" \
  --candidate "$candidate_id" --schema-version 2 --json \
  > /tmp/measurement.json
uv run --frozen python -c \
  'import json; p=json.load(open("/tmp/measurement.json")); assert p["decision"] == "synthesis_handles"'

uv run --frozen rtl-advisor agent report "$run_id" \
  --schema-version 2 --json > /tmp/report.json
uv run --frozen python -c \
  'import json; p=json.load(open("/tmp/report.json")); assert p["decision"] == "synthesis_handles" and p["run_schema"] == "rtl-advisor-run-v1"'
