# QASPER smoke test for RAG Fixture Check

This recipe selects six verbatim, human-annotated evidence paragraphs from six paper rows in the [Ai2 QASPER dataset](https://huggingface.co/datasets/allenai/qasper). The selection verifies each quote against the corresponding paper and records the source question, annotation and paper IDs in `DATASET.json`. The generated repo holds six excerpts, attribution and the fixture. Keep it in scratch space. The upstream data card lists CC BY 4.0 and identifies S2ORC as the source of the paper texts. [Dataset card](https://huggingface.co/datasets/allenai/qasper#licensing-information), [paper](https://aclanthology.org/2021.naacl-main.365/)

The baseline contains six original quotes. The next commit keeps one intact, moves one down two lines, replaces one, adds a duplicate, changes the context of one and removes one source file. These changes are simulated. The smoke test exercises source-quote integrity, without evaluating retriever quality or answer correctness.

## Prepare the test repository

From this directory, use Python 3.10 or newer and Git. `generated-qasper-run` must be a new directory. A network connection is needed only for the six-row API request. To reuse a locally saved copy of that response, add `--dataset-json qasper-rows.download.json`.

```powershell
$fixture = python .\make_qasper_repo.py --repo .\generated-qasper-run | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'QASPER preparation failed' }
$fixture | ConvertTo-Json -Depth 6
```

The reference API payload was 292,705 bytes with SHA-256 `0098103221a7826517066a7057e6ba5a12c6005e2aabd03fcc040faeb00ae4ce`. Add `--expect-sha256` followed by this value to reject a changed response. Each run records the digest it actually consumed.

## Exercise the CLI

Run these commands with `rag-fixture-check` available on `PATH`. Replace the executable name with an absolute path when testing a locally built binary.

`verify_qasper.py` prepares a fresh repo and performs the full check automatically. It asserts the reported case statuses, evidence counts, the two-line relocation, and the distinction between duplicates and a context mismatch.

```powershell
python .\verify_qasper.py --checker rag-fixture-check --repo .\generated-qasper-verification
```

To inspect the raw CLI reports for a repo prepared above, run the commands separately.

```powershell
$control = rag-fixture-check check --repo $fixture.repo --base $fixture.base --head $fixture.base --format json
$controlExit = $LASTEXITCODE
$control | ConvertFrom-Json

$drift = rag-fixture-check check --repo $fixture.repo --base $fixture.base --head $fixture.head --format json
$driftExit = $LASTEXITCODE
$drift | ConvertFrom-Json

if ($controlExit -ne 0 -or $driftExit -ne 1) {
    throw "Unexpected exit codes. Control=$controlExit Drift=$driftExit"
}
```

The control reports six `intact` anchors. The controlled revision reports one of each status below. An API payload with reordered paper rows will still produce the same counts. The script emits the exact case mapping for each run in `expected_head_status`.

| Case from reference payload | Expected revision status | Expected source behavior |
| --- | --- | --- |
| `qas-1909.00694` | `intact` | Exact quote at the original location |
| `qas-2003.07723` | `relocated` | Exact quote plus context remain intact two lines later |
| `qas-1705.09665` | `missing` | Neither the original location nor another occurrence contains the exact quote |
| `qas-1908.06606` | `ambiguous` | Two exact occurrences with no selectors to distinguish them |
| `qas-1811.00942` | `context_changed` | Literal quote survives, but its stored prefix differs |
| `qas-1805.02400` | `file_missing` | The source path is gone at the comparison commit |

For setup failure coverage, supply `--head no-such-commit` and verify exit `2`. The generated repository is self-contained and has no configured remote.
