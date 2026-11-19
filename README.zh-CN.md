# rag-fixture-check（中文简版）

**在文档 PR 合并之前，确定哪些 RAG 评测题引用的证据已经失效。**

单二进制 Go CLI，零第三方依赖，只做一件事：把 RAG fixture 里的每条证据引文锚回 PR head 提交的文件内容，告诉你哪条引文消失了、挪位置了、变得有歧义了，或者选择器上下文对不上了。

**它只验证证据锚定，不判断语义。** 引文是否还能支撑答案，仍然要人来判断——每种输出都会写明这一点。

完整文档见英文 [README.md](README.md)。

---

## 为什么需要

```json
{
  "id": "refund-30",
  "question": "顾客有多少天退款期？",
  "evidence": [{ "path": "docs/refund.md", "exact": "退款在 30 天内受理。" }]
}
```

文档作者把 30 天改成 14 天、重命名 `docs/refund.md`、或者改掉引文上方的标题时，评测集会继续拿着过期预期"通过"。而这类 PR 往往根本不改 fixture，diff 里看不到任何线索。

本工具在合并前把每条锚点重新解析一遍，用 base 提交解释"发生了什么变化"。

## 两分钟上手

```bash
go install github.com/shenjingzhonggudie/rag-fixture-check@latest

# 在一次性仓库里看全部状态，无需准备环境
git clone https://github.com/shenjingzhonggudie/rag-fixture-check
cd rag-fixture-check
go run ./examples/demo
```

demo 会建一个带两次提交的临时仓库，故意制造：退款期从 30 天改成 14 天、段落被插入、文件被重命名、标题被改、引文重复、页面被删除。然后打印报告并断言退出码为 `1`。

在自己的仓库里使用：

```bash
cd /path/to/your-docs-repo
# 写好 .rag-fixtures.json 并与文档一起提交
rag-fixture-check check --repo . --base HEAD~1 --head HEAD
```

第一次运行因为 `HEAD~1` 还没有 fixture，所有题会被标为 `added`（锚点仍然逐个验证）。从下个提交开始就有真实的增删改对比。

## CLI

```
rag-fixture-check check --repo <path> --base <commit-ish> --head <commit-ish> [flags]
```

| 参数 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `--repo` | 是 | — | 仓库路径，指向仓库内任意子目录都可以，工具自己找根。 |
| `--base` | 是 | — | 基线版本，解析后固定为完整 commit SHA。 |
| `--head` | 是 | — | 待检查版本，解析后固定为完整 commit SHA。 |
| `--fixture` | 否 | `.rag-fixtures.json` | 仓库相对路径；从 head 读取，同路径的 base 版本作为 baseline。 |
| `--format` | 否 | `text` | `text` / `json` / `github`。 |

退出码：

| 码 | 含义 |
| --- | --- |
| `0` | 全部锚点为 `intact` 或 `relocated`。fixture 的增删改只是信息，不算失败。 |
| `1` | 至少一条锚点需要审阅：`missing` / `ambiguous` / `context_changed` / `file_missing` / `unreadable`。 |
| `2` | 输入或 Git 错误：参数非法、版本不存在、fixture 缺失或损坏、不是仓库、blob 不可读等。 |

## Fixture 格式

```json
{
  "version": 1,
  "cases": [
    {
      "id": "refund-30",
      "question": "顾客有多少天退款期？",
      "evidence": [
        { "path": "docs/refund.md", "exact": "退款在 30 天内受理。", "prefix": "", "suffix": "" }
      ]
    }
  ]
}
```

- `path` 必须是仓库相对路径、正斜杠、不能出现 `..`/`.`/空段/反斜杠/冒号；中文和空格没问题。
- `exact` 是字面文本，匹配前只做 `\r\n → \n` 归一，不做大小写折叠、空白折叠或模糊匹配。
- `prefix`/`suffix` 是紧邻引文前后、可缺省的字面文本，用来在同文件出现多处相同引文时消歧。
- 引文出现多次且选择器无法收敛到唯一一处时，结果是 `ambiguous`——**不会偷偷取第一条**。
- JSON Schema：[`schema/rag-fixtures.schema.json`](schema/rag-fixtures.schema.json)。

