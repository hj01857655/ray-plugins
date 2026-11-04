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
HISTORY = REG_DIR / "history"                                   # 编辑版本史：history/<name>.md 按时间头追加
LINK_RE = re.compile(r"\[\[([a-z0-9][a-z0-9.-]{0,47})\]\]")      # 正文 [[slug]] 实体链
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
    created = re.search(r"^created:\s*(\d{4}-\d{2}-\d{2})", fm, re.M)
    tags = re.search(r"^tags:\s*\[([^\]]*)\]", fm, re.M)
    body_created = re.search(r"记录于 (\d{4}-\d{2}-\d{2})", body)
    hits_m = re.search(r"^hits:\s*(\d+)", fm, re.M)
    last_hit_m = re.search(r"^last_hit:\s*(\d{4}-\d{2}-\d{2})", fm, re.M)
    related_m = re.search(r"^related:\s*\[([^\]]*)\]", fm, re.M)
    sup_m = re.search(r"^superseded_by:\s*(.+)$", fm, re.M)
    return {"file": path, "name": (name.group(1).strip() if name else path.stem),
            "description": (desc.group(1).strip() if desc else ""),
            "type": (typ.group(1) if typ else ""),
            "created": (created.group(1) if created else body_created.group(1) if body_created else ""),
            "tags": [x.strip().strip("'\"") for x in tags.group(1).split(",") if x.strip()] if tags else [],
            "hits": int(hits_m.group(1)) if hits_m else 0,
            "last_hit": last_hit_m.group(1) if last_hit_m else "",
            "related": [x.strip() for x in related_m.group(1).split(",") if x.strip()] if related_m else [],
            "superseded_by": sup_m.group(1).strip() if sup_m else "",
            "links": sorted(set(LINK_RE.findall(body.lower()))),
            "body": body.strip(), "mtime": path.stat().st_mtime}


def write_entry(root: Path, name: str, mtype: str, description: str, body: str,
                created: str | None = None, tags: list[str] | None = None,
                hits: int | None = None, last_hit: str | None = None,
                related: list[str] | None = None, superseded_by: str | None = None) -> Path:
    """统一写入格式：save 与 memory_edit 共用，保证 frontmatter 结构一致。"""
    today = datetime.date.today().isoformat()
    desc = " ".join(description.split())   # 描述必须单行，否则 frontmatter 结构被弄破
    tag_line = ("tags: [" + ", ".join(tags) + "]\n") if tags else ""
    heat_line = (f"hits: {hits}\n" if hits else "") + (f"last_hit: {last_hit}\n" if last_hit else "")
    rel_line = ("related: [" + ", ".join(related) + "]\n") if related else ""
    sup_line = f"superseded_by: {superseded_by}\n" if superseded_by else ""
    text = (f"---\nname: {name}\ndescription: {desc}\n"
            f"{tag_line}metadata:\n  type: {mtype}\ncreated: {created or today}\n"
            f"{heat_line}{rel_line}{sup_line}---\n\n{body.rstrip()}\n")
    path = frontmatter_path(root, name)
    path.write_text(text, encoding="utf-8")
    return path


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
         description: str | None = None, tags: list[str] | None = None) -> dict:
    if SECRET_RE.search(text):
        raise ValueError("内容疑似凭据/token，拒绝写入；请先脱敏")
    if mtype not in TYPES:
        raise ValueError(f"type 必须是 {'/'.join(TYPES)}")
    root = resolve_root(scope, workspace_path)
    if root.is_file():
        # 引擎约定 memory 是目录；被同名文件占据说明是旧工具写入的产物，拒绝覆盖并提示
        raise ValueError(f"记忆根被同名文件占据（引擎无法读取）：{root}；请先移走该文件")
    root.mkdir(parents=True, exist_ok=True)
    # 跨会话隔离①写入侧：全局条目自动带上来源话题 tag（当前工程目录名），
    # 召回侧可按话题软降权——global 大池不再是无差别可见。
    if scope == "global" and not tags:
        proj = os.environ.get("ZCODE_PROJECT_DIR") or os.getcwd()
        topic = os.path.basename(proj.replace("\\", "/").rstrip("/")).lower()
        if topic and len(topic) >= 2:
            tags = ["topic:" + topic[:40]]
    nm = name or slugify(text)
    today = datetime.date.today().isoformat()
    desc = description or text.strip().replace("\n", " ")[:100]
    if mtype in ("user", "reference"):
        body = text.strip()
    else:
        body = f"{text.strip()}\n\n**Why:** {desc}\n**How to apply:** 记录于 {today}"
    write_entry(root, nm, mtype, desc, body, created=today, tags=tags)
    index_update(root, nm, desc.split("。")[0][:40], today)
    return {"name": nm, "path": str(frontmatter_path(root, nm)), "scope": scope}


def _history_append(name: str, old: dict) -> None:
    """Zep 式不丢旧事实：改版前把旧全文按时间头追加进 history/<name>.md（记忆根之外，不会被扫到）。"""
    HISTORY.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with (HISTORY / f"{name}.md").open("a", encoding="utf-8") as f:
        f.write(f"\n\n## {stamp}\n\n{old['body'].rstrip()}\n")


