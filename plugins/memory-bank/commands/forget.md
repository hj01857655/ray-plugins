---
description: 按 name 删除内置记忆文件（移入 trash 可回溯）
argument-hint: <name前缀或关键词>
---

1. `memory_search` 列出所有匹配（name | type | description），确认要删的
2. `memory_forget{name}`：文件移入 `~/.zcode/memory-bank/trash/`（时间戳前缀），MEMORY.md 索引行同步移除
3. 若该记忆在 pin 注册表里，同时 `unpin`
4. 报告删除结果与回溯路径
