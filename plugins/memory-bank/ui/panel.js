'use strict';
// Memory Bank 面板逻辑 v3（由 memory_bank.py 在 resources/read 时内联进 panel.html）。
// 握手：ui/initialize (protocolVersion 2026-01-26) → ui/notifications/initialized；
// 之后 tools/call 经宿主代理到插件 MCP server。
const PROTOCOL_VERSION = '2026-01-26';
const pending = new Map();
let nextId = 1;

function post(msg) { window.parent.postMessage(msg, '*'); }
function notify(method, params) { post({ jsonrpc: '2.0', method, params }); }
function call(method, params) {
  return new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, { resolve, reject });
    post({ jsonrpc: '2.0', id, method, params });
    setTimeout(() => { if (pending.has(id)) { pending.delete(id); reject(new Error(method + ' 超时')); } }, 15000);
  });
}
const tool = (name, args) => call('tools/call', { name, arguments: args })
  .then((r) => {
    if (r.isError) throw new Error((r.content || []).map((c) => c.text).join('\n'));
    return (r.content || []).map((c) => c.text).join('\n');
  });

window.addEventListener('message', (ev) => {
  if (ev.source !== window.parent) return;
  const msg = ev.data;
  if (!msg || msg.jsonrpc !== '2.0') return;
  if (msg.id !== undefined && (msg.result !== undefined || msg.error !== undefined)) {
    const p = pending.get(msg.id);
    if (p) { pending.delete(msg.id); msg.error ? p.reject(new Error(msg.error.message)) : p.resolve(msg.result); }
    return;
  }
  if (msg.method !== undefined) {
    post({ jsonrpc: '2.0', id: msg.id, error: { code: -32601, message: 'not implemented: ' + msg.method } });
  }
});

async function connect() {
  await call('ui/initialize', {
    appInfo: { name: 'Memory Bank', version: '0.8.0' },
    appCapabilities: {},
    protocolVersion: PROTOCOL_VERSION,
  });
  notify('ui/notifications/initialized');
}

// 属性上下文也要安全：引号必须转义（esc 结果会放进 value="…"）
function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }

// ── toast ──
let toastTimer;
function toast(msg, isErr) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'show' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = ''; }, 2400);
}

// ── 相对时间 ──
function relTime(s) {
  if (!s) return '';
  const d = new Date(s.length === 10 ? s + 'T00:00:00' : s);
  if (isNaN(d)) return s;
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
  if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
  if (diff < 86400 * 30) return Math.floor(diff / 86400) + ' 天前';
  return s.slice(0, 10);
}

// ── tag 过滤状态 ──
let activeTag = null;