def history_versions(name: str, scope: str = "global", workspace_path: str | None = None) -> list[dict]:
    """memory_history 数据源：解析 history/<name>.md 的 ## 时间头，旧→新返回。"""
    root = resolve_root(scope, workspace_path)
    found = find_entry(root, name)
    if not found:
        return []
    hist = HISTORY / f"{found['name']}.md"
    if not hist.exists():
        return []
    parts = re.split(r"\n## (\d{4}-\d{2}-\d{2} \d{2}:\d{2})\n", hist.read_text(encoding="utf-8"))
    return [{"saved_at": parts[i], "body": parts[i + 1].strip()} for i in range(1, len(parts) - 1, 2)]


def edit(name: str, text: str | None = None, description: str | None = None,
         mtype: str | None = None, tags: list[str] | None = None, scope: str = "global",
         workspace_path: str | None = None) -> dict:
    """更新既有记忆：只改给定的字段，保留创建日期与 slug。"""
    root = resolve_root(scope, workspace_path)
    e = find_entry(root, name)
    if not e:
        raise ValueError(f"未找到: {name}")
    new_type = mtype if mtype in TYPES else (e["type"] or "project")
    new_desc = description if description is not None else e["description"]
    if text is not None:
        new_body = text.strip()
    else:
        # 去掉旧的 Why/How 尾巴，保持正文干净再按类型补
        base = re.sub(r"\n\n\*\*Why:\*\*[\s\S]*$", "", e["body"])
        new_body = base if new_type in ("user", "reference") else \
            f"{base}\n\n**Why:** {new_desc}\n**How to apply:** 记录于 {e.get('created') or datetime.date.today().isoformat()}"
    # tags 传入即替换（面板编辑把删掉的 tag 从列表里去掉，合并语义会让删除失效）
    new_tags = sorted(set(tags or [])) if tags is not None else e.get("tags")
    if text is not None or description is not None:
        _history_append(e["name"], e)   # 只在内容真变了才归档旧版，type/tags 微调不算
    write_entry(root, e["name"], new_type, new_desc, new_body, created=e.get("created"), tags=new_tags,
                hits=e.get("hits"), last_hit=e.get("last_hit"), related=e.get("related"),
                superseded_by=e.get("superseded_by") or None)
    index_update(root, e["name"], new_desc.split("。")[0][:40], e.get("created") or "")
    return {"name": e["name"], "scope": scope, "description": new_desc}


def _tokens(query: str) -> list[str]:
    """查询分词：ASCII 按词切；CJK 连续段切 bigram（单字保留）。
    中文整句子串匹配永远失败（issue：recall 中文零命中），这里做多 token OR 匹配。"""
    q = (query or "").lower()
    if not q.strip():
        return []
    out: list[str] = []
    for chunk in q.split():
        for m in re.finditer(r"[a-z0-9_]+|[一-鿿]+", chunk):
            t = m.group(0)
            if re.fullmatch(r"[一-鿿]+", t):
                if len(t) == 1:
                    out.append(t)
                else:
                    out.extend(t[i:i + 2] for i in range(len(t) - 1))   # CJK bigram
            else:
                out.append(t)
    return sorted(set(out))


def search(query: str, scope: str = "all", workspace_path: str | None = None, limit: int = 20,
           tag: str | None = None) -> list[dict]:
    roots = []
    if scope in ("all", "global"):
        roots.append(("global", resolve_root("global", None)))
    if scope in ("all", "project"):
        roots.append(("project", resolve_root("project", workspace_path)))
    toks = _tokens(query)
    tag_l = tag.lower() if tag else None
    hits = []
    for s, root in roots:
        for e in entries(root):
            if toks:
                hay = (e["name"] + " " + e["description"] + " " + e["body"]).lower()
                n = sum(1 for t in toks if t in hay)
                if n == 0:
                    continue
            else:
                n = 0
            if tag_l and tag_l not in [t.lower() for t in (e.get("tags") or [])]:
                continue
            hits.append(e | {"scope": s, "tok_hits": n})
    hits.sort(key=lambda x: x.get("tok_hits", 0), reverse=True)
    return hits[:limit]


def touch(root: Path, name: str) -> None:
    """命中计数：memory_get/memory_recall 每次命中 hits+1、last_hit 记当天，供加权召回。"""
    e = find_entry(root, name)
    if not e:
        return
    write_entry(root, e["name"], e["type"] or "project", e["description"], e["body"],
                created=e.get("created"), tags=e.get("tags"),
                hits=e.get("hits", 0) + 1, last_hit=datetime.date.today().isoformat(),
                related=e.get("related"), superseded_by=e.get("superseded_by") or None)


