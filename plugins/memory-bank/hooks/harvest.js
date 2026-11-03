#!/usr/bin/env node
'use strict';
// Memory Bank SessionEnd hook：会话末自动收割学习记录（doobidoo memory_harvest 模式）。
// 读 stdin JSON（payload 可含 transcript_path/cwd），从 transcript 尾部提取对话要点，
// 直写 frontmatter 记忆文件 + MEMORY.md 索引行（与引擎 save() 同格式，type=project, tags=[session,auto]）。
// 没抓到内容 / 任何失败一律静默 exit 0，绝不阻塞会话结束。MEMBANK_HOME 可重定向 ~/.zcode 便于测试。
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');

const HOME = process.env.MEMBANK_HOME || os.homedir();
const MEM_ROOT = path.join(HOME, '.zcode', 'cli', 'memories');
const MAX_BODY = 4000;

function projectRoot(cwd) {
  const normalized = path.resolve(cwd);
  const key = process.platform === 'win32' ? normalized.toLowerCase() : normalized;
  const h = crypto.createHash('sha256').update(key, 'utf8').digest('hex').slice(0, 16);
  const slug = (path.basename(normalized).toLowerCase().replace(/[^a-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '') || 'project').slice(0, 48);
  return path.join(MEM_ROOT, 'projects', `${slug}-${h}`, 'memory');
}

let payload = {};
try { payload = JSON.parse(fs.readFileSync(0, 'utf8') || '{}'); } catch { payload = {}; }

function transcriptLines() {
  const tp = payload.transcript_path;
  if (!tp) return [];
  try {
    const raw = fs.readFileSync(tp, 'utf8');
    return raw.trim().split(/\r?\n/).filter(Boolean).slice(-120);   // 只看会话尾部
  } catch { return []; }
}

// transcript JSONL 每行一个事件：{type:'user'|'assistant', message:{content:[{type:'text',text}]}}
function extractPoints(lines) {
  const pts = [];
  for (const line of lines) {
    let ev;
    try { ev = JSON.parse(line); } catch { continue; }
    const who = ev.type === 'assistant' ? 'assistant' : ev.type === 'user' ? 'user' : null;
    if (!who) continue;
    const c = ev.message && ev.message.content;
    let text = '';
    if (typeof c === 'string') text = c;
    else if (Array.isArray(c)) text = c.filter((b) => b && b.type === 'text').map((b) => b.text).join(' ');
    text = (text || '').replace(/\s+/g, ' ').trim();
    if (text.length < 12) continue;                                    // 太短没营养
    if (/^<(system-reminder|local-command|command)/i.test(text)) continue;
    pts.push(who + ': ' + text.slice(0, 200));
    if (pts.length >= 12) break;
  }
  return pts;
}

const pts = extractPoints(transcriptLines());
if (pts.length) {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  const today = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  const name = `session-${today.replace(/-/g, '')}-${pad(now.getHours())}${pad(now.getMinutes())}`;
  const cwd = process.env.ZCODE_PROJECT_DIR || payload.cwd || process.cwd();
  const root = projectRoot(cwd);
  const file = path.join(root, `${name}.md`);
  try {
    fs.mkdirSync(root, { recursive: true });
    if (!fs.existsSync(file)) {   // 同分钟重复触发只写一份
      const hm = `${pad(now.getHours())}:${pad(now.getMinutes())}`;
      const desc = `会话记录 ${today} ${hm}（SessionEnd 自动收割）`;
      const text = `---\nname: ${name}\ndescription: ${desc}\ntags: [session, auto]\nmetadata:\n  type: project\ncreated: ${today}\n---\n\n${pts.join('\n').slice(0, MAX_BODY)}\n`;
      fs.writeFileSync(file, text, 'utf8');
      // 索引行（与引擎 index_update 同格式：按文件名去重后追加）
      const idx = path.join(root, 'MEMORY.md');
      let lines = [];
      try { lines = fs.readFileSync(idx, 'utf8').split(/\r?\n/).filter(Boolean); } catch { lines = []; }
      lines = lines.filter((l) => !l.includes(`](${name}.md)`));
      lines.push(`- [${desc.split('（')[0]}](${name}.md) — ${today}`);
      fs.writeFileSync(idx, lines.join('\n') + '\n', 'utf8');
    }
  } catch { /* 收割失败不影响会话结束 */ }
}
process.exit(0);
