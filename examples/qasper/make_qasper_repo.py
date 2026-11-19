"""Make a small, disposable QA repo from exact, human-annotated QASPER evidence."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path


DATA_URL = (
    "https://datasets-server.huggingface.co/rows?"
    "dataset=allenai%2Fqasper&config=qasper&split=train&offset=0&length=6"
)
DATASET_URL = "https://huggingface.co/datasets/allenai/qasper"
STATES = ("intact", "relocated", "missing", "ambiguous", "context_changed", "file_missing")
MAX_INPUT_BYTES = 2 * 1024 * 1024
PREFIX = "Evidence excerpt begins below.\n"
SUFFIX = "\nEvidence excerpt ends above."


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], text=True, encoding="utf-8",
        capture_output=True, check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "RAG Fixture QA",
            "GIT_AUTHOR_EMAIL": "qa@example.invalid",
            "GIT_COMMITTER_NAME": "RAG Fixture QA",
            "GIT_COMMITTER_EMAIL": "qa@example.invalid",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
        },
    ).stdout.strip()


def read_dataset(local_path):
    if local_path is None:
        request = urllib.request.Request(DATA_URL, headers={"User-Agent": "rag-fixture-qa/1"})
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = response.read(MAX_INPUT_BYTES + 1)
    else:
        with local_path.open("rb") as downloaded:
            payload = downloaded.read(MAX_INPUT_BYTES + 1)
    if len(payload) > MAX_INPUT_BYTES:
        raise ValueError("Use the six-row API response, not a full dataset export.")
    return json.loads(payload), hashlib.sha256(payload).hexdigest()


def choose_anchor(row):
    paper_id = row["id"]
    if not re.fullmatch(r"[0-9]{4}\.[0-9]{4,5}", paper_id):
        return None
    source_paragraphs = [paragraph for section in row["full_text"]["paragraphs"] for paragraph in section]
    questions = row["qas"]
    for question, qid, answers in zip(
        questions["question"], questions["question_id"], questions["answers"], strict=True,
    ):
        for annotation_id, answer in zip(answers["annotation_id"], answers["answer"], strict=True):
            if answer["unanswerable"]:
                continue
            for quote in answer["evidence"]:
                if (
                    120 <= len(quote) <= 700
                    and not quote.startswith("FLOAT SELECTED")
                    and source_paragraphs.count(quote) == 1
                    and sum(paragraph.count(quote) for paragraph in source_paragraphs) == 1
                ):
                    return {
                        "paper_id": paper_id, "question_id": qid, "annotation_id": annotation_id,
                        "question": question, "exact": quote,
                    }
    return None


def write_json(path, value):
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def prepare(repo, source, pin):
    if repo.exists() or repo.is_symlink():
        raise ValueError(f"Choose a new directory. Already exists: {repo}")
    repo = repo.resolve()

    dataset, digest = read_dataset(source)
    if pin and digest.casefold() != pin.casefold():
        raise ValueError(f"Source SHA-256 differs from requested pin: {digest}")
    anchors = []
    for item in dataset["rows"]:
        if item["truncated_cells"]:
            continue
        selected = choose_anchor(item["row"])
        if selected is not None:
            anchors.append(selected)
    if len(anchors) != len(STATES):
        raise ValueError(f"Expected six usable exact evidence paragraphs, found {len(anchors)}.")

    repo.mkdir(parents=True)
    (repo / "docs").mkdir()
    git(repo, "init", "--quiet")
    git(repo, "config", "--local", "core.autocrlf", "false")
    git(repo, "config", "--local", "commit.gpgsign", "false")

    cases = []
    provenance = []
    baseline_docs = {}
    expected = {}
    for status, anchor in zip(STATES, anchors, strict=True):
        quote = anchor["exact"]
        paper_id = anchor["paper_id"]
        case_id = f"qas-{paper_id}"
        relative_path = f"docs/{paper_id}.md"
        title = f"# QASPER evidence excerpt for paper {paper_id}\n\n"
        doc = f"{title}{PREFIX}{quote}{SUFFIX}\n"
        baseline_docs[status] = (repo / relative_path, doc, quote)
        (repo / relative_path).write_bytes(doc.encode("utf-8"))
        evidence = {"path": relative_path, "exact": quote}
        if status != "ambiguous":
            evidence.update(prefix=PREFIX, suffix=SUFFIX)
        cases.append({"id": case_id, "question": anchor["question"], "evidence": [evidence]})
        provenance.append({
            "case": case_id,
            "paper": paper_id,
            "question": anchor["question_id"],
            "annotation": anchor["annotation_id"],
        })
        expected[case_id] = status

    write_json(repo / ".rag-fixtures.json", {"version": 1, "cases": cases})
    write_json(repo / "DATASET.json", {
        "dataset": DATASET_URL,
        "data_endpoint": DATA_URL,
        "sha256_of_rows_response": digest,
        "split": "train",
        "row_offset": 0,
        "row_limit": 6,
        "licensing_and_attribution": "https://huggingface.co/datasets/allenai/qasper#licensing-information",
        "paper": "https://aclanthology.org/2021.naacl-main.365/",
        "cases": provenance,
    })
    git(repo, "add", "--", ".")
    git(repo, "commit", "--quiet", "-m", "QA baseline from QASPER evidence")
    base = git(repo, "rev-parse", "HEAD")

    for status, (path, doc, quote) in baseline_docs.items():
        if status == "intact":
            continue
        if status == "file_missing":
            path.unlink()
            continue
        if status == "relocated":
            doc = "Editorial preface inserted in the simulated revision.\n\n" + doc
        elif status == "missing":
            doc = doc.replace(quote, "This evidence paragraph was edited in the simulated revision.", 1)
        elif status == "ambiguous":
            doc += f"\nSecond copy in the simulated revision:\n\n{quote}\n"
        elif status == "context_changed":
            doc = doc.replace(PREFIX, "Revised lead-in for the simulated revision.\n", 1)
        path.write_bytes(doc.encode("utf-8"))

    git(repo, "add", "--", ".")
    git(repo, "commit", "--quiet", "-m", "QA revision with controlled evidence drift")
    head = git(repo, "rev-parse", "HEAD")
    return {"repo": str(repo), "base": base, "head": head, "expected_head_status": expected}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="Nonexistent path for the disposable Git repository.")
    parser.add_argument("--dataset-json", type=Path, help="Use a locally saved API response.")
    parser.add_argument("--expect-sha256", help="Require this SHA-256 of the original API response.")
    args = parser.parse_args()
    try:
        report = prepare(args.repo, args.dataset_json, args.expect_sha256)
    except subprocess.CalledProcessError as error:
        parser.exit(2, f"git: {error.stderr}\n")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"{error}\n")
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
