# cortex-skills

给 Agent 用的 skill，配合 Cortex App 使用。人在 App 里阅读 Git 仓库中的 Markdown 和 HTML 文档，选中文字后可以写批注（给自己看）或评论（交给 Agent），Agent 用这里的 skill 处理评论。

| skill | 用途 |
| --- | --- |
| `cortex-comments` | 处理文档上的评论：改正文或回答问题，写 `resolve` / `needs_input` 事件。不读人的批注 |

评论存在知识库仓库的 `.cortex/comments/` 下，批注存在 `.cortex/annotations/` 下，每个事件一个 JSON 文件。`skills/cortex-comments/scripts/cortex_ann.py` 是协议 v0.2 的参考实现，只依赖 Python 3 标准库和 git。

0.3.0 起 skill 改名为 `cortex-comments`，处理的是 `.cortex/comments/` 下的评论。手动安装过 `cortex-annotations` 的，删掉旧的软链，按下面的命令重新链接。

## 安装

Claude Code：

```text
/plugin marketplace add danielzhou82/cortex-skills
/plugin install cortex@cortex-skills
```

手动安装，适用于 Claude Code 和 Codex：

```bash
git clone https://github.com/danielzhou82/cortex-skills.git ~/.local/share/cortex-skills
ln -s ~/.local/share/cortex-skills/skills/cortex-comments ~/.claude/skills/cortex-comments
ln -s ~/.local/share/cortex-skills/skills/cortex-comments ~/.codex/skills/cortex-comments
```

更新时在 clone 目录里 `git pull`。

## 使用

在知识库仓库里对 Agent 说：

```text
处理 notes/sync-design.md 上的评论
```

Agent 是否 pull、commit、push，由仓库自己的同步方式决定，详见 `SKILL.md` 第 2 节。

本仓库由私有的 cortex-app 仓库导出，请不要直接在这里修改。
