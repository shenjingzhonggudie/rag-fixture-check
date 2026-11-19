package match_test

import (
	"testing"

	"github.com/shenjingzhonggudie/rag-fixture-check/internal/match"
)

func TestEvaluateIntact(t *testing.T) {
	t.Parallel()
	doc := match.NewDocument("docs/refund.md", []byte("# Refunds\n\nRefunds are accepted within 30 days.\n"))
	out := match.Evaluate(doc, match.NewAnchor("Refunds are accepted within 30 days.", "", ""))

	if out.Status != match.Intact {
		t.Fatalf("status = %s, want intact (%s)", out.Status, out.Detail)
	}
	if out.Line != 3 {
		t.Fatalf("line = %d, want 3", out.Line)
	}
	if out.Occurrences != 1 || out.Selected != 1 {
		t.Fatalf("occurrences/selected = %d/%d, want 1/1", out.Occurrences, out.Selected)
	}
}

func TestEvaluateMissing(t *testing.T) {
	t.Parallel()
	doc := match.NewDocument("docs/refund.md", []byte("Refunds are accepted within 14 days.\n"))
	out := match.Evaluate(doc, match.NewAnchor("Refunds are accepted within 30 days.", "", ""))

	if out.Status != match.Missing {
		t.Fatalf("status = %s, want missing", out.Status)
	}
	if out.Line != 0 {
		t.Fatalf("line = %d, want 0 for a missing quote", out.Line)
	}
}

func TestEvaluateAmbiguousNeverPicksFirst(t *testing.T) {
	t.Parallel()
	body := "Refunds are accepted within 30 days.\n\nRefunds are accepted within 30 days.\n"
	doc := match.NewDocument("docs/dup.md", []byte(body))
	out := match.Evaluate(doc, match.NewAnchor("Refunds are accepted within 30 days.", "", ""))

	if out.Status != match.Ambiguous {
		t.Fatalf("status = %s, want ambiguous", out.Status)
	}
	if out.Occurrences != 2 || out.Selected != 2 {
		t.Fatalf("occurrences/selected = %d/%d, want 2/2", out.Occurrences, out.Selected)
	}
	if out.Line != 0 {
		t.Fatalf("line = %d, want 0: an ambiguous quote must not resolve to a line", out.Line)
	}
}

func TestEvaluateSelectorDisambiguates(t *testing.T) {
	t.Parallel()
	body := "## A\n\nRefunds are accepted within 30 days.\n\n## B\n\nRefunds are accepted within 30 days.\n"
	doc := match.NewDocument("docs/dup.md", []byte(body))
	out := match.Evaluate(doc, match.NewAnchor("Refunds are accepted within 30 days.", "## B\n\n", ""))

	if out.Status != match.Intact {
		t.Fatalf("status = %s, want intact (%s)", out.Status, out.Detail)
	}
	if out.Occurrences != 2 || out.Selected != 1 {
		t.Fatalf("occurrences/selected = %d/%d, want 2/1", out.Occurrences, out.Selected)
	}
	if out.Line != 7 {
		t.Fatalf("line = %d, want 7", out.Line)
	}
}

func TestEvaluateContextChanged(t *testing.T) {
	t.Parallel()
	doc := match.NewDocument("docs/refund.md", []byte("## Returns\n\nRefunds are accepted within 30 days.\n"))
	out := match.Evaluate(doc, match.NewAnchor("Refunds are accepted within 30 days.", "## Refunds\n\n", ""))

	if out.Status != match.ContextChanged {
		t.Fatalf("status = %s, want context_changed", out.Status)
	}
	if out.Occurrences != 1 || out.Selected != 0 {
		t.Fatalf("occurrences/selected = %d/%d, want 1/0", out.Occurrences, out.Selected)
	}
}

func TestEvaluateSuffixContext(t *testing.T) {
	t.Parallel()
	body := "Refunds are accepted within 30 days.\n\nRefunds are accepted within 30 days. See terms.\n"
	doc := match.NewDocument("docs/dup.md", []byte(body))
	out := match.Evaluate(doc, match.NewAnchor("Refunds are accepted within 30 days.", "", " See terms."))

	if out.Status != match.Intact || out.Line != 3 {
		t.Fatalf("status/line = %s/%d, want intact/3", out.Status, out.Line)
	}
}

func TestEvaluateOverlappingOccurrences(t *testing.T) {
	t.Parallel()
	doc := match.NewDocument("docs/x.md", []byte("aaaa\n"))
	out := match.Evaluate(doc, match.NewAnchor("aa", "", ""))

	if out.Status != match.Ambiguous {
		t.Fatalf("status = %s, want ambiguous", out.Status)
	}
	if out.Occurrences != 3 {
		t.Fatalf("occurrences = %d, want 3 (overlaps counted)", out.Occurrences)
	}
}

func TestEvaluateEmptyQuoteIsUnreadable(t *testing.T) {
	t.Parallel()
	doc := match.NewDocument("docs/x.md", []byte("anything\n"))
	out := match.Evaluate(doc, match.NewAnchor("", "", ""))

	if out.Status != match.Unreadable {
		t.Fatalf("status = %s, want unreadable", out.Status)
	}
}

func TestNewDocumentNormalizesCRLF(t *testing.T) {
	t.Parallel()
	doc := match.NewDocument("docs/x.md", []byte("# T\r\n\r\nRefunds are accepted within 30 days.\r\n"))
	out := match.Evaluate(doc, match.NewAnchor("Refunds are accepted within 30 days.", "", ""))

	if out.Status != match.Intact {
		t.Fatalf("status = %s, want intact", out.Status)
	}
	if out.Line != 3 {
		t.Fatalf("line = %d, want 3 after CRLF normalization", out.Line)
	}
	if got := match.NewDocument("docs/x.md", []byte("a\rb\n")).Text; got != "a\rb\n" {
		t.Fatalf("lone CR must be preserved, got %q", got)
	}
}

func TestLineAtBoundaries(t *testing.T) {
	t.Parallel()
	doc := match.NewDocument("docs/x.md", []byte("abc\ndef\n"))
	cases := []struct {
		offset int
		want   int
	}{
		{0, 1},
		{2, 1},
		{3, 1},
		{4, 2},
		{7, 2},
		{8, 3},
	}
	for _, tc := range cases {
		if got := doc.LineAt(tc.offset); got != tc.want {
			t.Errorf("LineAt(%d) = %d, want %d", tc.offset, got, tc.want)
		}
	}
}
