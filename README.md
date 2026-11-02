# ray-plugins

ZCode 个人插件集（本地/远程双用插件市场）。

## 安装

设置 → 插件市场 → 添加插件市场 → 粘贴本仓库地址（或本地目录路径）→ 安装插件。

## 插件

### Memory Bank (memory-bank)

对接 ZCode 原生记忆系统的 Memory Bank：

- 按原生格式（一事实一文件 frontmatter + MEMORY.md 索引）写入 `~/.zcode/cli/memories/`，引擎的自动抽取、语义召回、dream 整理全部生效
- pin 注册表 + SessionStart hook：置顶记忆每会话确定性注入，不依赖语义召回
- MCP 工具 `memory_save` / `memory_search` / `memory_forget`，命令 `/remember` `/recall` `/forget`
- 凭据自动拒存、软删可回溯（trash + .bak）

要求：Python 3.8+（仅标准库）。
