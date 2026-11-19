package fixture_test

import (
	"strings"
	"testing"

	"github.com/shenjingzhonggudie/rag-fixture-check/internal/fixture"
)

const validDocument = `{
  "$schema": "./schema/rag-fixtures.schema.json",
  "version": 1,
  "cases": [
    {
      "id": "refund-30",
      "question": "How many days?",
      "evidence": [
        {"path": "docs/refund.md", "exact": "Refunds are accepted within 30 days.", "prefix": "", "suffix": ""}
      ]
    }
  ]
}`

func TestParseAndValidate(t *testing.T) {
	t.Parallel()
	doc, err := fixture.Parse([]byte(validDocument))
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if err := fixture.Validate(doc); err != nil {
		t.Fatalf("Validate: %v", err)
	}
	if len(doc.Cases) != 1 || doc.Cases[0].ID != "refund-30" {
		t.Fatalf("unexpected document: %+v", doc)
	}
	if doc.Schema != "./schema/rag-fixtures.schema.json" {
		t.Fatalf("schema = %q", doc.Schema)
	}
	if got := doc.Cases[0].Evidence[0].Path; got != "docs/refund.md" {
		t.Fatalf("path = %q", got)
	}
}

func TestParseRejectsUnknownFields(t *testing.T) {
	t.Parallel()
	_, err := fixture.Parse([]byte(`{"version":1,"cases":[],"typo":true}`))
	if err == nil {
		t.Fatal("expected an error for an unknown top-level field")
	}
	if !strings.Contains(err.Error(), "invalid JSON") {
		t.Fatalf("error = %v, want invalid JSON", err)
	}
}

func TestParseRejectsTrailingData(t *testing.T) {
	t.Parallel()
	_, err := fixture.Parse([]byte(validDocument + `{"version":1}`))
	if err == nil {
		t.Fatal("expected an error for trailing data")
	}
}

func TestParseRejectsEmptyDocument(t *testing.T) {
	t.Parallel()
	if _, err := fixture.Parse([]byte("  \n")); err == nil {
		t.Fatal("expected an error for an empty document")
	}
}

func TestValidateProblems(t *testing.T) {
	t.Parallel()
	doc := &fixture.Document{
		Version: 2,
		Cases: []fixture.Case{
			{ID: "", Evidence: []fixture.Evidence{}},
			{ID: "dup", Evidence: []fixture.Evidence{{Path: "../escape.md", Exact: ""}}},
			{ID: "dup", Evidence: []fixture.Evidence{{Path: "/abs.md", Exact: "x"}}},
		},
	}
	err := fixture.Validate(doc)
	if err == nil {
		t.Fatal("expected validation errors")
	}
	msg := err.Error()
	for _, want := range []string{"version", "id", "duplicate", "exact", "repo-relative"} {
		if !strings.Contains(msg, want) {
			t.Errorf("error message missing %q:\n%s", want, msg)
		}
	}
}

func TestValidateRequiresCasesArray(t *testing.T) {
	t.Parallel()
	if err := fixture.Validate(&fixture.Document{Version: 1}); err == nil {
		t.Fatal("expected an error when cases is missing")
	}
}

func TestValidatePath(t *testing.T) {
	t.Parallel()
	valid := []string{
		"docs/refund.md",
		".rag-fixtures.json",
		"docs/退款说明.md",
		"docs/refund policy.md",
		"a/b/c.txt",
	}
	for _, p := range valid {
		if err := fixture.ValidatePath(p); err != nil {
			t.Errorf("ValidatePath(%q) = %v, want nil", p, err)
		}
	}

	invalid := map[string]string{
		"":            "empty",
		"/abs.md":     "absolute",
		`docs\a.md`:   "forward slashes",
		"C:/a.md":     "':'",
		"a//b.md":     "empty segment",
		"a/":          "empty segment",
		"./a.md":      "'.' segment",
		"../a.md":     "'..' segment",
		"a/../b.md":   "'..' segment",
		"bad\x01name": "control",
	}
	for p, want := range invalid {
		err := fixture.ValidatePath(p)
		if err == nil {
			t.Errorf("ValidatePath(%q) = nil, want an error", p)
			continue
		}
		if !strings.Contains(err.Error(), want) {
			t.Errorf("ValidatePath(%q) = %v, want it to mention %q", p, err, want)
		}
	}
}

func TestEqualEvidence(t *testing.T) {
	t.Parallel()
	a := fixture.Evidence{Path: "docs/a.md", Exact: "x", Prefix: "", Suffix: ""}
	b := a
	if !fixture.EqualEvidence(a, b) {
		t.Fatal("identical evidence must compare equal")
	}
	b.Suffix = "y"
	if fixture.EqualEvidence(a, b) {
		t.Fatal("evidence differing in suffix must not compare equal")
	}
}
