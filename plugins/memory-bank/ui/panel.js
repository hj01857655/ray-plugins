'use strict';
// Memory Bank 面板逻辑（由 memory_bank.py 在 resources/read 时内联进 panel.html）。
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
    appInfo: { name: 'Memory Bank', version: '0.7.2' },
    appCapabilities: {},
    protocolVersion: PROTOCOL_VERSION,
  });
  notify('ui/notifications/initialized');
}

// 属性上下文也要安全：引号必须转义（esc 结果会放进 value="…"）
function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }

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
    items.sort((a, b) => sort === 'name' ? a.name.localeCompare(b.name)
      : sort === 'pin' ? ((b.pinned ? 1 : 0) - (a.pinned ? 1 : 0)) || String(b.created).localeCompare(String(a.created))
      : String(b.created).localeCompare(String(a.created)));
    const st = document.getElementById('stats');
    st.textContent = items.length + ' 条 · 全局 ' + items.filter(i => i.scope === 'global').length +
      ' · 项目 ' + items.filter(i => i.scope !== 'global').length +
      ' · 📌 ' + items.filter(i => i.pinned).length;
    if (!items.length) { list.innerHTML = '<div class="status">（暂无记忆）</div>'; return; }
    list.innerHTML = '';
    for (const it of items) {
      const card = document.createElement('div');
      card.className = 'card';
      card.innerHTML =
        '<div class="head"><span class="name">' + esc(it.name) + '</span>' +
        (it.pinned ? '<span title="已 pin，每会话必注入">📌</span>' : '') +
        '<span class="badge t-' + esc(it.type || 'project') + '">' + esc(it.type || '?') + '</span>' +
        '<span class="badge">' + esc(it.scope) + '</span>' +
        (it.created ? '<span class="date">' + esc(it.created) + '</span>' : '') + '</div>' +
        '<div class="desc">' + esc(it.description) + '</div>' +
        ((it.tags && it.tags.length) ? '<div style="margin-top:4px">' +
          it.tags.map((t) => '<span class="tag">#' + esc(t) + '</span>').join(' ') + '</div>' : '') +
        '<pre style="display:none">' + esc(it.body) + '</pre>' +
        '<div class="edit" style="display:none">' +
        '<div class="f"><input class="e-desc" value="' + esc(it.description) + '" placeholder="描述"/></div>' +
        '<div class="f"><input class="e-tags" value="' + esc((it.tags || []).join(', ')) + '" placeholder="标签，逗号分隔"/></div>' +
        '<textarea class="e-text">' + esc(it.body) + '</textarea></div>' +
        '<div class="ops">' +
        '<button data-op="edit">编辑</button>' +
        '<button data-op="save" class="primary" style="display:none">保存修改</button>' +
        '<button data-op="cancel" style="display:none">取消</button>' +
        '<button data-op="pin">' + (it.pinned ? '取消 Pin' : 'Pin') + '</button>' +
        '<button data-op="forget" class="danger">删除</button></div>';
      const pre = card.querySelector('pre'), editBox = card.querySelector('.edit');
      const btn = (op) => card.querySelector('[data-op=' + op + ']');
      card.querySelector('.head').onclick = () => {
        if (editBox.style.display === 'block') return;
        pre.style.display = pre.style.display === 'none' ? 'block' : 'none';
      };
      btn('edit').onclick = () => {
        pre.style.display = 'none';
        editBox.style.display = 'block';
        for (const op of ['edit', 'pin', 'forget']) btn(op).style.display = 'none';
        btn('save').style.display = btn('cancel').style.display = '';
      };
      btn('cancel').onclick = () => { editBox.style.display = 'none'; refresh(); };
      btn('save').onclick = async () => {
        try {
          await tool('memory_edit', { name: it.name, scope: it.scope,
            text: card.querySelector('.e-text').value, description: card.querySelector('.e-desc').value,
            tags: card.querySelector('.e-tags').value.split(',').map((s) => s.trim()).filter(Boolean) });
          refresh();
        } catch (err) { alert('保存失败: ' + err.message); }
      };
      btn('pin').onclick = async () => {
        try { await tool('memory_pin', { name: it.name, on: !it.pinned, scope: it.scope }); refresh(); }
        catch (err) { alert('pin 失败: ' + err.message); }
      };
      btn('forget').onclick = async () => {
        if (!confirm('删除记忆 ' + it.name + '？（移入 trash，可回溯）')) return;
        try { await tool('memory_forget', { name: it.name, scope: it.scope }); refresh(); }
        catch (err) { alert('删除失败: ' + err.message); }
      };
      list.appendChild(card);
    }
  } catch (err) {
    if (seq === refreshSeq) list.innerHTML = '<div class="status err">加载失败: ' + esc(err.message) + '</div>';
  }
}

// 新增表单
const form = document.getElementById('form');
document.getElementById('add').onclick = () => { form.style.display = form.style.display === 'block' ? 'none' : 'block'; };
document.getElementById('f-cancel').onclick = () => { form.style.display = 'none'; };
document.getElementById('f-save').onclick = async () => {
  const text = document.getElementById('f-text').value.trim();
  if (!text) { alert('正文不能为空'); return; }
  try {
    await tool('memory_save', { text, type: document.getElementById('f-type').value,
      scope: document.getElementById('f-scope').value });
    document.getElementById('f-text').value = '';
    document.getElementById('f-desc').value = '';
    form.style.display = 'none';
    refresh();
  } catch (err) { alert('保存失败: ' + err.message); }
};

// 面板加载即聚焦搜索框
const searchBox = document.getElementById('q');
searchBox.focus();

let debounce;
document.getElementById('q').addEventListener('input', () => {
  clearTimeout(debounce);
  debounce = setTimeout(refresh, 300);
});
document.getElementById('scope').onchange = refresh;
document.getElementById('sort').onchange = refresh;
document.getElementById('refresh').onclick = refresh;

// 回收站视图
const trashview = document.getElementById('trashview');
document.getElementById('trashbtn').onclick = () => {
  trashview.style.display = trashview.style.display === 'none' ? 'block' : 'none';
  if (trashview.style.display === 'block') loadTrash();
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
        try { await tool('memory_restore', { name: t.name }); loadTrash(); refresh(); }
        catch (err) { alert('恢复失败: ' + err.message); }
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
