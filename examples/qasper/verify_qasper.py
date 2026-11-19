"""Run the actual checker against the QASPER control and drift commits."""

import argparse
import json
import subprocess
from pathlib import Path

from make_qasper_repo import prepare


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def check(binary, repo, base, head, exit_code):
    command = [binary, "check", "--repo", repo, "--base", base, "--head", head, "--format", "json"]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=60)
    require(
        result.returncode == exit_code,
        f"Expected exit {exit_code} for {head}, got {result.returncode}: {result.stderr.strip()}",
    )
    if exit_code == 2:
        require(result.stderr.strip(), "An invalid commit should produce a diagnostic.")
        return None

    report = json.loads(result.stdout)
    require(report["schemaVersion"] == 1, "Unexpected report schema version.")
    require(report["base"] == base and report["head"] == head, "Report used the wrong revisions.")
    require(report["fixtureChanges"] == {"added": [], "modified": [], "removed": []}, "Some fixture changed.")
    require(report["baseline"] == {"present": True, "readable": True}, "The baseline fixture was not usable.")
    require(report["summary"]["cases"] == 6 and report["summary"]["evidence"] == 6, "Expected six real anchors.")
    require(report["summary"]["needsReview"] == (exit_code == 1), "Inconsistent review flag.")
    require(all(len(case["evidence"]) == 1 for case in report["cases"]), "Expected one anchor per QA case.")
    return {case["id"]: case["evidence"][0] for case in report["cases"]}


def verify(binary, prepared):
    repo, base, head = (prepared[key] for key in ("repo", "base", "head"))
    expected = prepared["expected_head_status"]
    controls = check(binary, repo, base, base, 0)
    require({case: value["status"] for case, value in controls.items()} == dict.fromkeys(expected, "intact"),
            "All six baseline anchors should be intact.")

    drifted = check(binary, repo, base, head, 1)
    statuses = {case: value["status"] for case, value in drifted.items()}
    require(statuses == expected, f"Wrong status assignments: {statuses}")
    relocated = next(value for value in drifted.values() if value["status"] == "relocated")
    require(relocated["oldLine"] > 0 and relocated["newLine"] == relocated["oldLine"] + 2,
            "Expected relocation by two lines.")
    ambiguous = next(value for value in drifted.values() if value["status"] == "ambiguous")
    require(ambiguous["occurrences"] == 2 and ambiguous["selected"] == 2,
            "Expected two literal matches selected for ambiguity.")
    context = next(value for value in drifted.values() if value["status"] == "context_changed")
    require(context["occurrences"] == 1 and context["selected"] == 0,
            "Expected one literal match with mismatched context.")
    check(binary, repo, base, "rag-fixture-qa-no-such-commit", 2)
    return {**prepared, "exit_codes": {"control": 0, "drift": 1, "invalid_ref": 2}, "observed_status": statuses}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checker", default="rag-fixture-check", help="Executable on PATH or a path to a local build.")
    parser.add_argument("--repo", required=True, type=Path, help="Fresh directory for the generated Git repo.")
    parser.add_argument("--dataset-json", type=Path, help="Use a saved QASPER rows response.")
    parser.add_argument("--expect-sha256", help="Require a pinned digest for the rows response.")
    args = parser.parse_args()
    prepared = prepare(args.repo, args.dataset_json, args.expect_sha256)
    print(json.dumps(verify(args.checker, prepared), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