## 状态

| 状态 | 含义 | 是否退出 1 |
| --- | --- | --- |
| `intact` | 引文在预期路径唯一命中，选择器吻合，行号与 baseline 一致（或无 baseline）。 | 否 |
| `relocated` | 引文唯一命中，但行号变了、fixture 路径更新了，或真实 Git 重命名把文件挪走了。 | 否 |
| `missing` | 文件在，但字面引文在 head 里找不到。 | 是 |
| `ambiguous` | 引文出现多处，选择器无法收敛到唯一一处。 | 是 |
| `context_changed` | 引文在，但没有任何一处满足 `prefix`/`suffix`，通常是上方标题或句子被改。 | 是 |
| `file_missing` | 路径在 head tree 中不存在，且没有唯一重命名可救援。 | 是 |
| `unreadable` | 路径存在但不是普通文件（符号链接、子模块、目录）或超过大小上限。 | 是 |

`relocated` 只报告不失败：证据还在，只是位置变了。重命名只在 **Git 自己识别出唯一映射** 时才跟进，删除后新增的相似文件不会被猜成重命名。

## 输出

- `text`：给人看，含旧/新行号与匹配数，无颜色，可直接落文件。
- `json`：带 `schemaVersion`，字段稳定、输出可重复；空数组输出 `[]` 而不是 `null`。Schema 见 [`schema/report.schema.json`](schema/report.schema.json)。
- `github`：输出 `::error` / `::notice` 注解，失败直接标注到文件行；消息里的 `%`、CR、LF 会被转义，控制字符会被剥离，避免注入额外的 workflow command。

## 接入 CI

1. **先观察一周**：复制 [`examples/workflows/rag-fixture-check.yml`](examples/workflows/rag-fixture-check.yml)，检查步骤加 `continue-on-error: true`，只收集报告。
2. **改成阻塞**：去掉 `continue-on-error`，并用 `paths:` 限定到 fixture 与文档目录。
3. **明确归属**：把 `.rag-fixtures.json` 加进 `CODEOWNERS`。

工作流要点（完整版见 README）：

```yaml
permissions:
  contents: read
steps:
  - uses: actions/checkout@v4
    with:
      ref: ${{ github.event.pull_request.head.sha }}
      fetch-depth: 0          # 必须：base 提交要在本地
  - run: ./rag-fixture-check check --repo . \
      --base "${{ github.event.pull_request.base.sha }}" \
      --head "${{ github.event.pull_request.head.sha }}" \
      --format github
```

- `fetch-depth: 0` 不能省；浅克隆缺 base 提交时工具会退出 2 并提示 `fetch-depth`，不会假装检查成功。
- 两个 SHA 都取事件载荷里的固定值，分支名可能移动。
- 只需 `contents: read`，不需要任何 secret，fork PR 也能直接跑。

## 导入 RAGKit 评测集（v0.2）

`tools/ragkit_import.py` 把 RAGKit 项目转成 `check` 需要的两个输入：fixture 与规范化语料快照。它直接调用 RAGKit 的 `load_file` 与 `normalize_text`，不自己实现 Markdown 解析或空白归一。

**为什么要快照而不是直接用原始文档。** RAGKit 的标注区间是在 `normalize_text(load_file(path).text)` 上算的；直接拿原始 Markdown 当语料，front matter、HTML 注释、MDX 行、全角归一与行尾空白都会让偏移错位，检查器会把大量有效证据误报成 `missing`（参考实验里 589 条中有 459 条假缺失）。用规范化快照后 589 条全部 `intact`。

