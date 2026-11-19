// Package match 实现"字面引文 + 相邻选择器"的确定性匹配。
//
// 职责边界：输入是已归一换行的文档文本与一条锚点，输出是状态、行号与匹配
// 计数。本包不读文件、不调 Git、不判断语义，只回答"这段字面文本在文件里
// 能不能被唯一定位"。
package match

import (
	"fmt"
	"strings"
)

// Status 是单条证据锚点的确定性判定结果。
type Status string

// 锚点状态取值。
const (
	// Intact 表示引文在文件中原位命中，选择器上下文也吻合。
	Intact Status = "intact"

	// Relocated 表示引文仍然唯一命中，但位置或路径发生了变化。
	Relocated Status = "relocated"

	// Missing 表示引文在目标文件中完全找不到。
	Missing Status = "missing"

	// Ambiguous 表示引文出现多处，选择器也无法收敛到唯一一处。
	Ambiguous Status = "ambiguous"

	// ContextChanged 表示引文存在，但 prefix/suffix 指定的上下文不再吻合。
	ContextChanged Status = "context_changed"

	// FileMissing 表示路径在 head 提交的 tree 中不存在。
	FileMissing Status = "file_missing"

	// Unreadable 表示路径存在但无法作为普通文本文件读取（符号链接、子模块、超限）。
	Unreadable Status = "unreadable"
)

// Problem 判断该状态是否需要人工审阅。
//
// Returns:
//
//	Intact 与 Relocated 返回 false，其余状态返回 true。
//
// Notes:
//
//	Relocated 表示引文仍在，只是行号或路径变了，因此不算失败；它会在报告里
//	单独计数，供 reviewer 判断是否需要同步更新行号。
func (s Status) Problem() bool {
	switch s {
	case Intact, Relocated:
		return false
	default:
		return true
	}
}

// Anchor 是一条待匹配的锚点。
//
// Notes:
//
//	Exact 必须非空；Prefix/Suffix 为空表示该侧不约束。三者都在构造时做了
//	CRLF→LF 归一，因此 fixture 里写 \r\n、文档里存 LF 也能对上。
type Anchor struct {
	Exact  string
	Prefix string
	Suffix string
}

// NewAnchor 构造锚点并对三段文本做换行归一。
//
// Args:
//
//	exact: 必须出现的字面引文。
//	prefix: 紧邻引文之前的字面文本，可缺省。
//	suffix: 紧邻引文之后的字面文本，可缺省。
//
// Returns:
//
//	归一后的 Anchor。
//
// Notes:
//
//	只归一 \r\n，不折叠连续空格、不做大小写折叠、不做 Unicode 归一。任何
//	"宽松匹配"都会让工具报告一个实际上不存在的锚点。
func NewAnchor(exact, prefix, suffix string) Anchor {
	return Anchor{
		Exact:  NormalizeNewlines(exact),
		Prefix: NormalizeNewlines(prefix),
		Suffix: NormalizeNewlines(suffix),
	}
}

// NormalizeNewlines 把 CRLF 统一成 LF。
//
// Args:
//
//	s: 原始文本。
//
// Returns:
//
//	替换过 \r\n 的文本；孤立的 \r 保持原样。
//
// Notes:
//
//	只处理 \r\n 是刻意的：孤立 \r 在老式 Mac 文本里才有意义，把它也当换行会
//	改变字节偏移，进而影响行号计算。
func NormalizeNewlines(s string) string {
	if !strings.Contains(s, "\r\n") {
		return s
	}
	return strings.ReplaceAll(s, "\r\n", "\n")
}

// Outcome 是一次匹配的完整结果。
type Outcome struct {
	Status      Status
	Occurrences int
	Selected    int
	Offset      int
	Line        int
	Detail      string
}

// Document 是一份已归一换行、预计算行首偏移的文本。
type Document struct {
	Path  string
	Text  string
	lines []uint32
}

