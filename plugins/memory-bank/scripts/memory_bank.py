# -*- coding: utf-8 -*-
"""Memory Bank v3 引擎（纯标准库）：对接 ZCode 内置记忆系统的管理器。

内置记忆架构（源码 packages/core/src/memory/，zai-org/ZCode v3.17.x）：
  一事实一文件：<root>/<slug>.md，frontmatter(name/description/metadata.type) + [[links]]
  索引：MEMORY.md，一行一条 `- [Title](file.md) — hook`
  项目根：memories/projects/<slug>-<sha256(workspacePath)[:16]>/memory（win32 路径小写归一）
  召回：引擎扫描全目录（mtime 排序，200 文件上限）+ 辅助模型语义选取

本引擎只补内置没有的三块：
  1. 命令/MCP 写入入口（确定性保存 + 索引行维护 + 拒密）
  2. pin 注册表（pins.json）→ SessionStart hook 确定性注入（不依赖语义召回）
  3. 显式管理：search/list/forget（删除文件移入 <plugin 外> 的 trash，防 manifest 误扫）

CLI:  python memory_bank.py save|search|list|forget|pin|unpin|stats
MCP:  python memory_bank.py --mcp  （memory_save / memory_search / memory_forget）
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

STORAGE_ROOT = Path.home() / ".zcode" / "cli"                # getCliStorageRoot(): storageRoot 非 cli 则追加 cli
MEMORIES = STORAGE_ROOT / "memories"
REG_DIR = Path.home() / ".zcode" / "memory-bank"                # memory-bank 自有注册数据（在记忆根之外）
PINS = REG_DIR / "pins.json"
TRASH = REG_DIR / "trash"
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|eyJ[A-Za-z0-9_-]{20,}|[A-Fa-f0-9]{32,})")
PROTOCOL = "2024-11-05"
TYPES = ("user", "feedback", "project", "reference")


# ---------- 根解析（与 project-root.ts 对齐） ----------

def project_memory_root(workspace_path: str) -> Path:
    normalized = os.path.abspath(workspace_path)
    key = normalized.lower() if sys.platform == "win32" else normalized
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    slug = re.sub(r"[^a-z0-9._-]+", "-", os.path.basename(normalized).lower()).strip("-")[:48] or "project"
    return MEMORIES / "projects" / f"{slug}-{h}" / "memory"


def resolve_root(scope: str, workspace_path: str | None) -> Path:
    if scope == "project":
        ws = workspace_path or os.environ.get("ZCODE_PROJECT_DIR") or os.getcwd()
        return project_memory_root(ws)
    return MEMORIES / "global" / "memory"


# ---------- 文件格式 ----------

def slugify(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:4]
    if len(words) >= 2:
        return "-".join(words)[:48]
    return "mem-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def frontmatter_path(root: Path, name: str) -> Path:
    return root / f"{name}.md"


def parse_entry(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.match(r"^---\s*\n([\s\S]*?)\n---\s*\n?([\s\S]*)$", text)
    if not m:
        return None
    fm, body = m.group(1), m.group(2)
    name = re.search(r"^name:\s*(.+)$", fm, re.M)
    desc = re.search(r"^description:\s*(.+)$", fm, re.M)
    typ = re.search(r"^\s*type:\s*(\w+)", fm, re.M)
    return {"file": path, "name": (name.group(1).strip() if name else path.stem),
            "description": (desc.group(1).strip() if desc else ""),
            "type": (typ.group(1) if typ else ""), "body": body.strip(), "mtime": path.stat().st_mtime}


def entries(root: Path) -> list[dict]:
    if not root.is_dir():
        return []
    out = [e for e in (parse_entry(p) for p in root.rglob("*.md")) if e and e["file"].name != "MEMORY.md"]
    out.sort(key=lambda e: e["mtime"], reverse=True)
    return out


# ---------- 索引维护 ----------

def index_update(root: Path, name: str, title: str, hook: str, remove: bool = False) -> None:
    idx = root / "MEMORY.md"
    lines = idx.read_text(encoding="utf-8").splitlines() if idx.exists() else []
    line = f"- [{title}]({name}.md) — {hook}"
    lines = [l for l in lines if f"]({name}.md)" not in l]
    if not remove:
        lines.append(line)
    idx.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


# ---------- 操作 ----------

def save(text: str, mtype: str = "project", scope: str = "global",
         workspace_path: str | None = None, name: str | None = None,
         description: str | None = None) -> dict:
    if SECRET_RE.search(text):
        raise ValueError("内容疑似凭据/token，拒绝写入；请先脱敏")
    if mtype not in TYPES:
        raise ValueError(f"type 必须是 {'/'.join(TYPES)}")
    root = resolve_root(scope, workspace_path)
    if root.is_file():
        # 引擎约定 memory 是目录；被同名文件占据说明是旧工具写入的产物，拒绝覆盖并提示
        raise ValueError(f"记忆根被同名文件占据（引擎无法读取）：{root}；请先移走该文件")
    root.mkdir(parents=True, exist_ok=True)
    nm = name or slugify(text)
    path = frontmatter_path(root, nm)
    today = datetime.date.today().isoformat()
    desc = description or text.strip().replace("\n", " ")[:100]
    body = text.strip() if mtype in ("user", "reference") else f"{text.strip()}\n\n**Why:** {desc}\n**How to apply:** 记录于 {today}"
    path.write_text(
        f"---\nname: {nm}\ndescription: {desc}\nmetadata:\n  type: {mtype}\n---\n\n{body}\n",
        encoding="utf-8")
    index_update(root, nm, desc.split("。")[0][:40], today)
    return {"name": nm, "path": str(path), "scope": scope}


def search(query: str, scope: str = "all", workspace_path: str | None = None, limit: int = 20) -> list[dict]:
    roots = []
    if scope in ("all", "global"):
        roots.append(("global", resolve_root("global", None)))
    if scope in ("all", "project"):
        roots.append(("project", resolve_root("project", workspace_path)))
    q = query.lower()
    hits = []
    for s, root in roots:
        for e in entries(root):
            if not q or q in e["name"].lower() or q in e["description"].lower() or q in e["body"].lower():
                hits.append(e | {"scope": s})
    return hits[:limit]


def forget(name: str, scope: str = "all", workspace_path: str | None = None) -> list[str]:
    roots = [("global", resolve_root("global", None))]
    if scope == "all":
        roots.append(("project", resolve_root("project", workspace_path)))
    removed = []
    for s, root in roots:
        for e in entries(root):
            if e["name"].startswith(name):
                TRASH.mkdir(parents=True, exist_ok=True)   # trash 在记忆根之外，引擎 manifest 不会扫到
                stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                shutil.move(str(e["file"]), str(TRASH / f"{stamp}-{e['file'].name}"))
                index_update(root, e["name"], "", "", remove=True)
                removed.append(f"{s}:{e['name']}")
    return removed


def load_pins() -> dict:
    try:
        return json.loads(PINS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def set_pin(name: str, on: bool, scope: str = "global", workspace_path: str | None = None) -> dict:
    pins = load_pins()
    root = resolve_root(scope, workspace_path)
    key = str(root)
    pins.setdefault(key, {})
    if on:
        pins[key][name] = True
    else:
        pins[key].pop(name, None)
    REG_DIR.mkdir(parents=True, exist_ok=True)
    PINS.write_text(json.dumps(pins, ensure_ascii=False, indent=1), encoding="utf-8")
    return pins


def stats() -> dict:
    g = len(entries(resolve_root("global", None)))
    p = len(entries(resolve_root("project", os.environ.get("ZCODE_PROJECT_DIR") or os.getcwd())))
    pins = sum(len(v) for v in load_pins().values())
    return {"global": g, "project": p, "pinned": pins}


def render(items: list[dict]) -> str:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not items:
        return "（无记忆）"
    return "\n".join(f"{e['name']}  [{e['type'] or '?'}] ({e['scope']})  {e['description']}" for e in items)


# ---------- MCP ----------

TOOLS = [
    {"name": "memory_save", "description": "保存一条事实到 ZCode 内置记忆（一事实一文件+索引行）；type: user/feedback/project/reference",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string"}, "type": {"type": "string", "enum": list(TYPES), "default": "project"},
         "scope": {"type": "string", "enum": ["global", "project"], "default": "global"}},
         "required": ["text"]}},
    {"name": "memory_search", "description": "按关键词检索内置记忆（空关键词=列出全部）",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "scope": {"type": "string", "enum": ["all", "global", "project"], "default": "all"}},
         "required": []}},
    {"name": "memory_list", "description": "列出记忆（结构化 JSON，含正文与 pin 状态；Memory Bank 面板数据源）",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "default": ""},
         "scope": {"type": "string", "enum": ["all", "global", "project"], "default": "all"}},
         "required": []}},
    {"name": "memory_get", "description": "按 name 读取单条记忆全文",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "memory_pin", "description": "置顶/取消置顶记忆（pin 过的每会话确定性注入）",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "on": {"type": "boolean", "default": True},
         "scope": {"type": "string", "enum": ["global", "project"], "default": "global"}},
         "required": ["name"]}},
    {"name": "memory_forget", "description": "按 name 前缀删除记忆文件（移入 trash，可回溯）",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
]

# ui.surfaces 面板资源：宿主经 resources/read 取 ui://memory-bank/panel.html 渲染进沙箱 webview
PANEL_URI = "ui://memory-bank/panel.html"
PANEL_FILE = Path(__file__).resolve().parent.parent / "ui" / "panel.html"


def _is_pinned(name: str, scope: str) -> bool:
    root = str(resolve_root("global" if scope == "global" else "project",
                            os.environ.get("ZCODE_PROJECT_DIR")))
    return bool(load_pins().get(root, {}).get(name))


def _items_json(items: list[dict]) -> str:
    return json.dumps([{"name": e["name"], "description": e["description"], "type": e["type"],
                        "scope": e["scope"], "body": e["body"],
                        "pinned": _is_pinned(e["name"], e["scope"])} for e in items],
                       ensure_ascii=True)


def handle_mcp() -> None:
    data = sys.stdin.buffer.read().decode("utf-8", "replace")
    for raw in data.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except ValueError:
            continue
        rid, method = req.get("id"), req.get("method", "")
        try:
            result = _mcp_dispatch(method, req.get("params") or {})
            resp = {"jsonrpc": "2.0", "id": rid, "result": result}
        except Exception as e:  # noqa: BLE001
            resp = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}}
        sys.stdout.write(json.dumps(resp, ensure_ascii=True) + "\n")
        sys.stdout.flush()


def _mcp_dispatch(method: str, params: dict):
    if method == "initialize":
        return {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "memory-bank", "version": "0.4.0"}}
    if method == "resources/list":
        return {"resources": [{"uri": PANEL_URI, "name": "Memory Bank Panel",
                               "mimeType": "text/html"}]}
    if method == "resources/read":
        if params.get("uri") != PANEL_URI:
            raise ValueError(f"unknown resource: {params.get('uri')}")
        return {"contents": [{"uri": PANEL_URI, "mimeType": "text/html",
                              "text": PANEL_FILE.read_text(encoding="utf-8")}]}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name, args = params.get("name", ""), params.get("arguments") or {}
        if name == "memory_save":
            r = save(args["text"], args.get("type", "project"), args.get("scope", "global"))
            return {"content": [{"type": "text", "text": f"已保存 {r['name']} ({r['scope']}) → {r['path']}"}]}
        if name == "memory_search":
            items = search(args.get("query", ""), args.get("scope", "all"))
            return {"content": [{"type": "text", "text": render(items)}]}
        if name == "memory_list":
            items = search(args.get("query", ""), args.get("scope", "all"), limit=100)
            return {"content": [{"type": "text", "text": _items_json(items)}]}
        if name == "memory_get":
            found = search(args["name"], "all", limit=1)
            if not found:
                raise ValueError(f"未找到: {args['name']}")
            return {"content": [{"type": "text", "text": found[0]["body"]}]}
        if name == "memory_pin":
            set_pin(args["name"], args.get("on", True), args.get("scope", "global"))
            return {"content": [{"type": "text",
                                 "text": f"{'pinned' if args.get('on', True) else 'unpinned'} {args['name']}"}]}
        if name == "memory_forget":
            gone = forget(args["name"])
            return {"content": [{"type": "text", "text": f"已删除 {len(gone)}: {', '.join(gone)}" if gone else "未找到"}]}
        raise ValueError(f"unknown tool: {name}")
    if method == "ping":
        return {}
    raise ValueError(f"unknown method: {method}")


# ---------- CLI ----------

def main() -> int:
    if "--mcp" in sys.argv:
        handle_mcp()
        return 0
    ap = argparse.ArgumentParser(prog="memory-bank")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("save"); sp.add_argument("text"); sp.add_argument("--type", default="project", choices=TYPES); sp.add_argument("--scope", default="global", choices=["global", "project"]); sp.add_argument("--name"); sp.add_argument("--workspace")
    sp = sub.add_parser("search"); sp.add_argument("query", nargs="?", default=""); sp.add_argument("--scope", default="all", choices=["all", "global", "project"]); sp.add_argument("--limit", type=int, default=20)
    sp = sub.add_parser("list"); sp.add_argument("--scope", default="all", choices=["all", "global", "project"])
    sp = sub.add_parser("forget"); sp.add_argument("name"); sp.add_argument("--scope", default="all", choices=["all", "global", "project"])
    sp = sub.add_parser("pin"); sp.add_argument("name"); sp.add_argument("--scope", default="global", choices=["global", "project"])
    sp = sub.add_parser("unpin"); sp.add_argument("name"); sp.add_argument("--scope", default="global", choices=["global", "project"])
    sp = sub.add_parser("stats")
    a = ap.parse_args()
    try:
        if a.cmd == "save":
            print(save(a.text, a.type, a.scope, a.workspace, a.name))
        elif a.cmd == "search":
            print(render(search(a.query, a.scope, limit=a.limit)))
        elif a.cmd == "list":
            print(render(search("", a.scope)))
        elif a.cmd == "forget":
            gone = forget(a.name, a.scope)
            print(f"deleted {len(gone)}: " + ", ".join(gone) if gone else "no match")
        elif a.cmd in ("pin", "unpin"):
            set_pin(a.name, a.cmd == "pin", a.scope)
            print(f"{a.cmd}ged {a.name} ({a.scope})")
        elif a.cmd == "stats":
            print(json.dumps(stats(), ensure_ascii=False))
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