从 [v0.2.0 源码压缩包](https://github.com/shenjingzhonggudie/rag-fixture-check/archive/refs/tags/v0.2.0.zip) 解压获得脚本。在源码目录运行，使用 Python 3.11+。导入器会执行所选 RAGKit 项目里的 Python 加载器，请使用你信任的项目。

```powershell
$snapshot = Join-Path $env:TEMP ("ragkit-snapshot-" + [guid]::NewGuid())
python tools/ragkit_import.py --ragkit-root "C:\path\to\ragkit" --output $snapshot
```

`--ragkit-root` 可以指向项目根或它的 `src` 目录。可选用 `--qrels`、`--manifest`、`--raw-dir`、`--corpus-prefix` 覆盖默认布局。`--force` 只替换采用同一语料目录名的完整导入快照。

产物包含题干、原文引文和被引用文档的完整规范化正文。请根据语料授权决定提交范围。

```text
/tmp/ragkit-snapshot/
  .rag-fixtures.json     # version 1，每条证据带 path/exact/prefix/suffix
  corpus/...             # 规范化正文，按被引用的文档一一对应
  import-summary.json    # 计数、输入哈希、问题列表
```

导入器保证：

- 只收有效题：`needs_review` 与无答案题分别跳过并计数；有 answer 但没有 evidence 的题单独计数。
- `exact` 必须与 `normalized[start:end]` 逐字节一致。
- 引文重复时按标注位置生成 8→256 字符的前缀/后缀，直到唯一命中；做不到就报问题，绝不猜。
- 路径规则与 Go 侧一致（相对、正斜杠、无 `..`、无冒号、无控制字符）。
- manifest 存在时校验 qrels 与每篇文档规范化正文的 SHA-256。
- 写出前用与 Go 相同的匹配规则复验每个锚点，保证产物一定能锚定。

退出码 `0` 表示写出快照。`1` 表示题目、证据或规范化正文校验失败，返回错误且不写入。`2` 表示环境、qrels 哈希、清单格式、输出冲突或文件写入失败。

以下 PowerShell 示例使用 PATH 上的发布版 `rag-fixture-check`。第二次提交要编辑的是规范化快照。以后修改原始语料并重新导入时，需先更新 RAGKit 的标注及清单。

```powershell
git -C $snapshot init -b main
git -C $snapshot add -- .rag-fixtures.json corpus
git -C $snapshot -c user.name=Demo -c user.email=demo@example.invalid commit -m "RAGKit snapshot"
rag-fixture-check check --repo $snapshot --base HEAD --head HEAD

# 在 $snapshot\corpus 里改写一条已标注的引文，再提交并比较
git -C $snapshot -c user.name=Demo -c user.email=demo@example.invalid commit -am "Rewrite evidence"
rag-fixture-check check --repo $snapshot --base 'HEAD~1' --head HEAD
```

参考实验的验收数字是 246 题 / 589 条证据、基线全部通过；这些数字只描述那次语料，代码里没有写死任何计数。

## 边界

- **只验证锚定**：不判断答案是否仍然正确、题目是否值得保留、检索排序是否变化。
- **只做字面匹配**：没有语义相似度、没有 embedding、没有模糊阈值。两个空格 vs 一个空格算变化——这是刻意的，假阳性只花一分钟，假阴性会消耗信任。
- **不跨越仓库**：所有路径都在 `--repo` 工作树内解析。
- **只读**：只跑 `rev-parse`/`ls-tree`/`cat-file`/`diff`，不写工作树、不动索引。
- **重命名受 Git 限制**：极大 diff 可能触发 `diff.renameLimit`，此时 Git 报删除+新增，工具如实报 `file_missing`，不做猜测。
- **硬上限**：fixture 4 MiB、5000 道题 / 20000 条锚点，超限是输入错误（退出 2）；证据文件超过 8 MiB 报 `unreadable`（退出 1）。任何情况都不做静默截断。

## 开发

```bash
gofmt -w .
go build ./...
go vet ./...
go test ./...        # 集成测试需要 git 在 PATH
python -m unittest discover -s tools/tests
go run ./examples/demo
```

CI 在 Linux/macOS/Windows × Go 1.22/1.23/1.24 上跑 vet、build 与全部测试。

## 许可

MIT，见 [LICENSE](LICENSE)。
