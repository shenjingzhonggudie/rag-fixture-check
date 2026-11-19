# rag-fixture-check

**Know which RAG evaluation cases your docs PR just broke — before it merges.**

[中文文档](README.zh-CN.md) · [Download v0.3.0](https://github.com/shenjingzhonggudie/rag-fixture-check/releases/tag/v0.3.0) · [CI workflow](examples/workflows/rag-fixture-check.yml)

`rag-fixture-check` is a single-binary Go CLI that resolves every evidence anchor in your RAG fixture against the committed tree of a pull request. It tells you exactly which quotes disappeared, which ones moved, which ones became ambiguous, and which `prefix`/`suffix` selectors no longer match.

It is deliberately narrow: **it verifies evidence anchoring, not answer quality.** Whether a quote still supports the expected answer is a human review decision, and the tool says so in every output format.

- Zero third-party dependencies, Go 1.22+, one static binary
- Reads committed trees only, so uncommitted edits and dirty worktrees cannot lie to you
- Deterministic statuses, deterministic JSON, exit codes you can gate CI on
- Works with Chinese filenames, spaces, CRLF documents, nested directories and shallow-clone mistakes

---

## Table of contents

- [The problem](#the-problem)
- [2-minute quickstart](#2-minute-quickstart)
- [Install](#install)
- [CLI](#cli)
- [Fixture format](#fixture-format)
- [Statuses](#statuses)
- [Output formats](#output-formats)
- [Fixture changes: added, modified, removed](#fixture-changes-added-modified-removed)
- [CI adoption path](#ci-adoption-path)
- [Worked example: refund window 30 → 14 days](#worked-example-refund-window-30--14-days)
- [Importing a RAGKit evaluation set](#importing-a-ragkit-evaluation-set)
- [What the test suite covers](#what-the-test-suite-covers)
- [Boundaries and non-goals](#boundaries-and-non-goals)
- [Security model](#security-model)
- [Development](#development)
- [License](#license)

---

## The problem

A RAG evaluation case is only as good as its evidence. A case like this:

```json
{
  "id": "refund-30",
  "question": "How many days do customers have?",
  "evidence": [{ "path": "docs/refund.md", "exact": "Refunds are accepted within 30 days." }]
}
```

silently rots the moment a writer changes `30 days` to `14 days`, renames `docs/refund.md`, or rewords the heading above the sentence. The evaluation suite keeps passing against stale expectations, and nobody notices until a customer does.

Diff review does not catch this reliably: the fixture file is often untouched by the docs PR, so it never appears in the diff at all.

`rag-fixture-check` closes that gap by re-resolving every anchor against the **head commit**, using the base commit only to explain *what changed*.

## 2-minute quickstart

```bash
# 1. Install (or use "go run" from a checkout, see below)
go install github.com/shenjingzhonggudie/rag-fixture-check@latest

# 2. See every status on a throwaway repo — no setup, no network
git clone https://github.com/shenjingzhonggudie/rag-fixture-check
cd rag-fixture-check
go run ./examples/demo
```

The demo builds a temporary repository with two commits, breaks a refund window, moves a paragraph, renames a file, drifts a heading, duplicates a quote and deletes a page. It prints the report below and asserts that the exit code is `1`.

To try real annotated evidence, run the [QASPER smoke test](examples/qasper/README.md). It retrieves six human-annotated evidence paragraphs and compares them across two Git commits with controlled edits.

```text
rag-fixture-check 0.3.0
repo:     /tmp/rag-fixture-demo-1234567
base:     4a1c0f0...
head:     9b7e2d1...
fixture:  .rag-fixtures.json (read from head)
baseline: present (read from base)

CASE refund-30
  [missing]         docs/refund.md (last seen at line 3)  matches=0 selected=0  quote not found in file

CASE shipping-moved
  [relocated]       docs/shipping.md:5 (was line 3)  matches=1 selected=1  quote moved from line 3 to line 5

CASE faq-renamed
  [relocated]       docs/faq.md:3 -> docs/guides/faq.md:3  matches=1 selected=1  file was renamed to docs/guides/faq.md (git detected a rename)

CASE context-drift
  [context_changed] docs/context.md:3  matches=1 selected=0  quote occurs 1 times but none match the prefix/suffix context

CASE dup-quote
  [ambiguous]       docs/dup.md  matches=2 selected=2  quote occurs 2 times and 2 of them match the selector

CASE legacy-gone
  [file_missing]    docs/legacy.md (last seen at line 1)  matches=0 selected=0  path is not present in the head commit

CASE chinese-path
  [intact]          docs/退款说明.md:3  matches=1 selected=1

CASE crlf-doc
  [intact]          docs/crlf.md:3  matches=1 selected=1

SUMMARY
  cases=8 evidence=8
  intact=2 relocated=2 missing=1 ambiguous=1 context_changed=1 file_missing=1 unreadable=0
  fixture: added=0 modified=0 removed=0
VERDICT: review needed (4 anchor(s) are not intact)
NOTE: This tool verifies evidence anchoring only. Whether a quote still supports the expected answer is a human review decision.
```

Now do it for real in your own repository:

```bash
cd /path/to/your-docs-repo

# Write .rag-fixtures.json (schema below), then commit it together with docs.
git add .rag-fixtures.json docs/
git commit -m "add RAG fixtures"

# Check the commit you just made against its parent.
rag-fixture-check check --repo . --base HEAD~1 --head HEAD
```

Because `HEAD~1` has no fixture yet, the first run reports every case as `added` (and still verifies every anchor). From the next commit on, you get real added/modified/removed detection.

## Install

**Go toolchain (recommended):**

```bash
go install github.com/shenjingzhonggudie/rag-fixture-check@latest
```

**Prebuilt binary:** download the archive and `SHA256SUMS` from [GitHub Releases](https://github.com/shenjingzhonggudie/rag-fixture-check/releases). Compare the checksum for your archive with `sha256sum` on Linux, `shasum -a 256` on macOS, or `Get-FileHash -Algorithm SHA256` in PowerShell. Extract the binary and put it on `PATH`.

**From source:**

```bash
git clone https://github.com/shenjingzhonggudie/rag-fixture-check
cd rag-fixture-check
go build -trimpath -o rag-fixture-check .
```

Requirements at runtime: a `git` executable in `PATH`. Nothing else — no Python, no embeddings, no network access.

Pin a version in CI with `go install ...@v0.3.0` or a checksummed release asset.

## CLI

```
rag-fixture-check check --repo <path> --base <commit-ish> --head <commit-ish> [flags]
rag-fixture-check version
rag-fixture-check help
```

| Flag | Required | Default | Meaning |
| --- | --- | --- | --- |
| `--repo` | yes | — | Repository to inspect. Any directory inside the worktree works; the tool resolves the root itself. |
| `--base` | yes | — | Baseline revision. Resolved and pinned to a full commit SHA. |
| `--head` | yes | — | Revision under review. Resolved and pinned to a full commit SHA. |
| `--fixture` | no | `.rag-fixtures.json` | Repo-relative fixture path. Read from `head`; the same path at `base` is the baseline. |
| `--format` | no | `text` | `text`, `json` or `github`. |
| `--help` | no | — | Print usage and exit 0. |
| `--version` | no | — | Print version and exit 0. |

Flags accept both `--flag value` and `--flag=value`. Only the flags above are recognised: unknown options are rejected before `git` is invoked, so option-injection attempts such as `--base --upload-pack=...` fail with exit code 2.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Every evidence anchor is `intact` or `relocated`. Fixture additions, modifications and removals are informational only. |
| `1` | At least one anchor is `missing`, `ambiguous`, `context_changed`, `file_missing` or `unreadable`. The change needs review. |
| `2` | Invalid input or a Git failure: bad flag, unknown revision, missing fixture at head, malformed fixture, not a repository, unreadable blob, etc. |

Exit code 2 prints nothing on stdout. Exit code 1 still prints the full report, so CI logs stay useful.

## Fixture format

Default location: `.rag-fixtures.json` at the repository root (configurable with `--fixture`).

```json
{
  "version": 1,
  "cases": [
    {
      "id": "refund-30",
      "question": "How many days do customers have?",
      "evidence": [
        {
          "path": "docs/refund.md",
          "exact": "Refunds are accepted within 30 days.",
          "prefix": "",
          "suffix": ""
        }
      ]
    }
  ]
}
```

| Field | Required | Rules |
| --- | --- | --- |
| `version` | yes | Must be `1`. Unknown versions are rejected. |
| `cases[].id` | yes | 1–200 bytes, unique, no control characters. Shown in every report. |
| `cases[].question` | no | Free text, ≤ 4096 bytes. Shown to reviewers; never used for matching. |
| `cases[].evidence` | yes | Array, ≤ 200 entries. May be empty, but then nothing can be verified. |
| `evidence[].path` | yes | Repo-relative, forward slashes, no `..`/`.`/empty segments, no backslash, no colon, no control characters. Chinese characters and spaces are fine. |
| `evidence[].exact` | yes | Literal text to find, 1–16384 bytes. |
| `evidence[].prefix` / `suffix` | no | Literal text immediately before/after `exact`, ≤ 4096 bytes each. Empty means "no constraint on that side". |

Machine-readable schema: [`schema/rag-fixtures.schema.json`](schema/rag-fixtures.schema.json). Point your editor at it for completion and validation:

```json
{ "$schema": "./schema/rag-fixtures.schema.json" }
```

**Matching rules** — deliberately literal and boring:

- Byte-exact match after normalising `\r\n` → `\n` in both the document and the selector. No case folding, no whitespace folding, no Unicode normalisation, no fuzzy matching.
- `prefix` must be the text *immediately* before `exact`; `suffix` *immediately* after. No skipping, no regex.
- If `exact` occurs more than once and the selectors cannot narrow it to exactly one occurrence, the result is `ambiguous`. The tool never silently picks the first occurrence.
- Overlapping occurrences are counted separately.

**Limits** (never silently truncated): fixture ≤ 4 MiB, ≤ 5000 cases, ≤ 20000 anchors total, ≤ 200 anchors per case — exceeding any of these is an input error (exit 2). An evidence file above 8 MiB is reported as `unreadable` (exit 1) rather than being partially read.

## Statuses

| Status | Meaning | Exit 1? |
| --- | --- | --- |
| `intact` | Quote found exactly once at the expected path, selectors satisfied, same line as the baseline (or no baseline). | no |
| `relocated` | Quote found exactly once, but the line moved, the fixture path was updated, or a real Git rename moved the file. | no |
| `missing` | The file exists but the literal quote does not occur in the head commit. | yes |
| `ambiguous` | The quote occurs multiple times and the selectors cannot select exactly one occurrence. | yes |
| `context_changed` | The quote occurs, but none of its occurrences match `prefix`/`suffix` — typically a nearby heading or sentence changed. | yes |
| `file_missing` | The path does not exist in the head tree and no unique Git rename rescues it. | yes |
| `unreadable` | The path exists but is not a regular file (symlink, submodule, directory), or exceeds the blob size limit. | yes |

`relocated` is reported but does not fail CI: the evidence is still there, it just moved. That keeps busy documentation repos from failing on every typo fix above a quote.

Rename handling is intentionally strict: the tool only follows renames that **Git itself detected** (`git diff --name-status -M`) and only when the mapping is unique. A delete-and-add of similar content is not guessed at.

## Output formats

### `--format text` (default)

Human-readable, no ANSI colour, safe to redirect to a file or paste into a PR comment. Shows old and new line numbers plus match counts:

```text
CASE refund-30  (modified)
  question: How many days do customers have?
  [relocated]       docs/refund.md:12 (was line 9)  matches=1 selected=1  quote moved from line 9 to line 12
```

### `--format json`

Stable, versioned (`schemaVersion: 1`), deterministic: the same input produces byte-identical output. All arrays are emitted as `[]` rather than `null`, and every anchor always carries the same set of keys. Schema: [`schema/report.schema.json`](schema/report.schema.json).

```json
{
  "schemaVersion": 1,
  "tool": "rag-fixture-check",
  "version": "0.3.0",
  "repo": "/repo",
  "base": "4a1c0f0...",
  "head": "9b7e2d1...",
  "fixture": ".rag-fixtures.json",
  "baseline": { "present": true, "readable": true },
  "summary": {
    "cases": 1, "evidence": 1,
    "intact": 0, "relocated": 0, "missing": 1, "ambiguous": 0,
    "contextChanged": 0, "fileMissing": 0, "unreadable": 0,
    "addedCases": 0, "modifiedCases": 0, "removedCases": 0,
    "needsReview": true
  },
  "fixtureChanges": { "added": [], "modified": [], "removed": [] },
  "removedCases": [],
  "cases": [
    {
      "id": "refund-30",
      "question": "How many days do customers have?",
      "change": "unchanged",
      "evidence": [
        {
          "index": 0,
          "path": "docs/refund.md",
          "status": "missing",
          "oldPath": "docs/refund.md",
          "newPath": "",
          "oldLine": 3,
          "newLine": 0,
          "occurrences": 0,
          "selected": 0,
          "selectorChanged": false,
          "detail": "quote not found in file"
        }
      ]
    }
  ],
  "notes": [],
  "disclaimer": "This tool verifies evidence anchoring only. Whether a quote still supports the expected answer is a human review decision."
}
```

### `--format github`

Emits GitHub Actions workflow commands. `missing` / `ambiguous` / `context_changed` / `file_missing` / `unreadable` become `::error` annotations with `file` and `line`; `relocated` and fixture changes become `::notice`. Annotation count is capped at 50 with an explicit truncation notice.

```text
::error file=docs/refund.md,title=rag-fixture-check (missing)::[refund-30] evidence 0: docs/refund.md (last seen at line 3)  matches=0 selected=0 quote not found in file
::notice file=docs/shipping.md,line=5,title=rag-fixture-check (relocated)::[shipping] evidence 0: docs/shipping.md:5 (was line 3) ...
::error title=rag-fixture-check::1 evidence anchor(s) need review; cases=1 evidence=1 ...
```

All `%`, CR and LF characters coming from fixture data are escaped (`%25`, `%0D`, `%0A`) and control characters are stripped, so a crafted case id cannot inject extra workflow commands.

## Fixture changes: added, modified, removed

When a baseline fixture exists at `base`, the tool compares the two documents and reports:

- **added** — case id only present at head; every anchor is still verified.
- **modified** — same id, but question or any evidence entry differs. `selectorChanged` is set per anchor, and a reviewed selector update does not get reported as a relocation.
- **removed** — the case is gone at head. The report lists the id, the question and the original evidence paths, so a reviewer can confirm the deletion was intentional. Removal alone does not fail the run.

These are observations, not judgements. The tool does not assume a new case is good or that a deleted case was useless.

If the baseline fixture is missing, every case is reported as `added` and a note says so. If the baseline fixture is present but malformed, it is ignored with a note instead of blocking the run — a broken baseline must not hide head-side anchor results.

## CI adoption path

### Step 1 — observe for a week (non-blocking)

Copy [`examples/workflows/rag-fixture-check.yml`](examples/workflows/rag-fixture-check.yml), set `continue-on-error: true` on the check step, and collect reports. Fix the low-hanging fruit (missing files, drifted selectors) without blocking anyone.

### Step 2 — make it blocking

Remove `continue-on-error`. Keep the workflow scoped to the paths it cares about:

```yaml
on:
  pull_request:
    paths:
      - ".rag-fixtures.json"
      - "docs/**"
```

### Step 3 — make ownership explicit

Add a `CODEOWNERS` entry for `.rag-fixtures.json` so fixture edits get a domain review, and keep `--format github` so failures land directly on the changed lines.

The [copyable workflow](examples/workflows/rag-fixture-check.yml) installs a pinned release and does not build code from the docs repository.

- **`fetch-depth: 0` is not optional.** With a shallow checkout the base commit is missing; the tool exits 2 with an explicit hint instead of silently skipping the comparison.
- **Pin both SHAs from the event payload.** `pull_request.base.sha` and `pull_request.head.sha` are immutable for the run; branch names can move under you.
- **No secrets, no write permissions.** Fork PRs work unmodified, and the job is safe to run with `contents: read`.
- **Keep the artifact step on `if: failure()`** so you retain the full text report after annotations are truncated or the log is trimmed.

## Worked example: refund window 30 → 14 days

Base commit:

```text
docs/refund.md
──────────────
1  # Refund policy
2
3  Refunds are accepted within 30 days.
```

```json
{ "id": "refund-30", "evidence": [{ "path": "docs/refund.md", "exact": "Refunds are accepted within 30 days." }] }
```

The PR changes line 3 to `Refunds are accepted within 14 days.` and does not touch the fixture:

```text
$ rag-fixture-check check --repo . --base origin/main --head HEAD
CASE refund-30
  [missing]         docs/refund.md (last seen at line 3)  matches=0 selected=0  quote not found in file

VERDICT: review needed (1 anchor(s) are not intact)
$ echo $?
1
```

The reviewer now has a concrete choice: update the expected answer and the evidence to 14 days, or revert the doc change. Either way, the evaluation suite cannot silently drift.

Other outcomes from the same mechanism:

| Change in the PR | Status |
| --- | --- |
| Paragraph inserted above the quote | `relocated` (line moved) |
| `docs/faq.md` renamed, fixture untouched | `relocated` via Git rename |
| Heading above the quote reworded | `context_changed` |
| Sentence duplicated elsewhere in the file | `ambiguous` |
| Page deleted | `file_missing` |
| Quote reworded | `missing` |

## Importing a RAGKit evaluation set

`tools/ragkit_import.py` converts a RAGKit project into the two inputs `check` needs: a fixture and a corpus snapshot. It reuses RAGKit's own `load_file` and `normalize_text`, so every `exact` quote is byte-identical to the text the annotators measured.

RAGKit records quotes and offsets against `normalize_text(load_file(path).text)`. Normalization can change the text itself, including punctuation and whitespace. In the reference pilot, applying the normalized quotes to unchanged raw Markdown reported 459 missing anchors out of 589. Exporting the normalized snapshot yielded 589 intact anchors.

### Requirements

- Python 3.11+ for the importer only; the Go CLI stays dependency-free
- A RAGKit checkout containing `data/eval/qrels.json`, `data/raw/` and, optionally, `data/eval/annotation_manifest.json`
- The importer script from the [v0.3.0 source tree](https://github.com/shenjingzhonggudie/rag-fixture-check/tree/v0.3.0). It is also available in the [source archive](https://github.com/shenjingzhonggudie/rag-fixture-check/archive/refs/tags/v0.3.0.zip) if you installed only the checker binary.

### Import

Run the command from the checker source tree. Use the RAGKit Python environment if the checkout needs its installed dependencies. Pass a RAGKit checkout you trust, since the importer executes its loader and normalizer.

```bash
python tools/ragkit_import.py \
  --ragkit-root /path/to/ragkit \
  --output /tmp/ragkit-snapshot
```

`--ragkit-root` accepts the project root or its `src` directory. The importer puts that directory on `sys.path` and imports `ragkit.loaders.load_file` plus `ragkit.text.normalize.normalize_text` from the checkout — it never re-implements Markdown parsing or normalization.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--qrels` | `data/eval/qrels.json` | evaluation set, relative to the root |
| `--manifest` | `data/eval/annotation_manifest.json` | annotation manifest; skipped when absent |
| `--raw-dir` | `data/raw` | raw corpus root |
| `--corpus-prefix` | `corpus` | snapshot directory that holds the normalized text |
| `--force` | off | replace a complete prior import with the same corpus prefix |

The output contains full normalized versions of referenced documents, plus the questions and quotes. Commit it only to a repository where those inputs can be shared.

```text
/tmp/ragkit-snapshot/
  .rag-fixtures.json     # version 1; every case carries path/exact/prefix/suffix
  corpus/...             # normalized text, one file per referenced document
  import-summary.json    # counts, input hashes, problems; no absolute paths
```

### What the importer guarantees

| Check | Behaviour |
| --- | --- |
| Case selection | `needs_review` and no-answer cases are skipped and counted; answers without any evidence are counted separately |
| Span check | `exact` must equal `normalized[start:end]` byte for byte |
| Duplicate quotes | prefix/suffix grow from 8 to 256 characters until exactly one occurrence matches the annotated offset; if that never happens the case is reported, never guessed |
| Paths | portable repo-relative paths, with extra checks for collisions and reserved names |
| Hashes | when the manifest exists, the qrels SHA-256 and every referenced document's normalized SHA-256 must match |
| Self-check | every anchor in the finished fixture is re-resolved against the snapshot with the Go matcher's rules before anything is written |

Exit `0` writes the snapshot. Exit `1` reports an invalid case, anchor or normalized document on stderr before writing. Exit `2` covers setup, qrels hash, manifest format, output conflict or filesystem errors.

### Refresh after the source documents change

Documentation edits can invalidate old quotes or the annotation manifest. Use `refresh` to copy the baseline fixture byte for byte and rebuild the normalized corpus for comparison.

```bash
python tools/ragkit_import.py refresh \
  --ragkit-root /path/to/ragkit \
  --baseline /tmp/ragkit-snapshot \
  --output /tmp/ragkit-snapshot-v2
```

| Behaviour | Result |
| --- | --- |
| fixture | copied byte for byte; ids, quotes and selectors never change |
| quote no longer present in the source | refresh exits 0; `check` reports `missing` |
| source file deleted | the file is omitted from the new snapshot; `check` reports `file_missing` |
| source file renamed | treated as "the old path went missing" in this round |
| permission, decode, path or write failure | exit 2 naming the offending path; never reported as a deletion |
| annotation manifest | not verified — it describes the old corpus |
| output directory | use a separate directory; `--force` can replace a complete prior refresh with the same corpus prefix |

`refresh-summary.json` records `counts.rebuilt`, `missingPaths` and the frozen fixture's SHA-256:

```json
{
  "mode": "refresh",
  "frozenFixture": { "path": ".rag-fixtures.json", "sha256": "…", "cases": 3, "anchors": 3 },
  "counts": { "cases": 3, "anchors": 3, "documents": 3, "rebuilt": 2, "missing": 1 },
  "missingPaths": ["corpus/docs/autoscaling.md"]
}
```

### Verify a PR without writing a new snapshot

`verify` runs the whole loop in one command: read the frozen fixture, rebuild the corpus from a PR's raw documents with the trusted loader, commit both snapshots into a throwaway Git repository, then run `check` there.

```bash
python tools/ragkit_import.py verify \
  --ragkit-root /path/to/trusted-ragkit \
  --baseline /tmp/ragkit-snapshot \
  --raw-dir /path/to/pr/data/raw \
  --checker /usr/local/bin/rag-fixture-check \
  --report verify-report.json
```

| Exit | Meaning |
| --- | --- |
| `0` | every anchor still resolves; the report is written |
| `1` | at least one anchor needs review; the report is written before exiting |
| `2` | input, read, Git, checker or report-write failure; any previous report is left in place |

The terminal prints the summary and the affected case ids. The JSON report adds provenance to the complete checker output:

```json
{
  "mode": "verify",
  "status": "review",
  "checkerExitCode": 1,
  "frozenFixture": { "sha256": "…", "bytesIdenticalAcrossCommits": true, "cases": 3, "anchors": 3 },
  "corpus": { "rebuilt": 2, "missing": 1, "missingPaths": ["corpus/docs/autoscaling.md"] },
  "commits": { "base": "…", "head": "…" },
  "affected": [{ "id": "refund-30", "question": "How many days?", "statuses": ["missing"] }],
  "check": { "…": "the complete rag-fixture-check JSON report" }
}
```

Properties worth relying on:

- the fixture is copied byte for byte, and both commits must contain the same bytes — verify refuses to report otherwise;
- the baseline snapshot, the loader checkout and the PR raw corpus are only read;
- the comparison happens in a temporary repository under the system temp directory. The `commits` fields are its snapshot SHAs. The caller's Git worktree and configuration are untouched;
- an unreadable source (permissions, decoding, a directory at the path) exits 2 and never degrades to `file_missing`;
- identical snapshots still produce two commits and exit 0;
- renames are still treated as "the old path went missing" in this round.

To use the [GitHub Actions example](examples/workflows/verify-ragkit-evidence.yml):

1. Commit the baseline fixture and normalized corpus under `rag-snapshot/` on the base branch, the trusted loader under `src/ragkit/`, and the documents under `data/raw/`.
2. Set two repository variables (Settings → Secrets and variables → Actions → Variables), or edit the `env:` block of the copied file:
   - `RAG_FIXTURE_CHECK_REF` (required): a reviewed tool commit containing `verify`, as a **full 40-character SHA**. Tags, branches and short SHAs are rejected before any checkout. This is what makes the comparison reproducible and independent of what the PR ships.
   - `RAG_FIXTURE_CHECK_REPO` (optional): `owner/name` of the tool repository. Defaults to `shenjingzhonggudie/rag-fixture-check`, which is useful for forks, mirrors and pre-release acceptance.
3. Use tracked `data/raw` from the PR head, or configure a trusted step to materialize the reviewed head corpus from a pinned artifact, digest, or corpus commit. Set `RAW_DIR` to its absolute path via `GITHUB_ENV`. A plain checkout supplies only tracked data.
4. Review the uploaded JSON when the check fails. It contains baseline question text and evidence paths. Run the job where that data can be shared. Make the job a required check if it should block merges.

The example loads Python from the base checkout and gives the PR checkout only the raw corpus role. Review changes to the workflow itself as code. [GitHub Actions pull request events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request)

### Commit a snapshot and compare two versions

The full loop: import a baseline, edit the original Markdown, refresh, commit the second snapshot, then let `check` compare the two commits. In this Bash example `rag-fixture-check` is on PATH. Choose a real source file referenced by your fixture for the edit.

```bash
# 1. Build the baseline snapshot and commit it.
scratch="$(mktemp -d)"
importer="$PWD/tools/ragkit_import.py"
ragkit=/path/to/ragkit
python "$importer" --ragkit-root "$ragkit" --output "$scratch/baseline" || exit

cd "$scratch/baseline" || exit
git init -b main || exit
git add .rag-fixtures.json corpus
git -c user.name=Demo -c user.email=demo@example.invalid commit -m "RAGKit snapshot"

# 2. Baseline: both sides are the same commit, so everything must pass.
rag-fixture-check check --repo . --base HEAD --head HEAD
# exit 0, intact = number of anchors

# 3. Edit the ORIGINAL Markdown inside the RAGKit checkout.
# Example path. Replace it with a document from your own fixture.
"${EDITOR:-vi}" "$ragkit/data/raw/docs/refund.md"

# 4. Rebuild only the corpus; the fixture stays frozen.
python "$importer" refresh \
  --ragkit-root "$ragkit" \
  --baseline "$scratch/baseline" \
  --output "$scratch/refreshed" || exit

# 5. Commit the refreshed snapshot as the second version.
git rm -r -- corpus || exit
cp -r "$scratch/refreshed/corpus" . || exit
cp "$scratch/refreshed/refresh-summary.json" . || exit
git add -A -- corpus refresh-summary.json
git -c user.name=Demo -c user.email=demo@example.invalid commit -m "refresh after the docs change"

# 6. Compare the two versions.
rag-fixture-check check --repo . --base HEAD~1 --head HEAD
# Exit 1 if any anchors need review.
```

A typical result after one rewritten page and one deleted page:

```text
CASE refund-30
  [missing]         corpus/docs/refund.md (last seen at line 431)  matches=0 selected=0  quote not found in file

CASE autoscaling
  [file_missing]    corpus/docs/autoscaling.md (last seen at line 58)  matches=0 selected=0  path is not present in the head commit

SUMMARY
  cases=3 evidence=3
  intact=1 relocated=0 missing=1 ambiguous=0 context_changed=0 file_missing=1 unreadable=0
VERDICT: review needed (2 anchor(s) are not intact)
```

`check` reads committed trees only, so the worktree state is irrelevant. Add `--format github` for PR annotations, or `--format json` to archive the report.

For a source PR, pass `--raw-dir /path/to/pr-checkout/data/raw` and point `--ragkit-root` at the trusted checkout with the loader used to create the baseline. The bundled workflow checks committed fixtures and documents. For generated snapshots, use a text or JSON report to review the generated `corpus/` paths.

**Acceptance numbers.** The pilot run against the reference RAGKit corpus imported 246 cases and 589 evidence anchors, and its baseline check was fully intact (589/589). After rewriting one source page and deleting another, `refresh` rebuilt 78 documents, omitted the deleted one, kept the fixture byte-identical across both commits, and `check` exited 1 with 586 intact, 1 `missing` and 2 `file_missing`. Those numbers describe that corpus; nothing in the importer is tuned to them or hard-codes them.

## What the test suite covers

Integration tests run against **real temporary Git repositories** (no mocks), with isolated Git config and deterministic identities:

- refund window 30 → 14 days → `missing`, exit 1
- quote moved within the same file → `relocated`, exit 0, old/new lines reported
- file renamed (`git mv`), fixture untouched → `relocated` via rename
- file renamed and fixture path updated → `relocated`, old path preserved
- heading changed under a `prefix` selector → `context_changed`, exit 1
- duplicated quote with no selector → `ambiguous`, exit 1, no line chosen
- duplicated quote disambiguated by `prefix` → `intact`
- Chinese filename and filename with a space → `intact`
- CRLF document with LF fixture → `intact`
- dirty worktree (uncommitted edits + untracked file) → ignored, `intact`
- added case and removed case → reported, exit 0, removed case lists its id and evidence count
- deleted evidence file → `file_missing`, exit 1
- symlink entry in the tree → `unreadable`, exit 1
- multiple quotes in one file → judged independently
- nested `--repo` path → root resolved via `git rev-parse --show-toplevel`
- `HEAD~1` / `HEAD` commit-ish → pinned to full SHAs in the report
- JSON output → byte-identical across runs, `schemaVersion: 1`
- GitHub output → annotations only, `error` for failures
- error paths → exit 2 for missing repo, non-repo, unknown revision, flag injection, unknown flag, missing fixture, malformed fixture, invalid path, empty quote
- shallow clone → exit 2 with a `fetch-depth` hint

Unit tests cover selector semantics, overlapping occurrences, line arithmetic, path validation, revision validation and workflow-command escaping.

The Python importer has its own synthetic-data suite — no RAGKit installation and no real evaluation set involved:

- valid cases imported; no-answer, needs-review and evidence-less cases counted and skipped
- duplicate quote disambiguated at the annotated occurrence, verified by re-matching the generated selector
- quote that does not match its annotated span → problem with case id and path, nothing written
- duplicate quote with identical 256-character context → reported as unresolvable
- manifest qrels/corpus hash mismatches, uncovered documents, unsafe `../` paths
- output conflict without `--force`, and `--force` clearing stale snapshot files
- a fake `ragkit` package under `<root>/src` proving `load_file` and `normalize_text` are really imported from `--ragkit-root`
- byte-identical fixture and summary across runs

`refresh` adds its own synthetic-data suite, including a real-Git end-to-end run:

- fixture copied byte for byte; ids, quotes and selectors unchanged
- rewritten quote still refreshes, and the rebuilt corpus shows the new text
- deleted source omitted from the new snapshot and listed in `missingPaths`
- renamed source treated as a missing old path
- decode failure and a directory at the source path → exit 2, nothing written
- output equal to or inside the baseline → refused; `--force` only replaces a prior refresh snapshot
- custom `--corpus-prefix` preserved; the annotation manifest deliberately not verified
- end-to-end: commit the baseline snapshot, refresh after rewriting one page and deleting another, commit again, then assert the fixture is byte-identical across commits and that `check` exits 0 without changes and 1 with the expected case ids

```bash
go test ./...
python -m unittest discover -s tools/tests
go run ./examples/demo
```

`verify` is covered end to end as well:

- unchanged documents → exit 0, empty `affected`, two commits with identical fixture bytes
- rewrite the original Markdown → exit 1, correct case id with `missing`, full report on disk
- delete a source file → exit 1, `file_missing` plus `corpus.missingPaths`
- unreadable source → exit 2, no report written
- missing checker or raw directory, and a report path inside the baseline → exit 2
- the baseline snapshot is byte-identical before and after a failing verify

The end-to-end cases need a `rag-fixture-check` binary: they use `RAG_FIXTURE_CHECK_BIN`, then `rag-fixture-check` on `PATH`, then `go build`. Without any of them those cases skip with an explicit reason.

## Boundaries and non-goals

- **Anchoring only.** The tool proves a quote still exists where the fixture says it does. It cannot tell you whether the answer is still correct, whether the case is still worth keeping, or whether the retrieval pipeline now ranks the chunk differently.
- **Literal matching only.** No semantic similarity, no embeddings, no fuzzy thresholds, no regex. Two spaces vs one space is a change; curly quotes vs straight quotes is a change. That is the point: false positives cost a minute, false negatives cost trust.
- **No cross-repository evidence.** Every path resolves inside the `--repo` worktree.
- **No generation or mutation.** It never writes to the repository, never edits fixtures, never stages files.
- **Rename detection has Git's limits.** Very large diffs can exceed `diff.renameLimit`, in which case Git reports delete+add and the tool reports `file_missing` rather than guessing.
- **Size limits are hard.** A 5 MiB fixture or 5001 cases is an input error (exit 2); an evidence file above 8 MiB is reported as `unreadable` (exit 1). Nothing is ever truncated silently.
- **Shallow repositories are rejected for the base side**, with a hint, instead of reporting bogus diffs.
- **The report is not a merge gate by itself.** It is one signal; pair it with your normal review and evaluation runs.

## Security model

- **No shell.** `git` is invoked with an explicit argument vector; nothing is interpolated into a command string.
- **No arbitrary revisions.** `--base`/`--head` reject leading `-`, whitespace and control characters, then are resolved with `rev-parse --verify <rev>^{commit}` and re-validated as a full hex object id before use.
- **No path injection.** Every path argument is preceded by `--`, and paths are validated as repo-relative before they reach Git.
- **No symlink or submodule traversal.** Only mode `100644`/`100755` blobs are read; `120000` and `160000` are refused. Contents come from `cat-file blob`, never from the filesystem.
- **Read-only.** Only `rev-parse`, `ls-tree`, `cat-file` and `diff` are executed. `GIT_OPTIONAL_LOCKS=0` and `GIT_TERMINAL_PROMPT=0` are set so the tool cannot block or take index locks.
- **Bounded memory.** Fixture, blob and command output sizes are capped.
- **Report injection is escaped.** Control characters are stripped in text output; `%`, CR and LF are escaped in GitHub annotations.
- **Untrusted fixture data is data.** Case ids and questions are only ever printed, never executed.

## Development

```bash
gofmt -w .          # the tree is kept gofmt-formatted
go build ./...
go vet ./...
go test ./...       # integration tests need git in PATH
python -m unittest discover -s tools/tests
go run ./examples/demo
```

Layout:

```text
main.go                     CLI entry point, nothing else
internal/app/               flag parsing, exit codes, dispatch
internal/fixture/           fixture schema, strict parsing, path rules
internal/gitx/              safe read-only git wrapper
internal/match/             literal + selector matching
internal/check/             orchestration, baseline comparison, summary
internal/report/            text / json / github rendering
tools/ragkit_import.py      RAGKit evaluation set → fixture + normalized snapshot (import / refresh)
tools/tests/                synthetic-data tests for the importer and the refresher
examples/demo/              end-to-end demo on a throwaway repo
examples/workflows/         copy-paste PR workflow
schema/                     JSON Schema for fixtures and reports
```

CI runs `go vet`, `go build` and `go test ./...` on Linux, macOS and Windows against Go 1.22, 1.23 and 1.24 (see `.github/workflows/ci.yml`).

## License

MIT — see [LICENSE](LICENSE).