let refreshSeq = 0;
async function refresh() {
  const seq = ++refreshSeq;   // 旧请求后返回时丢弃，避免慢响应覆盖新结果
  const q = document.getElementById('q').value.trim();
  const scope = document.getElementById('scope').value;
  const sort = document.getElementById('sort').value;
  const list = document.getElementById('list');
  list.innerHTML = '<div class="status">加载中…</div>';
  try {
    const items = JSON.parse(await tool('memory_list', { query: q, scope }));
    if (seq !== refreshSeq) return;
    const filtered = activeTag ? items.filter(i => (i.tags || []).includes(activeTag)) : items;
    filtered.sort((a, b) => sort === 'name' ? a.name.localeCompare(b.name)
      : sort === 'pin' ? ((b.pinned ? 1 : 0) - (a.pinned ? 1 : 0)) || String(b.created).localeCompare(String(a.created))
      : String(b.created).localeCompare(String(a.created)));
    const st = (id, v) => document.getElementById(id).textContent = v;
    st('st-total', items.length); st('st-global', items.filter(i => i.scope === 'global').length);
    st('st-proj', items.filter(i => i.scope !== 'global').length); st('st-pin', items.filter(i => i.pinned).length);
    if (!filtered.length) {
      const why = activeTag ? '无标签 #' + esc(activeTag) + ' 的记忆' :
        (q ? '无匹配「' + esc(q) + '」的记忆' : '（暂无记忆，点「＋ 新增」写入第一条）');
      list.innerHTML = '<div class="status">' + why + '</div>';
      return;
    }
    list.innerHTML = '';
    for (const it of filtered) {
      const card = document.createElement('div');
      card.className = 'card';
      card.style.setProperty('--tcolor', `var(--${it.type || 'project'}, var(--accent))`);
      card.innerHTML =
        '<div class="head"><span class="name">' + esc(it.name) + '</span>' +
        (it.pinned ? '<span title="已 pin，每会话必注入">📌</span>' : '') +
        '<span class="badge t-' + esc(it.type || 'project') + '">' + esc(it.type || '?') + '</span>' +
        '<span class="badge">' + esc(it.scope) + '</span>' +
        '<span class="date" title="' + esc(it.created || '') + '">' + relTime(it.created) + '</span></div>' +
        '<div class="desc">' + esc(it.description) + '</div>' +
        ((it.tags && it.tags.length) ? '<div style="margin-top:4px">' +
          it.tags.map((t) => '<span class="tag" data-tag="' + esc(t) + '">#' + esc(t) + '</span>').join(' ') + '</div>' : '') +
        '<div class="preview">' + esc(it.body.split('\n')[0] || '') + '</div>' +
        '<pre style="display:none">' + esc(it.body) + '</pre>' +
        '<div class="edit" style="display:none">' +
        '<div class="f"><input class="e-desc" value="' + esc(it.description) + '" placeholder="描述"/></div>' +
        '<div class="f"><input class="e-tags" value="' + esc((it.tags || []).join(', ')) + '" placeholder="标签，逗号分隔"/></div>' +
        '<textarea class="e-text">' + esc(it.body) + '</textarea></div>' +
        '<div class="ops">' +
        '<button data-op="copy">复制</button>' +
        '<button data-op="edit">编辑</button>' +
        '<button data-op="save" class="primary" style="display:none">保存修改</button>' +
        '<button data-op="cancel" style="display:none">取消</button>' +
        '<button data-op="pin">' + (it.pinned ? '取消 Pin' : 'Pin') + '</button>' +
        '<button data-op="forget" class="danger">删除</button></div>';
      const pre = card.querySelector('pre'), editBox = card.querySelector('.edit'), preview = card.querySelector('.preview');
      const btn = (op) => card.querySelector('[data-op=' + op + ']');
      card.querySelector('.head').onclick = () => {
        if (editBox.style.display === 'block') return;
        const open = pre.style.display === 'none';
        pre.style.display = open ? 'block' : 'none';
        preview.style.display = open ? 'none' : '';
      };
      for (const el of card.querySelectorAll('.tag[data-tag]')) {
        el.onclick = () => { activeTag = activeTag === el.dataset.tag ? null : el.dataset.tag; refresh(); };
      }
      btn('copy').onclick = async () => {
        try { await navigator.clipboard.writeText(it.body); toast('已复制正文'); }
        catch { toast('复制失败', true); }
      };
      btn('edit').onclick = () => {
        pre.style.display = 'none'; preview.style.display = 'none';
        editBox.style.display = 'block';
        for (const op of ['copy', 'edit', 'pin', 'forget']) btn(op).style.display = 'none';
        btn('save').style.display = btn('cancel').style.display = '';
      };
      btn('cancel').onclick = () => { editBox.style.display = 'none'; refresh(); };
      btn('save').onclick = async () => {
        try {
          await tool('memory_edit', { name: it.name, scope: it.scope,
            text: card.querySelector('.e-text').value, description: card.querySelector('.e-desc').value,
            tags: card.querySelector('.e-tags').value.split(',').map((s) => s.trim()).filter(Boolean) });
          toast('已保存 ' + it.name);
          refresh();
        } catch (err) { toast('保存失败: ' + err.message, true); }
      };
      btn('pin').onclick = async () => {
        try { await tool('memory_pin', { name: it.name, on: !it.pinned, scope: it.scope });
          toast(it.pinned ? '已取消 pin' : '已 pin，每会话必注入'); refresh(); }
        catch (err) { toast('pin 失败: ' + err.message, true); }
      };
      btn('forget').onclick = async () => {
        if (!confirm('删除记忆 ' + it.name + '？（移入 trash，可回溯）')) return;
        try { await tool('memory_forget', { name: it.name, scope: it.scope }); toast('已删除 ' + it.name); refresh(); }
        catch (err) { toast('删除失败: ' + err.message, true); }
      };
      list.appendChild(card);
    }
  } catch (err) {
    if (seq === refreshSeq) list.innerHTML = '<div class="status err">加载失败: ' + esc(err.message) + '</div>';
  }
}

