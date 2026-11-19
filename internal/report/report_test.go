package report

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/shenjingzhonggudie/rag-fixture-check/internal/check"
	"github.com/shenjingzhonggudie/rag-fixture-check/internal/match"
)

func TestEscapeWorkflowCommands(t *testing.T) {
	t.Parallel()
	if got := escapeData("100% done\r\nnext"); got != "100%25 done%0D%0Anext" {
		t.Fatalf("escapeData = %q", got)
	}
	if got := escapeProperty("a,b:c"); got != "a%2Cb%3Ac" {
		t.Fatalf("escapeProperty = %q", got)
	}
}

func TestSanitizeAndTruncate(t *testing.T) {
	t.Parallel()
	if got := sanitize("a\nb\tc\x00d\x1b[31m"); got != "a b cd[31m" {
		t.Fatalf("sanitize = %q", got)
	}
	if got := truncate("abcdef", 3); got != "abc..." {
		t.Fatalf("truncate = %q", got)
	}
	if got := truncate("abc", 3); got != "abc" {
		t.Fatalf("truncate(exact) = %q", got)
	}
	if got := truncate("中文测试", 2); got != "中文..." {
		t.Fatalf("truncate(multibyte) = %q", got)
	}
}

func sampleResult() *check.Result {
	return &check.Result{
		Repo:             "/repo",
		Base:             "1111111111111111111111111111111111111111",
		Head:             "2222222222222222222222222222222222222222",
		FixturePath:      ".rag-fixtures.json",
		BaselinePresent:  true,
		BaselineReadable: true,
		Cases: []check.Case{
			{
				ID:       "refund-30",
				Question: "How many days?",
				Change:   check.ChangeModified,
				Evidence: []check.Anchor{
					{Index: 0, Path: "docs/refund.md", Status: match.Missing, OldPath: "docs/refund.md", OldLine: 3, Occurrences: 0, Detail: "quote not found in file"},
					{Index: 1, Path: "docs/shipping.md", Status: match.Relocated, OldPath: "docs/shipping.md", OldLine: 3, NewLine: 7, Occurrences: 1, Selected: 1, Detail: "quote moved from line 3 to line 7"},
					{Index: 2, Path: "docs/dup.md", Status: match.Ambiguous, OldLine: 1, NewLine: 1, Occurrences: 2, Selected: 2, Detail: "quote occurs 2 times"},
				},
			},
		},
		Changes: check.Changes{Added: []string{}, Modified: []string{"refund-30"}, Removed: []string{}},
		Summary: check.Summary{Cases: 1, Evidence: 3, Missing: 1, Relocated: 1, Ambiguous: 1, NeedsReview: true},
		Notes:   []string{},
	}
}

func TestTextRendersStatuses(t *testing.T) {
	t.Parallel()
	var buf bytes.Buffer
	if err := Text(&buf, sampleResult(), "0.2.0"); err != nil {
		t.Fatalf("Text: %v", err)
	}
	out := buf.String()
	for _, want := range []string{"rag-fixture-check 0.2.0", "CASE refund-30  (modified)", "[missing]", "[relocated]", "[ambiguous]", "was line 3", "VERDICT: review needed (2 anchor(s) are not intact)", "NOTE:"} {
		if !strings.Contains(out, want) {
			t.Errorf("text output missing %q:\n%s", want, out)
		}
	}
}

func TestJSONIsStableAndVersioned(t *testing.T) {
	t.Parallel()
	var first, second bytes.Buffer
	if err := JSON(&first, sampleResult(), "0.2.0"); err != nil {
		t.Fatalf("JSON: %v", err)
	}
	if err := JSON(&second, sampleResult(), "0.2.0"); err != nil {
		t.Fatalf("JSON: %v", err)
	}
	if first.String() != second.String() {
		t.Fatalf("JSON output is not deterministic:\n%s\n---\n%s", first.String(), second.String())
	}

	var payload map[string]any
	if err := json.Unmarshal(first.Bytes(), &payload); err != nil {
		t.Fatalf("decode JSON: %v", err)
	}
	if got := payload["schemaVersion"]; got != float64(SchemaVersion) {
		t.Fatalf("schemaVersion = %v, want %d", got, SchemaVersion)
	}
	changes, ok := payload["fixtureChanges"].(map[string]any)
	if !ok {
		t.Fatalf("fixtureChanges missing: %v", payload["fixtureChanges"])
	}
	if _, ok := changes["added"].([]any); !ok {
		t.Fatalf("fixtureChanges.added must be an array, got %T", changes["added"])
	}
	if _, ok := changes["removed"].([]any); !ok {
		t.Fatalf("fixtureChanges.removed must be an array, got %T", changes["removed"])
	}
	if _, ok := payload["disclaimer"].(string); !ok {
		t.Fatal("disclaimer must be a string")
	}
}

func TestGitHubAnnotations(t *testing.T) {
	t.Parallel()
	var buf bytes.Buffer
	if err := GitHub(&buf, sampleResult()); err != nil {
		t.Fatalf("GitHub: %v", err)
	}
	out := buf.String()
	for _, want := range []string{
		"::error file=docs/refund.md,title=rag-fixture-check (missing)::",
		"::notice file=docs/shipping.md,line=7,title=rag-fixture-check (relocated)::",
		"::error file=docs/dup.md,line=1,title=rag-fixture-check (ambiguous)::",
		"::notice title=rag-fixture-check (fixture)::case \"refund-30\" was modified in the fixture",
		"need review",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("github output missing %q:\n%s", want, out)
		}
	}
	for _, line := range strings.Split(strings.TrimRight(out, "\n"), "\n") {
		if !strings.HasPrefix(line, "::") {
			t.Errorf("annotation line does not start with '::': %q", line)
		}
	}
}

func TestGitHubEscapesInjectedControlCharacters(t *testing.T) {
	t.Parallel()
	res := sampleResult()
	res.Cases[0].ID = "evil\n::error::pwned"
	res.Cases[0].Evidence = res.Cases[0].Evidence[:1]
	res.Summary = check.Summary{Cases: 1, Evidence: 1, Missing: 1, NeedsReview: true}

	var buf bytes.Buffer
	if err := GitHub(&buf, res); err != nil {
		t.Fatalf("GitHub: %v", err)
	}
	out := buf.String()
	if strings.Contains(out, "\n::error::pwned") {
		t.Fatalf("control characters were passed through:\n%s", out)
	}
	if !strings.Contains(out, "evil%0A::error::pwned") {
		t.Fatalf("injected id was not escaped:\n%s", out)
	}
}

func TestGitHubTruncatesAnnotationFlood(t *testing.T) {
	t.Parallel()
	res := sampleResult()
	res.Cases = nil
	for i := 0; i < maxAnnotations+5; i++ {
		res.Cases = append(res.Cases, check.Case{
			ID:     "case",
			Change: check.ChangeUnchanged,
			Evidence: []check.Anchor{
				{Index: i, Path: "docs/x.md", Status: match.Missing},
			},
		})
	}
	res.Summary = check.Summary{Cases: len(res.Cases), Evidence: len(res.Cases), Missing: len(res.Cases), NeedsReview: true}

	var buf bytes.Buffer
	if err := GitHub(&buf, res); err != nil {
		t.Fatalf("GitHub: %v", err)
	}
	if !strings.Contains(buf.String(), "additional annotations were suppressed") {
		t.Fatalf("expected a truncation notice:\n%s", buf.String())
	}
}