def recall(query: str, scope: str = "all", workspace_path: str | None = None, k: int = 5,
           tags: list[str] | None = None, topic: str | None = None) -> list[dict]:
    """加权召回：pin 置顶 ≫ 热度 ≫ 新近度 ≫ 标题命中；Agent 找上下文的入口。
    跨会话隔离②召回侧：tags 硬过滤（任一命中才保留）；topic 软降权——
    带其它 topic: 标签且与当前话题无关的条目 -50 分（不绝交，跨项目经验仍可捞回）。"""
    items = search(query, scope, workspace_path, limit=500)
    if tags:
        tl = [t.lower() for t in tags]
        items = [e for e in items if any(t in [x.lower() for x in (e.get("tags") or [])] for t in tl)]
    pins = load_pins()
    proj = str(resolve_root("project", workspace_path or os.environ.get("ZCODE_PROJECT_DIR")))
    glob = str(resolve_root("global", None))
    today = datetime.date.today()
    topic_l = ("topic:" + topic.lower()) if topic else None
    for e in items:
        age = 30
        if e.get("created"):
            try:
                age = (today - datetime.date.fromisoformat(e["created"][:10])).days
            except ValueError:
                pass
        pinned = bool(pins.get(proj if e["scope"] != "global" else glob, {}).get(e["name"]))
        if e.get("superseded_by"):   # Zep 式：被取代的事实沉底但不删（history 里仍是事实）
            e["score"] = -100 + e.get("hits", 0)
        else:
            e["score"] = ((1000 if pinned else 0) + e.get("hits", 0) * 5
                          + max(0, 30 - min(age, 30)) + (3 if query.lower() in e["name"].lower() else 0)
                          + e.get("tok_hits", 0) * 10)
        if topic_l:
            etags = [x.lower() for x in (e.get("tags") or [])]
            entry_topics = [x for x in etags if x.startswith("topic:")]
            if entry_topics and topic_l not in entry_topics:
                e["score"] -= 50      # 别的会话弧线 → 重罚但可翻盘
            elif topic_l in entry_topics:
                e["score"] += 20      # 本话题 → 置顶倾向
    items.sort(key=lambda x: x["score"], reverse=True)
    return items[:k]


def link(name: str, related: list[str], scope: str = "global",
         workspace_path: str | None = None) -> list[str]:
    """写 related frontmatter 字段：related 名按前缀归一到本根已存在的 slug。"""
    root = resolve_root(scope, workspace_path)
    e = find_entry(root, name)
    if not e:
        raise ValueError(f"未找到: {name}")
    names = {x["name"] for x in entries(root)}
    rel = sorted({n if n in names else next((m for m in names if m.startswith(n)), n) for n in related})
    write_entry(root, e["name"], e["type"] or "project", e["description"], e["body"],
                created=e.get("created"), tags=e.get("tags"), hits=e.get("hits"),
                last_hit=e.get("last_hit"), related=rel, superseded_by=e.get("superseded_by") or None)
    return rel


def find_entry(root: Path, name: str) -> dict | None:
    """名字解析：先精确同名，再前缀匹配（取最新）——避免 "a" 误配到 "a-b"。"""
    es = entries(root)
    return next((e for e in es if e["name"] == name), None) or \
           next((e for e in es if e["name"].startswith(name)), None)


def supersede(name: str, by: str, scope: str = "global",
              workspace_path: str | None = None) -> dict:
    """Zep 式矛盾处理：旧事实不删，打 superseded_by 标记并双向 related；召回时自动沉底。"""
    root = resolve_root(scope, workspace_path)
    e = find_entry(root, name)
    if not e:
        raise ValueError(f"未找到待取代的记忆: {name}")
    names = {x["name"] for x in entries(root)}
    tgt = next((m for m in names if m == by), None) or \
          next((m for m in names if m.startswith(by)), None)
    if not tgt:
        raise ValueError(f"未找到取代者: {by}")
    if tgt == e["name"]:
        raise ValueError("不能取代自己")
    write_entry(root, e["name"], e["type"] or "project", e["description"], e["body"],
                created=e.get("created"), tags=e.get("tags"), hits=e.get("hits"),
                last_hit=e.get("last_hit"),
                related=sorted({*e.get("related", []), tgt}), superseded_by=tgt)
    ne = next((x for x in entries(root) if x["name"] == tgt), None)   # 反向：新事实 related 到旧事实，演进链可见
    if ne:
        write_entry(root, ne["name"], ne["type"] or "project", ne["description"], ne["body"],
                    created=ne.get("created"), tags=ne.get("tags"), hits=ne.get("hits"),
                    last_hit=ne.get("last_hit"),
                    related=sorted({*ne.get("related", []), e["name"]}),
                    superseded_by=ne.get("superseded_by") or None)
    index_update(root, e["name"], "[已取代→" + tgt + "] " + e["description"].split("。")[0][:36], "superseded")
    return {"name": e["name"], "superseded_by": tgt}


def unsupersede(name: str, scope: str = "global", workspace_path: str | None = None) -> str:
    """撤取代标记（错标，或事实重新成立）。"""
    root = resolve_root(scope, workspace_path)
    e = find_entry(root, name)
    if not e:
        raise ValueError(f"未找到: {name}")
    write_entry(root, e["name"], e["type"] or "project", e["description"], e["body"],
                created=e.get("created"), tags=e.get("tags"), hits=e.get("hits"),
                last_hit=e.get("last_hit"), related=e.get("related"))
    index_update(root, e["name"], e["description"].split("。")[0][:40], e.get("created") or "")
    return e["name"]


def _evolve_dir() -> Path:
    d = resolve_root("global", None) / "evolve"
    d.mkdir(parents=True, exist_ok=True)
    return d


