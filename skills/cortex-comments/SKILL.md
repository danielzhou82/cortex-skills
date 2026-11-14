---
name: cortex-comments
description: >-
  Process comments (requests, questions, corrections) a person left for the agent on
  Markdown or HTML documents in a Git-backed knowledge base that follows the Cortex
  annotation and comment protocol, i.e. a repo with a `.cortex/comments/` directory.
  Use when the user asks to handle, process, resolve, or reply to comments / 评论 /
  批注 on a document, or on all pending comments in such a repo. Covers finding pending
  comments, editing the document, writing resolve / needs_input events, and leaving Git
  sync to the repo's own convention. Never reads the person's private annotations.
---

# Cortex 评论处理

人在 Cortex App 里阅读文档，选中一段文字后可以留两种东西：

- **评论**：交给你处理的请求，以事件文件的形式存在仓库的 `.cortex/comments/` 下。你的任务是按评论修改文档或回答问题，再写一个事件告诉人处理结果。人下次打开 App 时会看到你的说明和正文的改动
- **批注**：人写给自己看的笔记，存在 `.cortex/annotations/` 下。批注不是给你的，不要读、不要改、不要处理，只有一种例外，见第 4 节的 `from`

用户会告诉你处理哪篇文档，比如「处理 `notes/sync.md` 上的评论」。用户说「批注」时，指的也是要你处理的评论，按本 skill 处理 `.cortex/comments/`，不要去读 `.cortex/annotations/`。用户没有指定文档、只说处理全部时，处理仓库里所有待处理的评论。不要处理用户没提到的文档。

## 1. 准备

1. 找到知识库仓库的根目录（含 `.cortex/` 的那个 Git 仓库），下文记为 `<repo>`。没有 `.cortex/comments/` 目录，说明这个仓库没有评论，告诉用户后停止。
2. 读仓库根目录的 `AGENTS.md`、`CLAUDE.md` 等说明文件，了解这个仓库怎么同步、文档有哪些写作约定。改正文时遵守这些约定。
3. 本 skill 自带参考脚本 `scripts/cortex_ann.py`（路径相对于本文件所在目录），只依赖 Python 3 标准库和 git。下文用 `ann` 代指：

   ```bash
   python3 <skill-dir>/scripts/cortex_ann.py --repo <repo> --author <agent>@<host> <命令>
   ```

   `--author` 写你是谁、在哪台机器上，比如 `claude-code@macbook`、`codex@garrick`。人在 App 里靠它区分评论是谁处理的。

## 2. 同步由仓库自己决定

你是否执行 `git pull`、`commit`、`push`，取决于这个仓库平时怎么同步，不由本 skill 决定：

- 仓库说明文件写了同步方式时，照着做
- 仓库由同步工具管理时（比如存在 `.obsidian/plugins/obsidian-git/`，或说明文件提到 obsidian-git），只修改工作区里的文件，不执行任何 git 写操作，提交和推送交给那个工具
- 以上都判断不了时，先问用户要不要 pull、处理完要不要提交和推送，得到答复前不执行 git 写操作

`git log`、`git show` 这类只读命令随时可以用。

用户说有评论、但你列不出来时，可能是仓库还没同步到最新。告诉用户这一点，不要自行 pull，除非仓库约定允许。

## 3. 列出待处理的评论

```bash
ann list --actionable --path <文档路径>    # 指定文档
ann list --actionable                     # 全部文档
```

`--path` 同时匹配文档现在的路径和评论创建时的路径，文档改过名也能找到。每条输出两行：短 ID、状态、锚点结果、路径，下一行是原文片段和评论内容。

待处理的评论同时满足两个条件：状态是 `open`，锚点结果不是 `orphaned`。其中也包括你以前写过 `needs_input`、人随后回复了的评论，人的回复会把状态从 `waiting` 改回 `open`。

去掉 `--actionable` 可以看到这篇文档上的全部评论。`waiting` 的评论在等人回复，跳过。`orphaned` 的评论原文已经找不到，见第 4 节最后一段。

## 4. 读懂每一条评论

```bash
ann show <短 ID>
```

输出是 JSON，重点看下面几项：

- `body`：人最初要你做什么，已经应用过人后来的修改
- `events`：全部事件，按时间排序。第一个是 `create`，其中 `target.selector.quote` 有人选中的原文 `exact`、前后文 `prefix` 和 `suffix`，`headings` 是所在的标题层级。后面的 `reply`、`needs_input`、`resolve`、`reopen` 是对话记录，要读完再动手
- `anchor`：原文在当前文档里的位置。`result` 为 `anchored` 表示原文还在，`start` 和 `end` 是字符位置，`text` 是这段原文。`drifted` 表示原文被改过，`text` 是现在最接近的那段，要判断评论是否仍然适用
- `from`：只在这条评论由人的一条批注转来时出现，`body` 是那条批注的内容，可以当作人对这段文字的理解来参考。这是你唯一可以读的批注，不要顺着去读其他批注

然后打开文档，读评论位置所在的整个小节，不要只看选中的那几个字。