// NewDocument 构造函数并预计算行首偏移。
//
// Args:
//
//	path: 仓库相对路径，仅用于展示。
//	raw: 文件原始字节，允许包含 CRLF。
//
// Returns:
//
//	可直接用于 Evaluate 的文档。
//
// Notes:
//
//	不做 UTF-8 校验：匹配是字节级的，非 UTF-8 文件只要引文一致也能命中。
//	行号从 1 开始，空文件的第 1 行偏移为 0。
func NewDocument(path string, raw []byte) *Document {
	text := NormalizeNewlines(string(raw))
	doc := &Document{Path: path, Text: text, lines: make([]uint32, 1, 1+strings.Count(text, "\n"))}
	for i := 0; i < len(text); i++ {
		if text[i] == '\n' {
			doc.lines = append(doc.lines, uint32(i+1))
		}
	}
	return doc
}

// LineAt 把字节偏移换算成 1 起始行号。
//
// Args:
//
//	offset: 文档内的字节偏移，必须在 [0, len(Text)] 内。
//
// Returns:
//
//	该偏移所在的行号。
//
// Notes:
//
//	使用二分查找，保证大文件下每个锚点仍是 O(log n)。
func (d *Document) LineAt(offset int) int {
	lo, hi := 0, len(d.lines)-1
	for lo < hi {
		mid := (lo + hi + 1) / 2
		if int(d.lines[mid]) <= offset {
			lo = mid
		} else {
			hi = mid - 1
		}
	}
	return lo + 1
}

// Evaluate 在文档中定位锚点。
//
// Args:
//
//	doc: 目标文档。
//	anchor: 已经归一的锚点。
//
// Returns:
//
//	Outcome 携带状态、总出现次数、通过选择器的次数、命中的行号与说明文本。
//
// Notes:
//
//	判定顺序是固定的：
//	  1. 引文 0 次出现 → Missing；
//	  2. 通过 prefix/suffix 过滤后恰好 1 处 → Intact；
//	  3. 过滤后多于 1 处 → Ambiguous（绝不默认取第一处）；
//	  4. 引文存在但过滤后为 0 处 → ContextChanged。
//	同一位置的重叠出现会被分别计数，保证歧义不会被漏掉。
func Evaluate(doc *Document, anchor Anchor) Outcome {
	if anchor.Exact == "" {
		return Outcome{Status: Unreadable, Detail: "selector has an empty exact quote"}
	}

	var out Outcome
	var first, selected int
	for start := 0; ; {
		index := strings.Index(doc.Text[start:], anchor.Exact)
		if index < 0 {
			break
		}
		offset := start + index
		if out.Occurrences == 0 {
			first = offset
		}
		out.Occurrences++
		if contextMatches(doc.Text, offset, anchor) {
			selected = offset
			out.Selected++
		}
		start = offset + 1 // Count overlapping matches.
	}
	if out.Occurrences == 0 {
		return Outcome{Status: Missing, Detail: "quote not found in file"}
	}
	switch {
	case out.Selected == 1:
		out.Status = Intact
		out.Offset = selected
		out.Line = doc.LineAt(selected)
		return out
	case out.Selected > 1:
		out.Status = Ambiguous
		out.Detail = fmt.Sprintf("quote occurs %d times and %d of them match the selector", out.Occurrences, out.Selected)
		return out
	default:
		out.Status = ContextChanged
		out.Offset = first
		out.Line = doc.LineAt(first)
		out.Detail = fmt.Sprintf("quote occurs %d times but none match the prefix/suffix context", out.Occurrences)
		return out
	}
}

// contextMatches 判断某次出现的前后是否与选择器完全吻合。
func contextMatches(text string, offset int, anchor Anchor) bool {
	start := offset - len(anchor.Prefix)
	end := offset + len(anchor.Exact) + len(anchor.Suffix)
	if start < 0 || end > len(text) {
		return false
	}
	if anchor.Prefix != "" && text[start:offset] != anchor.Prefix {
		return false
	}
	if anchor.Suffix != "" && text[offset+len(anchor.Exact):end] != anchor.Suffix {
		return false
	}
	return true
}