def capture(session_id: str, note: str = "") -> dict:
    """进化闭环①捕获：会话结束时 Stop hook 写一条摘要进 inbox。
    只记坐标（时间/会话/工作区），实质内容由 crystallize 从 model-io 日志提取——
    hook 里做重解析会拖慢每轮收尾。"""
    rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "session": session_id, "note": note[:300]}
    inbox = _evolve_dir() / "inbox.jsonl"
    with inbox.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def crystallize(max_candidates: int = 5) -> dict:
    """进化闭环②结晶：从 model-io 日志提取候选记忆（带 candidate 标记，不进 pin 注入）。
    提取启发式只收三类硬证据：新出现且存在的绝对路径、可执行命令行、doctor/verify 的
    显式结论行。软知识（判断/教训）不自动入库——无门控抽取产垃圾（psnb L13）。"""
    inbox = _evolve_dir() / "inbox.jsonl"
    if not inbox.exists():
        return {"candidates": 0, "skipped": 0}
    seen_sessions = set()
    for l in inbox.read_text(encoding="utf-8").splitlines():
        try:
            seen_sessions.add(json.loads(l)["session"])
        except Exception:
            pass
    processed = _evolve_dir() / "processed.jsonl"
    done = set()
    if processed.exists():
        for l in processed.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(l)["session"])
            except Exception:
                pass
    todo = seen_sessions - done
    root = resolve_root("global", None)
    made = 0
    for sid in sorted(todo):
        # 从当日 rollout 找该会话日志
        roll = Path(os.environ.get("ZCODE_HOME", str(Path.home() / ".zcode"))) / "cli" / "rollout"
        if not roll.exists():
            continue
        cands = []
        # 日志可达 GB 级：只读尾部 8MB（近期证据在尾部，zcode-purge verify 同款纪律）
        TAIL = 8 * 1024 * 1024
        # 正则 chr 构造：heredoc 会剥反斜杠，这里不写字面转义
        B = chr(92)
        PAT = re.compile("[EDe]:[/" + B + B + "][^" + chr(34) + chr(39) + B + "s,)]{4,90}")
        for f in sorted(roll.glob("*" + sid[:8] + "*")):
            with f.open("rb") as fh:
                fh.seek(max(0, f.stat().st_size - TAIL))
                text = fh.read().decode("utf-8", errors="replace")
            for m in PAT.finditer(text):
                v = m.group(0)
                if any(k in v.lower() for k in ("appdata", "temp", "node_modules", ".git")):
                    continue
                if Path(v.replace("/", os.sep)).exists() and v not in [c[1] for c in cands]:
                    cands.append(("path", v))
            if len(cands) >= max_candidates:
                break
        for kind, val in cands[:max_candidates]:
            name = slugify(kind + " " + val)[:48]
            if find_entry(root, name):
                continue
            write_entry(root, name, "reference", "候选：" + val[:60],
                        f"crystallize 自 {sid[:12]} 提取的证据：{val}" + chr(10) + chr(10) + "> " + chr(0x23F3) + " candidate：未晋升。晋升用 memory_promote，淘汰直接 forget。")
            # candidate 标记写进正文尾（frontmatter metadata 由 write_entry 固定，不动核心结构）
            fp = frontmatter_path(root, name)
            t = fp.read_text(encoding="utf-8")
            pass   # 标记已随 body 写入（见下方 write_entry 调用）
            made += 1
    with processed.open("a", encoding="utf-8") as f:
        for sid in todo:
            f.write(json.dumps({"session": sid}) + "\n")
    return {"candidates": made, "skipped": len(todo) - made}


def promote(name: str) -> dict:
    """进化闭环③晋升：candidate → 正式记忆（去掉候选标记）。"""
    root = resolve_root("global", None)
    fp = frontmatter_path(root, name)
    if not fp.exists():
        raise ValueError("未找到: " + name)
    mark = "> " + chr(0x23F3) + " candidate：未晋升。晋升用 memory_promote，淘汰直接 forget。"
    t = fp.read_text(encoding="utf-8").replace(mark, "").rstrip() + chr(10)
    fp.write_text(t, encoding="utf-8")
    touch(root, name)
    return {"name": name, "promoted": True}