HTML 文档的位置基于页面纯文本，不是源文件里的位置。拿 `anchor.text` 或 `exact` 去源文件里搜索，找到后直接改源文件。

`orphaned` 表示原文已经找不到。用 `git show <target.commit>:<target.path>` 读评论时的版本，判断这条评论还有没有意义：内容已经被别的改动覆盖了，就写 `resolve`，`outcome` 用 `no_change` 并说明原因；还需要处理的，按现在的文档处理。

## 5. 按评论内容处理

每条评论都是人交给你的请求。要做什么，看 `body` 加上人后来写的事件：

- `reply`：人在你处理之前补充的信息，或者对你 `needs_input` 问题的回答
- `reopen`：你上次处理过，人不满意，`body` 写的是原因。这次要针对这个原因重新处理，不要重复上次的做法

这些内容前后有冲突时，以人最后写的那条为准。比如一条评论的事件依次是：

| 事件 | 作者 | body |
| --- | --- | --- |
| `create` | 人 | 这个数字从哪里来的？ |
| `reply` | 人 | 应该在 9 月的会议纪要里 |
| `resolve` answered | Agent | 来自 2026-09 区域合规会议纪要 |
| `reopen` | 人 | 来源要写进正文，不只是回答我 |

这时要做的是在正文里补上会议纪要的链接，`outcome` 用 `changed`。

人写的意思常见的几种：

| 人写的意思 | 你该做什么 | 通常的 outcome |
| --- | --- | --- |
| 提问，比如「为什么」「依据是什么」 | 查清楚后在事件里回答；答案应该写进文档时，同时补进正文 | `answered` 或 `changed` |
| 要来源 | 找到可靠来源，在正文补链接或出处；找不到时说明找过哪里 | `changed` 或 `no_change` |
| 指出错误 | 核实后改正文；核实后发现原文没错，说明依据 | `changed` 或 `no_change` |
| 要展开或精简 | 补充论证、例子或结构；或者精简正文，不丢信息 | `changed` |
| 记一个决策或待办 | 按仓库约定记进决策记录或待办；仓库没有约定时，用 `needs_input` 问记在哪里 | `changed` |

判断不需要改时，写 `resolve`，`outcome` 用 `no_change`，在 `body` 里说明理由。看不懂人要做什么时，写 `needs_input` 问清楚，不要猜。

写进正文或回答里的事实，能联网时要查到出处再写，并在事件的 `body` 里给出来源。没法查证、只能凭已有知识回答时，在 `body` 里明说没有查证，人在手机上只看得到这段说明。

修改正文时：

- 只改评论涉及的那一段，不顺手改别的地方。附近有其他评论或批注时尽量不动它们的原文，否则它们会定位不准
- 不在正文里留下任何评论标记，比如「（已按评论修改）」
- 需要整篇重写时，先处理完这篇文档上的全部评论再重写，否则大量评论和批注会找不到原文
- 信息不够、需要人做选择、或者评论的意思有歧义时，不要猜，写 `needs_input` 提一个具体的问题

## 6. 写事件

每条评论处理完写一个事件：

```bash
ann event <短 ID> resolve --outcome changed --body "<改了什么>"
ann event <短 ID> resolve --outcome answered --body "<回答>"
ann event <短 ID> resolve --outcome no_change --body "<为什么不改>"
ann event <短 ID> needs_input --body "<要人回答的具体问题>"
```

`body` 是人在手机上读的，用文档的语言写，一到三句话说清楚结果。`changed` 要写改了什么，比如「补上了 2026-09 会议纪要的链接，数字改为 3 个区域」，不要只写「已修改」。`answered` 的 `body` 就是答案本身。

处理多条、耗时较长时，可以先对每条写 `claim`，表示正在处理。一次处理完的不需要写。

找不到 Python 时可以手写事件文件。路径是 `.cortex/comments/<评论完整 ID>/<新 ULID>.json`，内容如下，`at` 带时区：

```json
{"v": 2, "id": "<新 ULID>", "comment": "<评论完整 ID>", "type": "resolve",
 "at": "2026-10-10T21:00:00+08:00", "author": "<agent>@<host>",
 "outcome": "changed", "body": "…"}
```

## 7. 提交

只在第 2 节确认可以提交时做：

1. 把正文改动和事件文件放进同一个 commit，提交信息写 `cortex: 处理 <文档路径> 上的 <N> 条评论`，正文逐行列出每条评论的短 ID 和结果
2. push。被拒绝时 `git pull --rebase` 后重试。事件文件只会新增，rebase 不会因为它们冲突。正文冲突时停下来告诉用户，不要强推

## 8. 不要做的事

- 不读、不改 `.cortex/annotations/`，评论带 `from` 时除外，见第 4 节
- 不修改、不删除已有的事件文件，包括你自己以前写的
- 不替人写 `withdraw` 或 `reopen`
- 不处理用户没有指定的文档上的评论

## 9. 汇报

结束时告诉用户：

- 处理了几条评论，每条的短 ID 和结果
- 写了 `needs_input` 的，列出你提的问题
- 是否已提交和推送；没有提交时，说明改动留在工作区，等仓库的同步方式处理
