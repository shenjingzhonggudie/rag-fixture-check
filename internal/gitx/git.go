// Package gitx 封装对 git 的只读调用。
//
// 职责边界：所有 Git 交互都集中在这里，调用方只看到"已解析的提交 SHA""tree
// 里某个路径的 blob 内容""base→head 的重命名映射"。本包不解析 fixture，也不
// 做文本匹配。
//
// 安全承诺：
//   - 永远用参数列表调用 git，不经过 shell；
//   - 所有路径参数前都加 "--"，阻断路径被当作选项解析；
//   - revision 必须是 -- 之前通过白名单校验的单个参数，且最终落到一个完整 SHA；
//   - 只读子命令（rev-parse/ls-tree/cat-file/diff），不写工作树与索引。
package gitx

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

// 输出与大小限额。
const (
	// MaxBlobBytes 限制单个证据文件的读取大小。
	MaxBlobBytes int64 = 8 << 20

	// MaxCommandOutputBytes 限制单次 git 调用的标准输出，防止异常仓库刷爆内存。
	MaxCommandOutputBytes = 64 << 20

	// MaxRevisionBytes 限制 revision 参数长度。
	MaxRevisionBytes = 256
)

// 明确的错误分类，便于上层给出可操作的提示。
var (
	// ErrNotRepository 表示 --repo 指向的目录不在 Git 仓库内。
	ErrNotRepository = errors.New("not a git repository")

	// ErrNotRegularFile 表示路径在 tree 中存在，但不是普通文件（符号链接、子模块或目录）。
	ErrNotRegularFile = errors.New("not a regular file")
)

// objectIDRE 校验 git 返回的对象 id 只含十六进制字符，长度符合 SHA-1 或 SHA-256。
var objectIDRE = regexp.MustCompile(`^(?:[0-9a-f]{40}|[0-9a-f]{64})$`)

// Repo 是一个已定位到工作树根目录的仓库句柄。
//
// Notes:
//
//	Root 是绝对路径；同一句柄的所有命令都以 Root 为工作目录执行，因此调用方
//	在哪个子目录启动进程都不影响结果（nested cwd 场景）。
type Repo struct {
	Root string

	env            []string
	shallowChecked bool
	shallow        bool
}

// Entry 是 ls-tree 返回的一个 tree 条目。
type Entry struct {
	Mode string
	Type string
	OID  string
	Size int64
}

