---
name: memory-keeper
description: Use when the user states a durable fact, preference, decision, or project context to keep across sessions ("记住…", "以后都…", "remember that…"), or asks to recall, list, or delete saved memories. Writes ZCode's native memory format (one fact per file with frontmatter + MEMORY.md index) so auto-extraction, semantic recall and dream consolidation all apply; adds pin registry for deterministic SessionStart injection.
---

# Memory Keeper — 内置记忆管理器（v3 桥接）

**存储 = ZCode 内置记忆系统**（`~/.zcode/cli/memories/`），不是自建库：
- 一事实一文件：`<root>/<slug>.md`，frontmatter（name/description/metadata.type: user|feedback|project|reference）
- 索引行：`MEMORY.md` 里 `- [标题](slug.md) — hook`
- 项目根：`memories/projects/<slug>-<sha256(工作区路径小写)[:16]>/memory`；global 根：`memories/global/memory`
- 写成这个格式 ⇒ 引擎的自动抽取、语义召回、dream 整理全部生效

**memory-bank 增值三件**：MCP/命令入口（拒密+索引维护）、pin 注册表（`~/.zcode/memory-bank/pins.json`，SessionStart hook 确定性注入）、显式管理（search/forget，删除移入记忆根之外的 trash 可回溯）。

## 操作（优先 MCP）

- 保存：`memory_save{text, type?, scope?}` — type 选 user（用户偏好）/feedback（工作方式纠正）/project（非代码可推导的项目事实）/reference（外部链接）
- 检索：`memory_search{query?, scope?}`；删除：`memory_forget{name}`
- pin：CLI `python -I "<插件>/scripts/memory_bank.py" pin <name>` → hook 每会话强制注入
- MCP 不可用走 CLI：save/search/list/forget/pin/unpin/stats

## 什么值得存

非代码可推导的事实：用户拍板的决定、环境路径约定、排查出的根因、踩坑结论。
不存：仓库能查到的、一次性的、凭据（引擎自动拒存）。保存前先 search 避免重复，更新已有文件优先于新建。

## 与内置抽取的关系

引擎的 memoryExtraction 子代理会自己写记忆文件——它写的不经 memory-bank（无索引行也可能有）。
冲突时以文件内容为准；memory-bank 的 search/forget 按文件名 slug 操作，对两者一视同仁。