def dream(dry: bool = False, decay_days: int = None) -> dict:
    """自进化巩固（离线跑，Cron 夜间）：
      ① 去重：同 scope 内正文前缀相同的条目，旧的被新的取代（pin 豁免）
      ② 衰退：非 pin、从未命中且创建超过 decay_days → forget 入回收站（可 restore）
      ③ 建链：同 scope 内描述 token 重叠 ≥2 的条目自动互相关联（每条 related 上限 5）
      ④ 重建 OVERVIEW。返回动作清单，全部动作有账可查（supersede/forget 均可逆）。"""
    today = datetime.date.today()
    cfg = _evolve_dir() / "config.json"
    if decay_days is None:
        try:
            decay_days = int(json.loads(cfg.read_text(encoding="utf-8")).get("decay_days", 60))
        except (OSError, ValueError):
            decay_days = 60
    actions = {"dedup": [], "decay": [], "link": [], "dry": dry}
    for scope in ("global", "project"):
        root = resolve_root(scope, None if scope == "global" else os.environ.get("ZCODE_PROJECT_DIR"))
        items = [e for e in entries(root)]
        # ① 去重（旧 → 新取代）
        seen: dict[str, dict] = {}
        for e in sorted(items, key=lambda x: x.get("created") or ""):
            if e.get("superseded_by") or (e.get("pinned")):
                continue
            key = re.sub(r"\s+", " ", e["body"])[:100]
            if key in seen:
                if not dry:
                    supersede(e["name"], seen[key]["name"], scope=scope)
                actions["dedup"].append((e["name"], seen[key]["name"]))
            else:
                seen[key] = e
        # ② 衰退
        for e in items:
            if e.get("pinned") or e.get("superseded_by") or e.get("hits", 0) > 0:
                continue
            age = 0
            if e.get("created"):
                try:
                    age = (today - datetime.date.fromisoformat(e["created"][:10])).days
                except ValueError:
                    pass
            if age >= decay_days:
                if not dry:
                    forget(e["name"], scope=scope)
                actions["decay"].append(e["name"])
        # ③ 建链（描述 token 重叠）
        live = [e for e in entries(root) if not e.get("superseded_by")]
        for i, a in enumerate(live):
            ta = set(_tokens(a["description"]))
            for b in live[i + 1:]:
                tb = set(_tokens(b["description"]))
                if len(ta & tb) >= 2:
                    ra = set(a.get("related") or []); rb = set(b.get("related") or [])
                    if b["name"] not in ra and a["name"] not in rb and len(ra) < 5 and len(rb) < 5:
                        if not dry:
                            link(a["name"], [b["name"]], scope=scope)
                        actions["link"].append((a["name"], b["name"]))
    if not dry:
        overview()
        # 自调参：近 30 天恢复率 > 20% 说明衰退过狠 → decay_days +15（上限 120），写回 config
        try:
            rlog = _evolve_dir() / "restores.jsonl"
            dlog = _evolve_dir() / "dream.jsonl"
            cut = (datetime.datetime.now() - datetime.timedelta(days=30)).isoformat(timespec="seconds")
            restores = sum(len(json.loads(l).get("names", [])) for l in rlog.read_text(encoding="utf-8").splitlines()
                           if l.strip() and json.loads(l).get("ts", "") >= cut) if rlog.exists() else 0
            decayed = sum(len(json.loads(l).get("decayed", [])) for l in dlog.read_text(encoding="utf-8").splitlines()
                          if l.strip() and json.loads(l).get("ts", "") >= cut) if dlog.exists() else 0
            if decayed and restores / decayed > 0.2:
                decay_days = min(120, decay_days + 15)
            cfg.write_text(json.dumps({"decay_days": decay_days}, ensure_ascii=False) + "\n", encoding="utf-8")
            with dlog.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                                    "decayed": actions["decay"], "decay_days": decay_days}) + "\n")
        except (OSError, ValueError):
            pass
    return actions


def overview() -> str:
    """活文档：把全部有效记忆编成可读总览（pin/热度优先，跳已取代），写 REG_DIR/OVERVIEW.md。"""
    roots = {"global": resolve_root("global", None),
             "project": resolve_root("project", os.environ.get("ZCODE_PROJECT_DIR") or os.getcwd())}
    pins = load_pins()
    items = []
    for scope, root in roots.items():
        pk = str(root)
        for e in entries(root):
            if e.get("superseded_by"):
                continue
            e["scope"] = scope
            e["pinned"] = bool(pins.get(pk, {}).get(e["name"]))
            items.append(e)
    today = datetime.date.today().isoformat()
    lines = ["# Memory Overview", "",
             f"> 由 Memory Bank 生成于 {today}，共 {len(items)} 条有效记忆。", ""]
    for t in list(TYPES) + ["?"]:
        sel = [e for e in items if (e["type"] or "?") == t]
        if not sel:
            continue
        sel.sort(key=lambda e: (not e["pinned"], -e.get("hits", 0)))
        lines.append(f"## {t}（{len(sel)}）")
        lines.append("")
        for e in sel:
            rel = ""
            if e.get("related") or e.get("links"):
                rel = " → " + ", ".join(sorted({*e.get("related", []), *e.get("links", [])})[:4])
            lines.append(f"- {'📌 ' if e['pinned'] else ''}[[{e['name']}]]（{e['scope']}）"
                         f"— {e['description']}" + (f" 🔥{e['hits']}" if e.get("hits") else "") + rel)
        lines.append("")
    text = "\n".join(lines)
    REG_DIR.mkdir(parents=True, exist_ok=True)
    (REG_DIR / "OVERVIEW.md").write_text(text, encoding="utf-8")
    return text


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
                trash_name = f"{stamp}-{e['file'].name}"
                shutil.move(str(e["file"]), str(TRASH / trash_name))
                index_update(root, e["name"], "", "", remove=True)
                # 副账本：记录原始归属，restore 时按图索骥
                trash_log(TRASH, {"id": f"{stamp}-{s}", "name": e["name"], "scope": s,
                                  "root": str(root), "trash_file": trash_name,
                                  "deleted_at": stamp})
                removed.append(f"{s}:{e['name']}")
    return removed