// Open 定位仓库根目录并返回句柄。
//
// Args:
//
//	dir: --repo 的原始值，可以是仓库内任意子目录。
//
// Returns:
//
//	成功时 Root 为绝对路径的仓库根；失败返回 nil 与可读错误。
//
// Notes:
//
//	目录不存在、不是目录、不在仓库内都会失败并保留原始输入，方便用户对照。
//	本函数不修改任何 Git 配置，也不依赖当前进程的工作目录。
func Open(dir string) (*Repo, error) {
	if strings.TrimSpace(dir) == "" {
		return nil, errors.New("--repo is required")
	}
	abs, err := filepath.Abs(dir)
	if err != nil {
		return nil, fmt.Errorf("resolve repo path %q: %w", dir, err)
	}
	info, err := os.Stat(abs)
	if err != nil {
		return nil, fmt.Errorf("repo path %q: %w", dir, err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("repo path %q is not a directory", dir)
	}

	env := gitEnv()
	out, stderrText, err := execGit(abs, env, "rev-parse", "--show-toplevel")
	if err != nil {
		if strings.Contains(stderrText, "not a git repository") {
			return nil, fmt.Errorf("%w: %q", ErrNotRepository, dir)
		}
		return nil, fmt.Errorf("git rev-parse --show-toplevel in %q: %s", dir, firstLine(stderrText))
	}
	root := strings.TrimSuffix(strings.TrimSuffix(string(out), "\n"), "\r")
	if root == "" {
		return nil, fmt.Errorf("%w: %q", ErrNotRepository, dir)
	}
	return &Repo{Root: filepath.Clean(root), env: env}, nil
}

// ValidateRevision 校验一个 commit-ish 参数是否可以安全交给 git。
//
// Args:
//
//	rev: 用户通过 --base/--head 传入的 revision。
//
// Returns:
//
//	合法返回 nil；否则返回说明违规点的错误。
//
// Notes:
//
//	拒绝空串、超长、以 '-' 开头（阻断 --upload-pack=... 这类选项注入）、
//	含空白或控制字符的输入。允许 HEAD~3、origin/main、v1.2.3 这类正常语法，
//	它们最终都会被 rev-parse 固定成一个完整 SHA。
func ValidateRevision(rev string) error {
	switch {
	case rev == "":
		return errors.New("revision is empty")
	case len(rev) > MaxRevisionBytes:
		return fmt.Errorf("revision is longer than %d bytes", MaxRevisionBytes)
	case strings.HasPrefix(rev, "-"):
		return fmt.Errorf("revision %q must not start with '-'", rev)
	}
	for _, r := range rev {
		if r <= 0x20 || r == 0x7f {
			return fmt.Errorf("revision %q contains whitespace or control characters", rev)
		}
	}
	return nil
}

// ResolveCommit 把 commit-ish 固定成一个完整提交 SHA。
//
// Args:
//
//	rev: 已经通过 ValidateRevision 的 revision。
//
// Returns:
//
//	40 或 64 位十六进制 SHA；无法解析时返回错误。
//
// Notes:
//
//	解析命令是 rev-parse --verify --quiet <rev>^{commit}，因此 tag、分支、
//	相对引用都会被剥到提交对象。仓库是浅克隆且目标提交缺失时，错误信息会
//	附带 fetch-depth 提示，这是 CI 里最常见的失败原因。
func (r *Repo) ResolveCommit(rev string) (string, error) {
	if err := ValidateRevision(rev); err != nil {
		return "", err
	}
	out, _, err := r.exec("rev-parse", "--verify", "--quiet", "--end-of-options", rev+"^{commit}")
	if err != nil {
		msg := fmt.Sprintf("cannot resolve %q to a commit", rev)
		if r.isShallow() {
			msg += "; this repository is shallow, so older commits may be missing" +
				" (use git fetch --unshallow, or actions/checkout with fetch-depth: 0)"
		}
		return "", errors.New(msg)
	}
	sha := strings.TrimSpace(string(out))
	if !objectIDRE.MatchString(sha) {
		return "", fmt.Errorf("git returned unexpected object id %q for %q", sha, rev)
	}
	return sha, nil
}

// Lookup 在指定提交的 tree 中查找一个路径。
//
// Args:
//
//	commit: 完整提交 SHA。
//	path: 仓库相对路径，调用方需先用 fixture.ValidatePath 校验。
//
// Returns:
//
//	entry 为条目信息；found 表示该路径是否存在于 tree 中；err 表示 Git 调用错误。
//
// Notes:
//
//	只查 tree，不查工作树，因此未提交的改动、被删除的文件、脏工作树都不会
//	影响结果。路径精确比较，不做大小写折叠。
func (r *Repo) Lookup(commit, path string) (Entry, bool, error) {
	out, stderrText, err := r.exec("--literal-pathspecs", "ls-tree", "-z", "-l", commit, "--", path)
	if err != nil {
		return Entry{}, false, fmt.Errorf("git ls-tree %s -- %q: %s", shortSHA(commit), path, firstLine(stderrText))
	}
	for _, record := range bytes.Split(out, []byte{0}) {
		if len(record) == 0 {
			continue
		}
		tab := bytes.IndexByte(record, '\t')
		if tab < 0 {
			continue
		}
		if string(record[tab+1:]) != path {
			continue
		}
		fields := strings.Fields(string(record[:tab]))
		if len(fields) < 4 {
			return Entry{}, false, fmt.Errorf("unexpected ls-tree record for %q", path)
		}
		size, perr := strconv.ParseInt(fields[3], 10, 64)
		if perr != nil {
			size = -1
		}
		return Entry{Mode: fields[0], Type: fields[1], OID: fields[2], Size: size}, true, nil
	}
	return Entry{}, false, nil
}

// ReadFile 读取提交中某个路径的普通文件内容。
//
// Args:
//
//	commit: 完整提交 SHA。
//	path: 仓库相对路径。
//	max: 允许的最大字节数，通常传 MaxBlobBytes 或 fixture.MaxDocumentBytes。
//
// Returns:
//
//	data 为文件内容；found 表示路径是否在 tree 中；err 表示读取失败。
//
// Notes:
//
//	只接受 mode 100644/100755 的 blob。符号链接（120000）、子模块（160000）、
//	目录（040000）一律返回 ErrNotRegularFile，绝不跟随链接去读目标内容。
//	文件超过 max 时返回错误而不是截断。
func (r *Repo) ReadFile(commit, path string, max int64) ([]byte, Entry, bool, error) {
	entry, found, err := r.Lookup(commit, path)
	if err != nil || !found {
		return nil, entry, found, err
	}
	if err := entryProblem(entry); err != nil {
		return nil, entry, true, fmt.Errorf("%w: %q is %s", err, path, describeMode(entry))
	}
	if entry.Size > max {
		return nil, entry, true, fmt.Errorf("file %q is %d bytes, exceeds limit %d", path, entry.Size, max)
	}
	data, err := r.Blob(entry.OID, max)
	if err != nil {
		return nil, entry, true, err
	}
	return data, entry, true, nil
}

// Blob 按对象 id 读取 blob 内容。
//
// Args:
//
//	oid: ls-tree 返回的对象 id，必须是纯十六进制。
//	max: 允许的最大字节数。
//
// Returns:
//
//	blob 原始内容；失败返回错误。
//
// Notes:
//
//	对象 id 先做正则校验再传给 git，避免调用方拼进任意 revision 语法。
func (r *Repo) Blob(oid string, max int64) ([]byte, error) {
	if !objectIDRE.MatchString(oid) {
		return nil, fmt.Errorf("refusing to read non-hex object id %q", oid)
	}
	out, stderrText, err := r.exec("cat-file", "blob", oid)
	if err != nil {
		return nil, fmt.Errorf("git cat-file blob %s: %s", shortSHA(oid), firstLine(stderrText))
	}
	if int64(len(out)) > max {
		return nil, fmt.Errorf("blob %s is %d bytes, exceeds limit %d", shortSHA(oid), len(out), max)
	}
	return out, nil
}

// Renames 记录 base→head 之间被 Git 真实识别的重命名。
//
// Notes:
//
//	只有 diff 状态为 R（rename）的条目会进入映射；复制（C）、删除后新增都不算。
//	同一旧路径出现多次冲突时该路径被视为不可用，宁可报 file_missing 也不猜。
type Renames struct {
	forward  map[string]string
	conflict map[string]bool
}

// Target 返回旧路径被重命名后的唯一新路径。
//
// Args:
//
//	old: base 提交中的仓库相对路径。
//
// Returns:
//
//	唯一识别到新路径时返回 (新路径, true)；否则返回 ("", false)。
//
// Notes:
//
//	nil 接收者是安全的，永远返回 false。
func (m *Renames) Target(old string) (string, bool) {
	if m == nil || m.conflict[old] {
		return "", false
	}
	target, ok := m.forward[old]
	return target, ok
}

// DetectRenames 计算 base→head 的重命名映射。
//
// Args:
//
//	base: base 提交 SHA。
//	head: head 提交 SHA。
//
// Returns:
//
//	重命名映射；Git 调用失败时返回 nil 与错误。
//
// Notes:
//
//	使用 diff --name-status -z -M，NUL 分隔保证中文与空格路径不被转义。
//	重命名检测受 Git 自身的 diff.renameLimit 约束；文件数量极大时 Git 可能
//	把重命名降级成"删除+新增"，此时工具会如实报 file_missing，不会伪造映射。
func (r *Repo) DetectRenames(base, head string) (*Renames, error) {
	out, stderrText, err := r.exec("diff", "--name-status", "-z", "-M",
		"--no-ext-diff", "--no-textconv", base, head, "--")
	if err != nil {
		return nil, fmt.Errorf("git diff --name-status %s..%s: %s", shortSHA(base), shortSHA(head), firstLine(stderrText))
	}
	renames := &Renames{forward: make(map[string]string), conflict: make(map[string]bool)}
	fields := strings.Split(string(out), "\x00")
	for i := 0; i < len(fields); {
		status := fields[i]
		i++
		if status == "" {
			continue
		}
		switch status[0] {
		case 'R', 'C':
			if i+1 >= len(fields) {
				return nil, errors.New("malformed git diff output: truncated rename record")
			}
			oldPath, newPath := fields[i], fields[i+1]
			i += 2
			if status[0] != 'R' {
				continue
			}
			if prev, ok := renames.forward[oldPath]; ok && prev != newPath {
				renames.conflict[oldPath] = true
				continue
			}
			renames.forward[oldPath] = newPath
		default:
			i++
		}
	}
	return renames, nil
}

// exec 在仓库根目录执行只读 git 命令。
func (r *Repo) exec(args ...string) ([]byte, string, error) {
	return execGit(r.Root, r.env, args...)
}

// isShallow 判断仓库是否为浅克隆，结果在句柄内缓存。
func (r *Repo) isShallow() bool {
	if r.shallowChecked {
		return r.shallow
	}
	r.shallowChecked = true
	out, _, err := r.exec("rev-parse", "--is-shallow-repository")
	if err != nil {
		return false
	}
	r.shallow = strings.TrimSpace(string(out)) == "true"
	return r.shallow
}

// execGit 以给定工作目录与环境变量执行 git。
//
// Notes:
//
//	输出超过 MaxCommandOutputBytes 时直接报错；stderr 只用于错误信息，
//	不会进入正常输出，避免把 Git 的提示混进 JSON。
func execGit(dir string, env []string, args ...string) ([]byte, string, error) {
	cmd := exec.Command("git", append([]string{"--no-replace-objects"}, args...)...)
	cmd.Dir = dir
	cmd.Env = env
	stdout := &outputBuffer{limit: MaxCommandOutputBytes}
	stderr := &outputBuffer{limit: 64 << 10}
	cmd.Stdout = stdout
	cmd.Stderr = stderr
	err := cmd.Run()
	if stdout.overflow {
		err = fmt.Errorf("git output exceeds %d bytes", MaxCommandOutputBytes)
		return nil, err.Error(), err
	}
	return stdout.data.Bytes(), stderr.data.String(), err
}

type outputBuffer struct {
	data     bytes.Buffer
	limit    int
	overflow bool
}

func (b *outputBuffer) Write(p []byte) (int, error) {
	capacity := b.limit - b.data.Len()
	if len(p) > capacity {
		b.overflow = true
		n, _ := b.data.Write(p[:capacity])
		return n, io.ErrShortWrite
	}
	return b.data.Write(p)
}

// gitEnv 构造 git 子进程环境：禁止交互式提示、禁止分页。
//
// Returns:
//
//	继承当前进程环境并追加覆盖项的切片。
//
// Notes:
//
//	不设置 core.* 任何行为；用户的 autocrlf 等配置保持原样，因为工具读的是
//	已提交的 blob，与工作树换行转换无关。
func gitEnv() []string {
	env := os.Environ()
	return append(env,
		"GIT_TERMINAL_PROMPT=0",
		"GIT_PAGER=cat",
		"GIT_OPTIONAL_LOCKS=0",
		"GIT_NO_LAZY_FETCH=1",
		"LC_ALL=C",
		"GIT_ASKPASS=",
	)
}

// entryProblem 判断 tree 条目能否作为证据文件读取。
//
// Args:
//
//	entry: ls-tree 返回的条目。
//
// Returns:
//
//	普通文件返回 nil；否则返回 ErrNotRegularFile 包装的错误。
//
// Notes:
//
//	只放行 mode 100644 与 100755。120000（符号链接）、160000（子模块）、
//	040000（目录）都会被拒绝。
func entryProblem(entry Entry) error {
	if entry.Type != "blob" {
		return ErrNotRegularFile
	}
	switch entry.Mode {
	case "100644", "100755":
		return nil
	default:
		return ErrNotRegularFile
	}
}

// describeMode 把 git mode 翻译成人类可读的类型说明。
func describeMode(entry Entry) string {
	switch entry.Mode {
	case "120000":
		return "a symlink"
	case "160000":
		return "a submodule"
	case "040000":
		return "a directory"
	case "100644", "100755":
		return "a regular file"
	default:
		return "mode " + entry.Mode + " (" + entry.Type + ")"
	}
}

// firstLine 取 stderr 的第一行非空内容，用于错误提示。
func firstLine(s string) string {
	for _, line := range strings.Split(s, "\n") {
		line = strings.TrimSpace(line)
		if line != "" {
			return line
		}
	}
	return "no details"
}

// shortSHA 把完整 SHA 截短用于日志；非 SHA 输入原样返回。
func shortSHA(sha string) string {
	if objectIDRE.MatchString(sha) && len(sha) > 12 {
		return sha[:12]
	}
	return sha
}
