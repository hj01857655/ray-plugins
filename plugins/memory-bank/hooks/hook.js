#!/usr/bin/env node
'use strict';
// Memory Bank SessionStart hook v3：确定性注入内置记忆里被 pin 的记忆文件 + MEMORY.md 索引。
// 内置系统（语义召回）可能一条都不选；本 hook 保证 pin 过的关键事实每会话必在场。
// 注入格式与引擎 memory section 同源（一事实一文件 frontmatter，见 context/sections/memory.ts）。
// 任何失败都静默降级为空注入，绝不阻塞会话启动。
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');

const MEM_ROOT = path.join(os.homedir(), '.zcode', 'cli', 'memories');
const REG_DIR = path.join(os.homedir(), '.zcode', 'memory-bank');
const PINS = path.join(REG_DIR, 'pins.json');
const MAX_CHARS = 4000;

function projectRoot(cwd) {
  const normalized = path.resolve(cwd);
  const key = process.platform === 'win32' ? normalized.toLowerCase() : normalized;
  const h = crypto.createHash('sha256').update(key, 'utf8').digest('hex').slice(0, 16);
  const slug = (path.basename(normalized).toLowerCase().replace(/[^a-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '') || 'project').slice(0, 48);
  return path.join(MEM_ROOT, 'projects', `${slug}-${h}`, 'memory');
}

function readPins() {
  try { return JSON.parse(fs.readFileSync(PINS, 'utf8')); } catch { return {}; }
}

function parseEntry(p) {
  try {
    const text = fs.readFileSync(p, 'utf8');
    const m = text.match(/^---\s*\n([\s\S]*?)\n---\s*\n?([\s\S]*)$/);
    if (!m) return null;
    const name = (m[1].match(/^name:\s*(.+)$/m) || [])[1];
    return { name: (name || path.basename(p, '.md')).trim(), body: m[2].trim() };
  } catch { return null; }
}

function entryPath(root, name) {
  // slug 由 save() 生成，限定字符集，防路径逃逸
  if (!/^[a-z0-9][a-z0-9.-]{0,47}$/.test(name)) return null;
  const p = path.join(root, `${name}.md`);
  return p.startsWith(root) ? p : null;
}

let payload = {};
try { payload = JSON.parse(fs.readFileSync(0, 'utf8') || '{}'); } catch { /* stdin 可能为空 */ }
const cwd = process.env.ZCODE_PROJECT_DIR || payload.cwd || process.cwd();
const pins = readPins();
const pinnedNames = new Set(Object.values(pins).flatMap((s) => Object.keys(s)));

const lines = ['[Memory Bank] 确定性记忆（pin 全文 + 记忆索引，等效引擎 default-index 档）：', ''];
let used = lines.join('\n').length, shown = 0;
for (const [rootKey, names] of Object.entries(pins)) {
  const label = rootKey.includes('projects') ? `项目 ${path.basename(cwd)}` : 'global';
  for (const name of Object.keys(names)) {
    const p = entryPath(rootKey, name);
    if (!p) continue;
    const e = parseEntry(p);
    if (!e) continue;
    const chunk = `- (${label}) ${e.name}: ${e.body}`;
    if (used + chunk.length > MAX_CHARS) { lines.push('- …（超出注入预算，用 /recall 查询）'); shown++; break; }
    lines.push(chunk); used += chunk.length + 1; shown++;
  }
}
// MEMORY.md 索引行注入（global + 当前项目）：引擎开关关闭时由本 hook 代行 default-index 职责
for (const [label, root] of [['global', path.join(MEM_ROOT, 'global', 'memory')], [`项目 ${path.basename(cwd)}`, projectRoot(cwd)]]) {
  let idx = '';
  try { idx = fs.readFileSync(path.join(root, 'MEMORY.md'), 'utf8'); } catch { continue; }
  const items = idx.split(/\r?\n/).filter((l) => l.startsWith('- ['))
    // pin 过的记忆已有全文注入，索引行去重
    .filter((l) => !Object.values(Object.values(pins).find((s) => s) || {}).length || !pinnedNames.has(l.match(/\]\(([^)]+)\)/)?.[1]?.replace(/\.md$/, '')));
  if (!items.length) continue;
  const chunk = `记忆索引（${label}）：\n` + items.join('\n');
  if (used + chunk.length > MAX_CHARS) { lines.push(`- …（${label} 索引超预算，用 /recall 查询）`); break; }
  lines.push(chunk); used += chunk.length + 1; shown++;
}
if (shown) {
  lines.push('', '以上为确定性注入的持久记忆；与用户最新指令冲突时以最新指令为准。', '内置记忆目录也可直接读写（一事实一文件 frontmatter 格式，写后需在 MEMORY.md 加索引行）。');
}

// 非 ASCII 全部转义为 \uXXXX：输出纯 ASCII，消费端无论按什么编码读 stdout 都不会乱码
process.stdout.write(JSON.stringify({ additionalContext: shown ? lines.join('\n') : '' })
  .replace(/[^\x00-\x7F]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`));