def trash_log(trash_dir: Path, entry: dict | None = None) -> list[dict]:
    """trash 副账本（JSONL 追加）：记录每条删除的原始位置，支撑面板内恢复。"""
    log = trash_dir / "index.jsonl"
    if entry is not None:
        trash_dir.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=True) + "\n")
    out = []
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def restore(name: str) -> list[str]:
    """从 trash 恢复最近删除的匹配条目：文件移回原记忆根并补回索引行。"""
    done = []
    for entry in reversed(trash_log(TRASH)):   # 最新优先
        if entry.get("name", "").startswith(name) or entry.get("id", "").startswith(name):
            src = TRASH / entry["trash_file"]
            if not src.exists():
                continue
            root = Path(entry["root"])
            root.mkdir(parents=True, exist_ok=True)
            dest = frontmatter_path(root, entry["name"])
            if dest.exists():
                continue   # 同名已存在（可能已重写），不覆盖
            shutil.move(str(src), str(dest))
            index_update(root, entry["name"], (parse_entry(dest) or {}).get("description", ""),
                         entry.get("deleted_at", "")[:4] + "")
            done.append(f"{entry['scope']}:{entry['name']}")
    return done
    # 恢复记账：dream 自调参的反馈信号（衰退太狠会在这里体现）
    try:
        with (_evolve_dir() / "restores.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.datetime.now().isoformat(timespec="seconds"), "names": done}) + "\n")
    except OSError:
        pass
def trash_items() -> list[dict]:
    """列出回收站现存条目（文件还在的才有意义）。"""
    out = []
    for entry in reversed(trash_log(TRASH)):
        src = TRASH / entry.get("trash_file", "")
        if src.exists():
            out.append({"id": entry["id"], "name": entry["name"], "scope": entry["scope"],
                        "deleted_at": entry.get("deleted_at", ""),
                        "description": (parse_entry(src) or {}).get("description", "")})
    return out


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
    """全景统计：类型分布 / 近30天逐日写入 / top hits / 90天零命中候选（面板洞察视图数据源）。"""
    roots = {"global": resolve_root("global", None),
             "project": resolve_root("project", os.environ.get("ZCODE_PROJECT_DIR") or os.getcwd())}
    today = datetime.date.today()
    by_type, per_day, top_hits, stale = {}, {}, [], []
    superseded = 0
    for scope, root in roots.items():
        for e in entries(root):
            if e.get("superseded_by"):
                superseded += 1
                continue
            t = e["type"] or "?"
            by_type[t] = by_type.get(t, 0) + 1
            day = (e.get("created") or "")[:10]
            if day:
                per_day[day] = per_day.get(day, 0) + 1
            if e.get("hits"):
                top_hits.append({"name": e["name"], "hits": e["hits"], "scope": scope,
                                 "last_hit": e.get("last_hit", "")})
            elif day and (today - datetime.date.fromisoformat(day)).days > 90:
                stale.append({"name": e["name"], "scope": scope, "created": day,
                              "description": e["description"][:60]})
    return {"total": sum(by_type.values()), "by_type": by_type, "superseded": superseded,
            "per_day": {d: per_day[d] for d in sorted(per_day)[-30:]},
            "top_hits": sorted(top_hits, key=lambda x: x["hits"], reverse=True)[:8],
            "stale": stale[:10], "pinned": sum(len(v) for v in load_pins().values()),
            "global": len(entries(roots["global"])), "project": len(entries(roots["project"]))}


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
    {"name": "memory_save", "description": "保存一条事实到 ZCode 内置记忆（一事实一文件+索引行）；type: user/feedback/project/reference；global 条目自动带 topic:<来源目录名> 标签防跨会话串味，可显式 tags 覆盖",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string"}, "type": {"type": "string", "enum": list(TYPES), "default": "project"},
         "scope": {"type": "string", "enum": ["global", "project"], "default": "global"},
         "tags": {"type": "array", "items": {"type": "string"}}},
         "required": ["text"]}},
    {"name": "memory_search", "description": "按关键词检索内置记忆（空关键词=列出全部；支持 tag 过滤）",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "tag": {"type": "string"},
         "scope": {"type": "string", "enum": ["all", "global", "project"], "default": "all"}},
         "required": []}},
    {"name": "memory_list", "description": "列出记忆（结构化 JSON，含正文与 pin 状态；Memory Bank 面板数据源）",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "default": ""}, "tag": {"type": "string"},
         "scope": {"type": "string", "enum": ["all", "global", "project"], "default": "all"}},
         "required": []}},
    {"name": "memory_get", "description": "按 name 读取单条记忆全文（计入 hits 热度）",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "memory_edit", "description": "更新既有记忆的正文/描述/类型/标签（保留 slug 与创建日期；tags 传入即整体替换）",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "text": {"type": "string"},
         "description": {"type": "string"}, "type": {"type": "string", "enum": list(TYPES)},
         "tags": {"type": "array", "items": {"type": "string"}},
         "scope": {"type": "string", "enum": ["global", "project"], "default": "global"}},
         "required": ["name"]}},
    {"name": "memory_trash", "description": "列出回收站现存条目（面板回收站视图数据源）",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "memory_restore", "description": "从回收站恢复误删记忆（按 name 或 id 前缀）",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "memory_pin", "description": "置顶/取消置顶记忆（pin 过的每会话确定性注入）",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "on": {"type": "boolean", "default": True},
         "scope": {"type": "string", "enum": ["global", "project"], "default": "global"}},
         "required": ["name"]}},
    {"name": "memory_forget", "description": "按 name 前缀删除记忆文件（移入 trash，可回溯）",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "memory_recall", "description": "加权召回：pin ≫ 热度 ≫ 新近度；返回 top-k（含命中计数），Agent 找上下文用这个而不是 memory_search。跨会话隔离：tags 硬过滤；topic 传当前工程目录名软降权他弧线条目",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "k": {"type": "integer", "default": 5},
         "scope": {"type": "string", "enum": ["all", "global", "project"], "default": "all"},
         "tags": {"type": "array", "items": {"type": "string"}},
         "topic": {"type": "string"}},
         "required": ["query"]}},
    {"name": "memory_history", "description": "查看记忆的编辑版本史（每次改版前旧全文自动归档）",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "memory_link", "description": "维护记忆间关联（related frontmatter 字段；正文 [[slug]] 亦会被解析为 links）",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "related": {"type": "array", "items": {"type": "string"}},
         "scope": {"type": "string", "enum": ["global", "project"], "default": "global"}},
         "required": ["name", "related"]}},
    {"name": "memory_stats", "description": "全景统计：类型分布/近30天写入/top hits/90天零命中候选",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "memory_promote", "description": "晋升 candidate 记忆为正式记忆（去掉未晋升标记）。crystallize 产出的候选经确认后用这个转正",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "memory_dream", "description": "自进化巩固：去重（旧被新取代）+ 衰退（长期零命中入回收站，可 restore）+ 同域自动建链 + 重建 OVERVIEW。夜间 Cron 跑，也可手动。dry=true 先看会动什么",
     "inputSchema": {"type": "object", "properties": {
         "dry": {"type": "boolean", "default": False},
         "decay_days": {"type": "integer", "default": 60}}, "required": []}},
    {"name": "memory_supersede", "description": "标记旧事实被新事实取代（Zep 式：不删除，召回自动沉底，两边双向 related）",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "by": {"type": "string"},
         "scope": {"type": "string", "enum": ["global", "project"], "default": "global"}},
         "required": ["name", "by"]}},
    {"name": "memory_unsupersede", "description": "撤销取代标记（错标，或旧事实重新成立）",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"},
         "scope": {"type": "string", "enum": ["global", "project"], "default": "global"}},
         "required": ["name"]}},
    {"name": "memory_overview", "description": "生成活文档总览（按类型汇编有效记忆，跳已取代；写 OVERVIEW.md）",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
]

