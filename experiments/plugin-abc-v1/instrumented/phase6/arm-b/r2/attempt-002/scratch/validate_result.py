import json
from pathlib import Path

ROOT = Path("/Users/peter/Desktop/rtl-advisor")
DRAFT = ROOT / "experiments/plugin-abc-v1/instrumented/phase6/arm-b/r2/attempt-002/scratch/result-draft.json"
PACKET = ROOT / "experiments/plugin-abc-v1/packets/arm-b-r2.json"

data = json.loads(DRAFT.read_text())
packet = json.loads(PACKET.read_text())

top_required = {
    "schema",
    "experiment_id",
    "arm",
    "repetition",
    "model",
    "reasoning_effort",
    "manifest_sha256",
    "cases",
}
assert set(data) == top_required
assert data["schema"] == "rtl-advisor-plugin-abc-arm-result-v1"
for key in ("experiment_id", "arm", "repetition", "model", "reasoning_effort", "manifest_sha256"):
    assert data[key] == packet[key], key
assert 1 <= len(data["cases"]) <= 24
assert [(x["case_id"], x["source_sha256"]) for x in data["cases"]] == [
    (x["case_id"], x["source_sha256"]) for x in packet["cases"]
]

case_required = {
    "case_id",
    "source_sha256",
    "decision",
    "scope_claim",
    "source_locations",
    "rationale",
    "candidates",
    "evidence_complete",
}
candidate_required = {
    "candidate_id",
    "path",
    "formal_status",
    "measurement_status",
    "final_state",
    "evidence_paths",
}
decisions = {"recommend_change", "no_change", "unsupported", "inconclusive"}
scopes = {"whole_case", "finding_only", "none"}
formal_states = {"formal_passed", "formal_failed", "formal_inconclusive", "not_run"}
measurement_states = {"measured", "not_run", "not_applicable"}
final_states = {
    "measured_improvement",
    "synthesis_handles",
    "flow_dependent",
    "regression",
    "unproven",
    "not_applicable",
}

for case in data["cases"]:
    assert set(case) == case_required, case["case_id"]
    assert case["decision"] in decisions
    assert case["scope_claim"] in scopes
    assert isinstance(case["source_locations"], list)
    assert isinstance(case["rationale"], str)
    assert isinstance(case["evidence_complete"], bool)
    assert len(case["candidates"]) <= 3
    for candidate in case["candidates"]:
        assert set(candidate) == candidate_required
        assert candidate["formal_status"] in formal_states
        assert candidate["measurement_status"] in measurement_states
        assert candidate["final_state"] in final_states
        assert Path(candidate["path"]).is_file()
        assert all(Path(path).is_file() for path in candidate["evidence_paths"])

print(f"validated {len(data['cases'])} ordered cases and all candidate evidence paths")
