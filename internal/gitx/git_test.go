package gitx

import (
	"errors"
	"io"
	"strings"
	"testing"
)

func TestOutputBufferStopsAtLimit(t *testing.T) {
	t.Parallel()
	buffer := &outputBuffer{limit: 4}
	n, err := io.Copy(buffer, strings.NewReader("abcdef"))
	if !errors.Is(err, io.ErrShortWrite) || n != 4 || buffer.data.String() != "abcd" || !buffer.overflow {
		t.Fatalf("copy = %d, %v; buffer = %q, overflow = %v", n, err, buffer.data.String(), buffer.overflow)
	}
}

func TestValidateRevision(t *testing.T) {
	t.Parallel()
	valid := []string{"HEAD", "HEAD~3", "origin/main", "v1.2.3", "abc1234", "main^{commit}"}
	for _, rev := range valid {
		if err := ValidateRevision(rev); err != nil {
			t.Errorf("ValidateRevision(%q) = %v, want nil", rev, err)
		}
	}

	invalid := map[string]string{
		"":                       "empty",
		"--upload-pack=/tmp/x":   "'-'",
		"-c":                     "'-'",
		"HEAD..HEAD":             "", // 语法上允许，由 rev-parse 决定成败
		"two words":              "whitespace",
		"line\nbreak":            "whitespace or control",
		strings.Repeat("a", 300): "longer than",
	}
	for rev, want := range invalid {
		err := ValidateRevision(rev)
		if want == "" {
			if err != nil {
				t.Errorf("ValidateRevision(%q) = %v, want nil", rev, err)
			}
			continue
		}
		if err == nil {
			t.Errorf("ValidateRevision(%q) = nil, want an error", rev)
			continue
		}
		if !strings.Contains(err.Error(), want) {
			t.Errorf("ValidateRevision(%q) = %v, want it to mention %q", rev, err, want)
		}
	}
}

func TestEntryProblem(t *testing.T) {
	t.Parallel()
	ok := []Entry{
		{Mode: "100644", Type: "blob"},
		{Mode: "100755", Type: "blob"},
	}
	for _, entry := range ok {
		if err := entryProblem(entry); err != nil {
			t.Errorf("entryProblem(%+v) = %v, want nil", entry, err)
		}
	}

	bad := []Entry{
		{Mode: "120000", Type: "blob"},   // symlink
		{Mode: "160000", Type: "commit"}, // submodule
		{Mode: "040000", Type: "tree"},   // directory
		{Mode: "100644", Type: "tree"},   // inconsistent record
	}
	for _, entry := range bad {
		err := entryProblem(entry)
		if err == nil {
			t.Errorf("entryProblem(%+v) = nil, want an error", entry)
			continue
		}
		if !errors.Is(err, ErrNotRegularFile) {
			t.Errorf("entryProblem(%+v) = %v, want ErrNotRegularFile", entry, err)
		}
	}
}

func TestDescribeMode(t *testing.T) {
	t.Parallel()
	cases := map[string]string{
		"120000": "symlink",
		"160000": "submodule",
		"040000": "directory",
		"100644": "regular file",
		"100755": "regular file",
	}
	for mode, want := range cases {
		if got := describeMode(Entry{Mode: mode, Type: "blob"}); !strings.Contains(got, want) {
			t.Errorf("describeMode(%s) = %q, want it to mention %q", mode, got, want)
		}
	}
}

func TestShortSHAAndFirstLine(t *testing.T) {
	t.Parallel()
	full := "0123456789abcdef0123456789abcdef01234567"
	if got := shortSHA(full); got != full[:12] {
		t.Fatalf("shortSHA = %q, want %q", got, full[:12])
	}
	if got := shortSHA("HEAD"); got != "HEAD" {
		t.Fatalf("shortSHA(HEAD) = %q, want HEAD", got)
	}
	if got := firstLine("\n\n fatal: boom \nmore\n"); got != "fatal: boom" {
		t.Fatalf("firstLine = %q, want %q", got, "fatal: boom")
	}
	if got := firstLine("   \n"); got != "no details" {
		t.Fatalf("firstLine(empty) = %q, want %q", got, "no details")
	}
}