# ui.surfaces 面板资源：宿主经 resources/read 取 ui://memory-bank/panel.html 渲染进沙箱 webview。
# 源码拆成 panel.html/css/js 三件，读出时按 /*INLINE:<name>*/ 标记内联回单 HTML（宿主只认单文件资源）。
PANEL_URI = "ui://memory-bank/panel.html"
PANEL_DIR = Path(__file__).resolve().parent.parent / "ui"


def render_panel() -> str:
    html = (PANEL_DIR / "panel.html").read_text(encoding="utf-8")
    for tag in ("panel.css", "panel.js"):
        body = (PANEL_DIR / tag).read_text(encoding="utf-8")
        html = html.replace(f"/*INLINE:{tag}*/", body)
    if "/*INLINE:" in html:
        raise ValueError("panel 内联标记未全部替换，检查 ui/ 下文件")
    return html


def _items_json(items: list[dict]) -> str:
    pins = load_pins()   # 一次读入，别每条记忆都碰一次磁盘
    proj_root = str(resolve_root("project", os.environ.get("ZCODE_PROJECT_DIR")))
    glob_root = str(resolve_root("global", None))

    def pinned(name: str, scope: str) -> bool:
        return bool(pins.get(proj_root if scope != "global" else glob_root, {}).get(name))

    return json.dumps([{"name": e["name"], "description": e["description"], "type": e["type"],
                        "scope": e["scope"], "body": e["body"], "created": e.get("created", ""),
                        "tags": e.get("tags", []), "hits": e.get("hits", 0),
                        "last_hit": e.get("last_hit", ""), "related": e.get("related", []),
                        "superseded_by": e.get("superseded_by", ""),
                        "links": e.get("links", []),
                        "pinned": pinned(e["name"], e["scope"])} for e in items],
                       ensure_ascii=True)


