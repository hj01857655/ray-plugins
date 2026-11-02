---
description: 检索内置记忆（frontmatter 文件），可带关键词
argument-hint: [关键词]
---

用 `memory_search` 检索内置记忆（`~/.zcode/cli/memories/`，global + 当前项目）。

- 有关键词「$ARGUMENTS」：按 name/description/正文过滤
- 无关键词：全部列出，mtime 倒序

输出：name | type | scope | description。pin 过的标 📌。最后提示 /forget <name> 可删除。