// 新增表单
const form = document.getElementById('form');
document.getElementById('add').onclick = () => {
  const open = form.style.display !== 'block';
  form.style.display = open ? 'block' : 'none';
  if (open) document.getElementById('f-text').focus();
};
document.getElementById('f-cancel').onclick = () => { form.style.display = 'none'; };
document.getElementById('f-save').onclick = async () => {
  const text = document.getElementById('f-text').value.trim();
  if (!text) { toast('正文不能为空', true); return; }
  try {
    await tool('memory_save', { text, type: document.getElementById('f-type').value,
      scope: document.getElementById('f-scope').value });
    document.getElementById('f-text').value = '';
    document.getElementById('f-desc').value = '';
    form.style.display = 'none';
    toast('已保存');
    refresh();
  } catch (err) { toast('保存失败: ' + err.message, true); }
};

// 面板加载即聚焦搜索框；回车立即搜索
const searchBox = document.getElementById('q');
searchBox.focus();
let debounce;
searchBox.addEventListener('input', () => {
  clearTimeout(debounce);
  debounce = setTimeout(refresh, 300);
});
searchBox.addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') { clearTimeout(debounce); refresh(); }
  if (ev.key === 'Escape' && activeTag) { activeTag = null; refresh(); }
});
document.getElementById('scope').onchange = refresh;
document.getElementById('sort').onchange = refresh;
document.getElementById('refresh').onclick = refresh;

// 回收站视图
const trashview = document.getElementById('trashview');
document.getElementById('trashbtn').onclick = () => {
  const open = trashview.style.display === 'none';
  trashview.style.display = open ? 'block' : 'none';
  if (open) loadTrash();
};
async function loadTrash() {
  const tl = document.getElementById('trash-list');
  const st = document.getElementById('trash-status');
  try {
    const items = JSON.parse(await tool('memory_trash', {}));
    if (!items.length) { st.textContent = '（回收站为空）'; tl.innerHTML = ''; return; }
    st.textContent = '回收站 ' + items.length + ' 条：';
    tl.innerHTML = '';
    for (const t of items) {
      const row = document.createElement('div');
      row.className = 'card';
      row.innerHTML =
        '<div class="head"><span class="name">' + esc(t.name) + '</span>' +
        '<span class="badge">' + esc(t.scope) + '</span>' +
        '<span class="date">' + esc(t.deleted_at) + '</span></div>' +
        '<div class="desc">' + esc(t.description) + '</div>' +
        '<div class="ops"><button class="primary">恢复</button></div>';
      row.querySelector('button').onclick = async () => {
        try { await tool('memory_restore', { name: t.name }); toast('已恢复 ' + t.name); loadTrash(); refresh(); }
        catch (err) { toast('恢复失败: ' + err.message, true); }
      };
      tl.appendChild(row);
    }
  } catch (err) {
    st.textContent = '回收站加载失败: ' + err.message;
    st.className = 'status err';
  }
}

connect().then(refresh).catch((err) => {
  document.getElementById('list').innerHTML =
    '<div class="status err">握手失败: ' + esc(err.message) + '<br/>请确认插件 MCP server 已连接并重启过 ZCode。</div>';
});