def handle_mcp() -> None:
    # stdio 常开：必须逐行即时处理，不能 read() 到 EOF（宿主不关管道，读到 EOF=30s 超时假死）
    for raw in sys.stdin.buffer:
        raw = raw.decode("utf-8", "replace").strip()
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
                "serverInfo": {"name": "memory-bank", "version": "0.9.1"}}
    if method == "resources/list":
        return {"resources": [{"uri": PANEL_URI, "name": "Memory Bank Panel",
                               "mimeType": "text/html"}]}
    if method == "resources/read":
        if params.get("uri") != PANEL_URI:
            raise ValueError(f"unknown resource: {params.get('uri')}")
        return {"contents": [{"uri": PANEL_URI, "mimeType": "text/html",
                              "text": render_panel()}]}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name, args = params.get("name", ""), params.get("arguments") or {}
        if name == "memory_save":
            r = save(args["text"], args.get("type", "project"), args.get("scope", "global"),
                     tags=args.get("tags"))
            return {"content": [{"type": "text", "text": f"已保存 {r['name']} ({r['scope']}) → {r['path']}"}]}
        if name == "memory_search":
            items = search(args.get("query", ""), args.get("scope", "all"), tag=args.get("tag"))
            return {"content": [{"type": "text", "text": render(items)}]}
        if name == "memory_list":
            items = search(args.get("query", ""), args.get("scope", "all"), limit=100, tag=args.get("tag"))
            return {"content": [{"type": "text", "text": _items_json(items)}]}
        if name == "memory_get":
            found = [e for e in search("", "all", limit=500) if e["name"].startswith(args["name"])]
            found.sort(key=lambda e: e["name"] != args["name"])   # 精确同名优先于前缀误配
            if not found:
                raise ValueError(f"未找到: {args['name']}")
            hit = found[0]
            touch(resolve_root("project" if hit["scope"] != "global" else "global", None), hit["name"])
            return {"content": [{"type": "text", "text": hit["body"]}]}
        if name == "memory_edit":
            r = edit(args["name"], args.get("text"), args.get("description"),
                     args.get("type"), args.get("tags"), args.get("scope", "global"))
            return {"content": [{"type": "text", "text": f"已更新 {r['name']} ({r['scope']})"}]}
        if name == "memory_pin":
            set_pin(args["name"], args.get("on", True), args.get("scope", "global"))
            return {"content": [{"type": "text",
                                 "text": f"{'pinned' if args.get('on', True) else 'unpinned'} {args['name']}"}]}
        if name == "memory_trash":
            return {"content": [{"type": "text", "text": json.dumps(trash_items(), ensure_ascii=True)}]}
        if name == "memory_restore":
            done = restore(args["name"])
            return {"content": [{"type": "text", "text": f"已恢复 {len(done)}: {', '.join(done)}" if done else "未找到可恢复条目"}]}
        if name == "memory_forget":
            gone = forget(args["name"], args.get("scope", "all"))
            return {"content": [{"type": "text", "text": f"已删除 {len(gone)}: {', '.join(gone)}" if gone else "未找到"}]}
        if name == "memory_recall":
            items = recall(args.get("query", ""), args.get("scope", "all"), k=args.get("k", 5),
                           tags=args.get("tags"), topic=args.get("topic"))
            if args.get("query", "").strip():   # 命中计数：recall 也是一次命中（原实现只在 memory_get 计数）
                for e in items[:3]:
                    touch(resolve_root("project" if e["scope"] != "global" else "global", None), e["name"])
            out = "\n".join(f"{e['name']}  [{e['type'] or '?'}] ({e['scope']}) score={e['score']} "
                            f"hits={e.get('hits', 0)}  {e['description']}" for e in items)
            return {"content": [{"type": "text", "text": out or "（无命中）"}]}
        if name == "memory_history":
            vers = history_versions(args["name"], args.get("scope", "global"))
            return {"content": [{"type": "text", "text": json.dumps(vers, ensure_ascii=True)}]}
        if name == "memory_link":
            rel = link(args["name"], args.get("related", []), args.get("scope", "global"))
            return {"content": [{"type": "text", "text": f"已关联 {args['name']} → {', '.join(rel)}"}]}
        if name == "memory_stats":
            return {"content": [{"type": "text", "text": json.dumps(stats(), ensure_ascii=True)}]}
        if name == "memory_promote":
            r = promote(args["name"])
            return {"content": [{"type": "text", "text": "已晋升 " + r["name"]}]}
        if name == "memory_dream":
            r = dream(bool(args.get("dry")), int(args.get("decay_days", 60)))
            lines = ["dry=" + str(r["dry"]),
                     "去重: " + str(len(r["dedup"])) + " " + "; ".join(a + " -> " + b for a, b in r["dedup"][:5]),
                     "衰退: " + str(len(r["decay"])) + " " + ", ".join(r["decay"][:8]),
                     "建链: " + str(len(r["link"])) + " " + "; ".join(a + " <-> " + b for a, b in r["link"][:5])]
            return {"content": [{"type": "text", "text": chr(10).join(lines)}]}
        if name == "memory_supersede":
            r = supersede(args["name"], args["by"], args.get("scope", "global"))
            return {"content": [{"type": "text", "text": f"{r['name']} 已标记为被 {r['superseded_by']} 取代（旧事实保留，召回沉底）"}]}
        if name == "memory_unsupersede":
            return {"content": [{"type": "text", "text": f"已撤销取代标记: {unsupersede(args['name'], args.get('scope', 'global'))}"}]}
        if name == "memory_overview":
            return {"content": [{"type": "text", "text": overview()}]}
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
    sp = sub.add_parser("supersede"); sp.add_argument("name"); sp.add_argument("by"); sp.add_argument("--scope", default="global", choices=["global", "project"])
    sp = sub.add_parser("unsupersede"); sp.add_argument("name"); sp.add_argument("--scope", default="global", choices=["global", "project"])
    sp = sub.add_parser("capture"); sp.add_argument("--session", required=True); sp.add_argument("--note", default="")
    sp = sub.add_parser("crystallize"); sp.add_argument("--max", type=int, default=5)
    sp = sub.add_parser("promote"); sp.add_argument("name")
    sp = sub.add_parser("dream"); sp.add_argument("--dry", action="store_true"); sp.add_argument("--decay-days", type=int, default=60)
    sp = sub.add_parser("overview")
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
        elif a.cmd == "supersede":
            r = supersede(a.name, a.by, a.scope)
            print(f"{r['name']} -> superseded_by {r['superseded_by']}")
        elif a.cmd == "unsupersede":
            print(f"cleared: {unsupersede(a.name, a.scope)}")
        if a.cmd == "capture":
            print(json.dumps(capture(a.session, a.note), ensure_ascii=False)); return 0
        elif a.cmd == "crystallize":
            print(json.dumps(crystallize(a.max), ensure_ascii=False)); return 0
        elif a.cmd == "promote":
            print(json.dumps(promote(a.name), ensure_ascii=False)); return 0
        elif a.cmd == "dream":
            r = dream(a.dry, a.decay_days)
            print(json.dumps({"dry": r["dry"], "dedup": len(r["dedup"]), "decay": len(r["decay"]), "link": len(r["link"])}, ensure_ascii=False))
            return 0
        elif a.cmd == "overview":
            print(overview())
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
