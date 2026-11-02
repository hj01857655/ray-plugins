---
description: 把一条事实写入 ZCode 内置记忆（frontmatter 文件 + MEMORY.md 索引行）
argument-hint: <要记住的内容> [--project] [--type user|feedback|project|reference]
---

把以下内容存入内置记忆：**$ARGUMENTS**

- 参数含 `--project` → scope=project，否则 global；`--type` 缺省 project
- 用 MCP `memory_save`（自动拒密、维护索引行、slug 命名）
- 向用户确认：已存（附 name、scope、路径），重要内容顺手 pin（CLI `pin <name>`）
- 保存前先 `memory_search` 查重，更新已有文件优先于新建
