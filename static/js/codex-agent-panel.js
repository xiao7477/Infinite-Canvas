/* =========================================================================
 * Codex Agent Panel（独立 IIFE，挂在 gpt-chat.html 上）
 * 不动 canvas.js / smart-canvas.js / gpt-chat.html 主体
 * 端到端流程：
 *   1. 点 #cm-fab → 显示侧栏
 *   2. 点 #cm-proj 弹出项目下拉（从 /api/codex-agent/sessions/list 反推）
 *   3. 选项目 → POST /api/codex-agent/board/open { project_dir } → 拿 threadId
 *   4. 输入框打字 → POST /api/codex-agent/turn { project_dir, text }
 *      → ReadableStream 读 SSE → 解析每个 data: → 渲染
 * ========================================================================= */

(function () {
  'use strict';

  const $ = (s) => document.querySelector(s);

  // ---------------- 状态 ----------------
  const state = {
    open: false,
    status: 'idle',      // idle | ready | busy | error
    projectDir: '',
    threadId: '',
    messages: [],         // [{role: 'user'|'bot', blocks: [{type, ...}]}]
    projects: [],         // [{project_dir, session_count, last_active}]
    sessions: [],         // 当前 project_dir 下的 session
    projPopOpen: false,
    histPopOpen: false,
  };

  // 当前流式累积的 message
  let currentAgentMsgId = null;
  let currentAgentText = '';
  let currentReasoningId = null;
  let currentReasoningText = '';
  let currentToolId = null;

  // ---------------- 注入 UI ----------------
  function inject() {
    if (document.getElementById('cm-fab')) return;  // 防重复注入

    // 浮动按钮
    const fab = document.createElement('div');
    fab.id = 'cm-fab';
    fab.title = 'Codex Agent';
    fab.textContent = '💬';
    fab.addEventListener('click', togglePanel);
    document.body.appendChild(fab);

    // 侧栏
    const panel = document.createElement('div');
    panel.id = 'cm-panel';
    panel.innerHTML = `
      <div class="cm-head">
        <span class="cm-status" id="cm-status" title="Codex 状态"></span>
        <button class="cm-proj" id="cm-proj" title="点击选择项目文件夹">
          <span class="cm-proj-empty">— 选择项目 —</span>
        </button>
        <button class="cm-icon-btn" id="cm-new" title="新对话">＋</button>
        <button class="cm-icon-btn" id="cm-history" title="历史会话">≡</button>
        <button class="cm-icon-btn" id="cm-close" title="关闭">×</button>
      </div>
      <div class="cm-popover" id="cm-proj-pop"></div>
      <div class="cm-popover" id="cm-hist-pop"></div>
      <div class="cm-body" id="cm-body">
        <div class="cm-empty">
          选择一个项目文件夹，<br>然后开始和 Codex 对话。
        </div>
      </div>
      <div class="cm-foot">
        <textarea class="cm-input" id="cm-input" placeholder="输入消息，回车发送（Shift+Enter 换行）" disabled></textarea>
        <div class="cm-foot-row">
          <span class="cm-foot-hint" id="cm-hint">未选项目</span>
          <button class="cm-send" id="cm-send" disabled>发送</button>
        </div>
      </div>
    `;
    document.body.appendChild(panel);

    // 事件绑定
    $('#cm-close').addEventListener('click', togglePanel);
    $('#cm-proj').addEventListener('click', toggleProjPop);
    $('#cm-new').addEventListener('click', onNewSession);
    $('#cm-history').addEventListener('click', toggleHistPop);
    $('#cm-send').addEventListener('click', onSend);
    $('#cm-input').addEventListener('keydown', onInputKey);
    document.addEventListener('click', onDocClick);
  }

  // ---------------- 面板开关 ----------------
  function togglePanel() {
    state.open = !state.open;
    $('#cm-panel').classList.toggle('cm-open', state.open);
    if (state.open) {
      loadProjects();  // 打开时拉一次
    }
  }

  // ---------------- 项目下拉 ----------------
  async function loadProjects() {
    try {
      const r = await fetch('/api/codex-agent/sessions/list');
      const d = await r.json();
      const byProj = d.by_project || {};
      state.projects = Object.keys(byProj)
        .filter(k => k !== '(unknown)')
        .map(k => ({
          project_dir: k,
          session_count: byProj[k].length,
          last_active: byProj[k][0]?.started_at || '',
        }));
      state.projects.sort((a, b) => (b.last_active || '').localeCompare(a.last_active || ''));
    } catch (e) {
      console.error('loadProjects failed', e);
    }
  }

  function toggleProjPop() {
    state.projPopOpen = !state.projPopOpen;
    state.histPopOpen = false;
    renderProjPop();
  }

  function renderProjPop() {
    const pop = $('#cm-proj-pop');
    if (!state.projPopOpen) {
      pop.classList.remove('cm-popover-open');
      return;
    }
    pop.classList.add('cm-popover-open');
    const html = ['<div class="cm-popover-pick" id="cm-pick-new">＋ 选择其他文件夹...</div>'];
    if (state.projects.length === 0) {
      html.push('<div class="cm-popover-empty">还没有项目，去 Codex 软件里聊过一次就有了</div>');
    } else {
      for (const p of state.projects) {
        const active = p.project_dir === state.projectDir ? ' cm-item-active' : '';
        html.push(`
          <div class="cm-popover-item${active}" data-dir="${escapeAttr(p.project_dir)}">
            📁 ${escapeHtml(p.project_dir)}
            <span class="cm-popover-meta">${p.session_count} 个会话 · 最近 ${p.last_active || '?'}</span>
          </div>
        `);
      }
    }
    pop.innerHTML = html.join('');
    pop.querySelectorAll('.cm-popover-item').forEach(el => {
      el.addEventListener('click', () => {
        selectProject(el.getAttribute('data-dir'));
        state.projPopOpen = false;
        renderProjPop();
      });
    });
    const pickNew = $('#cm-pick-new');
    if (pickNew) pickNew.addEventListener('click', pickDirDialog);
  }

  // ---------------- 选目录（弹原生输入） ----------------
  function pickDirDialog() {
    // 用 prompt 让用户输入绝对路径（macOS Finder 集成留给后续）
    const p = prompt('输入项目文件夹绝对路径：', state.projectDir || '/Users/a000/Documents/');
    if (!p) return;
    selectProject(p);
    state.projPopOpen = false;
    renderProjPop();
  }

  // ---------------- 选项目 → 调 board/open ----------------
  async function selectProject(dir) {
    state.status = 'busy';
    setStatusUI();
    try {
      const r = await fetch('/api/codex-agent/board/open', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_dir: dir }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || 'board/open failed');
      state.projectDir = d.project_dir;
      state.threadId = d.thread_id;
      state.status = 'ready';
      // 拉历史
      await loadSessions(dir);
    } catch (e) {
      console.error('selectProject failed', e);
      state.status = 'error';
      alert('启动 Codex 失败：' + e.message);
    }
    setStatusUI();
    renderProjPop();
  }

  // ---------------- 历史下拉 ----------------
  async function loadSessions(dir) {
    try {
      const r = await fetch('/api/codex-agent/sessions/list?project_dir=' + encodeURIComponent(dir));
      const d = await r.json();
      const list = (d.by_project || {})[dir] || [];
      state.sessions = list;
    } catch (e) {
      console.error('loadSessions failed', e);
      state.sessions = [];
    }
  }

  function toggleHistPop() {
    if (!state.projectDir) {
      alert('先选个项目');
      return;
    }
    state.histPopOpen = !state.histPopOpen;
    state.projPopOpen = false;
    renderHistPop();
  }

  function renderHistPop() {
    const pop = $('#cm-hist-pop');
    if (!state.histPopOpen) {
      pop.classList.remove('cm-popover-open');
      return;
    }
    pop.classList.add('cm-popover-open');
    if (state.sessions.length === 0) {
      pop.innerHTML = '<div class="cm-popover-empty">该项目暂无历史会话</div>';
      return;
    }
    const html = state.sessions.map(s => `
      <div class="cm-popover-item" data-sid="${escapeAttr(s.session_id)}">
        💬 ${escapeHtml(s.preview || '(空)')}
        <span class="cm-popover-meta">${s.started_at} · ${s.model || '?'}</span>
      </div>
    `).join('');
    pop.innerHTML = html;
    pop.querySelectorAll('.cm-popover-item').forEach(el => {
      el.addEventListener('click', () => {
        const sid = el.getAttribute('data-sid');
        switchSession(sid);
        state.histPopOpen = false;
        renderHistPop();
      });
    });
  }

  async function switchSession(sessionId) {
    if (!sessionId) return;
    state.status = 'busy';
    setStatusUI();
    try {
      const r = await fetch('/api/codex-agent/board/open', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_dir: state.projectDir, thread_id: sessionId }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || 'board/open (resume) failed');
      state.threadId = d.thread_id;
      state.status = 'ready';
      // 清空当前消息流（接历史）
      state.messages = [];
      renderBody();
    } catch (e) {
      console.error('switchSession failed', e);
      state.status = 'error';
      alert('恢复会话失败：' + e.message);
    }
    setStatusUI();
  }

  // ---------------- 新对话 ----------------
  async function onNewSession() {
    if (!state.projectDir) {
      alert('先选个项目');
      return;
    }
    // 关闭旧 → 开新
    try {
      await fetch('/api/codex-agent/board/close', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_dir: state.projectDir }),
      });
    } catch {}
    state.status = 'busy';
    setStatusUI();
    try {
      const r = await fetch('/api/codex-agent/board/open', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_dir: state.projectDir }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || 'board/open failed');
      state.threadId = d.thread_id;
      state.status = 'ready';
      state.messages = [];
      renderBody();
    } catch (e) {
      state.status = 'error';
      alert('新对话失败：' + e.message);
    }
    setStatusUI();
  }

  // ---------------- 发消息 → SSE ----------------
  function onInputKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      onSend();
    }
  }

  async function onSend() {
    const input = $('#cm-input');
    const text = input.value.trim();
    if (!text || !state.projectDir || state.status === 'busy') return;

    state.messages.push({ role: 'user', blocks: [{ type: 'text', text }] });
    renderBody();
    input.value = '';

    // 准备一个 bot 消息容器
    const botMsg = { role: 'bot', blocks: [] };
    state.messages.push(botMsg);
    renderBody();

    state.status = 'busy';
    setStatusUI();

    currentAgentMsgId = null;
    currentAgentText = '';
    currentReasoningId = null;
    currentReasoningText = '';
    currentToolId = null;

    try {
      const r = await fetch('/api/codex-agent/turn', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_dir: state.projectDir, text, attachments: [] }),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d.detail || `HTTP ${r.status}`);
      }
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        // SSE: 一次拿到的可能是多条 data:，按 \n\n 拆
        let idx;
        while ((idx = buf.indexOf('\n\n')) !== -1) {
          const eventBlock = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          parseSSEBlock(eventBlock, botMsg);
        }
      }
    } catch (e) {
      botMsg.blocks.push({ type: 'error', text: e.message });
      renderBody();
    }

    state.status = 'ready';
    setStatusUI();
  }

  function parseSSEBlock(block, botMsg) {
    const lines = block.split('\n');
    for (const line of lines) {
      if (!line.startsWith('data:')) continue;
      const json = line.slice(5).trim();
      if (!json) continue;
      let msg;
      try { msg = JSON.parse(json); } catch { continue; }
      handleCodexEvent(msg, botMsg);
    }
  }

  function handleCodexEvent(msg, botMsg) {
    const method = msg.method || '';
    const params = msg.params || {};

    if (method === 'item/started') {
      const item = params.item || {};
      const type = item.type;
      if (type === 'userMessage') {
        // 已经在用户输入时加过了，忽略
      } else if (type === 'agentMessage') {
        currentAgentMsgId = item.id;
        currentAgentText = item.text || '';
        botMsg.blocks.push({ type: 'text', text: currentAgentText, id: item.id, streaming: true });
      } else if (type === 'reasoning') {
        currentReasoningId = item.id;
        currentReasoningText = item.summary || item.text || '';
        botMsg.blocks.push({ type: 'thinking', text: currentReasoningText, id: item.id });
      } else if (type === 'commandExecution') {
        currentToolId = item.id;
        botMsg.blocks.push({
          type: 'tool', text: `🔧 ${item.command || '(命令)'}`,
          status: 'running', id: item.id,
        });
      } else if (type === 'imageGeneration') {
        botMsg.blocks.push({
          type: 'image', path: item.savedPath || item.path || '',
          prompt: item.prompt || '',
          status: 'generating', id: item.id,
        });
      } else if (type === 'todoList' || type === 'plan') {
        botMsg.blocks.push({ type: 'todo', items: item.items || [] });
      } else if (type === 'mcpToolCall' || type === 'webSearch' || type === 'fileChange') {
        botMsg.blocks.push({
          type: 'tool', text: `🔧 ${type}: ${item.name || item.query || item.path || ''}`,
          status: 'running', id: item.id,
        });
      }
      renderBody();
    } else if (method === 'item/agentMessage/delta' || method === 'item/reasoning/summaryTextDelta') {
      const delta = params.delta || '';
      if (method === 'item/agentMessage/delta') {
        currentAgentText += delta;
        const blk = botMsg.blocks.find(b => b.id === currentAgentMsgId && b.type === 'text');
        if (blk) blk.text = currentAgentText;
      } else {
        currentReasoningText += delta;
        const blk = botMsg.blocks.find(b => b.id === currentReasoningId && b.type === 'thinking');
        if (blk) blk.text = currentReasoningText;
      }
      renderBody();
    } else if (method === 'item/commandExecution/outputDelta' || method === 'item/commandExecution/terminalInteraction') {
      const out = params.delta || params.output || '';
      const blk = botMsg.blocks.find(b => b.id === currentToolId && b.type === 'tool');
      if (blk) blk.text += (out ? '\n' + out : '');
      renderBody();
    } else if (method === 'item/completed') {
      const item = params.item || {};
      const blk = botMsg.blocks.find(b => b.id === item.id);
      if (blk) {
        if (blk.type === 'text') {
          blk.text = item.text || blk.text;
          blk.streaming = false;
        } else if (blk.type === 'image') {
          blk.path = item.savedPath || item.path || blk.path;
          blk.status = 'done';
        } else if (blk.type === 'tool') {
          blk.status = 'done';
        }
      }
      renderBody();
    } else if (method === 'turn/completed') {
      // 标记所有 streaming=false
      botMsg.blocks.forEach(b => { if (b.streaming) b.streaming = false; });
      renderBody();
    } else if (method === 'error' || method === 'fatal' || method === 'turn/timeout') {
      botMsg.blocks.push({ type: 'error', text: params.message || params.error || JSON.stringify(params) });
      renderBody();
    }
  }

  // ---------------- 渲染 ----------------
  function setStatusUI() {
    const dot = $('#cm-status');
    dot.classList.remove('cm-status-ready', 'cm-status-busy', 'cm-status-error');
    if (state.status === 'ready') dot.classList.add('cm-status-ready');
    else if (state.status === 'busy') dot.classList.add('cm-status-busy');
    else if (state.status === 'error') dot.classList.add('cm-status-error');

    const fab = $('#cm-fab');
    fab.classList.toggle('cm-fab-busy', state.status === 'busy');

    const proj = $('#cm-proj');
    if (state.projectDir) {
      proj.innerHTML = `<span title="${escapeAttr(state.projectDir)}">📁 ${escapeHtml(shortPath(state.projectDir))}</span>`;
    } else {
      proj.innerHTML = '<span class="cm-proj-empty">— 选择项目 —</span>';
    }

    const input = $('#cm-input');
    const send = $('#cm-send');
    const enabled = !!state.projectDir && state.status !== 'busy';
    input.disabled = !state.projectDir;
    send.disabled = !enabled;

    const hint = $('#cm-hint');
    if (!state.projectDir) hint.textContent = '未选项目';
    else if (state.status === 'busy') hint.textContent = 'Codex 正在思考...';
    else hint.textContent = `thread: ${(state.threadId || '').slice(0, 8)}…`;
  }

  function renderBody() {
    const body = $('#cm-body');
    if (state.messages.length === 0) {
      body.innerHTML = '<div class="cm-empty">选择一个项目文件夹，<br>然后开始和 Codex 对话。</div>';
      return;
    }
    const html = state.messages.map(msg => renderMessage(msg)).join('');
    body.innerHTML = html;
    // 滚动到底
    body.scrollTop = body.scrollHeight;
  }

  function renderMessage(msg) {
    if (msg.role === 'user') {
      return `<div class="cm-msg"><div class="cm-msg-user">${escapeHtml(msg.blocks[0]?.text || '')}</div></div>`;
    }
    const blocks = msg.blocks.map(renderBlock).join('');
    return `<div class="cm-msg">${blocks}</div>`;
  }

  function renderBlock(b) {
    if (b.type === 'text') {
      return `<div class="cm-msg-bot">${escapeHtml(b.text || '')}${b.streaming ? ' ▍' : ''}</div>`;
    }
    if (b.type === 'thinking') {
      const t = b.text || '';
      const truncated = t.length > 200 ? '…' + t.slice(-200) : t;
      return `<div class="cm-thinking" title="${escapeAttr(t)}">${escapeHtml(truncated)}</div>`;
    }
    if (b.type === 'tool') {
      return `<div class="cm-msg-tool">${escapeHtml(b.text || '')}${b.status === 'running' ? ' ⏳' : ' ✅'}</div>`;
    }
    if (b.type === 'image') {
      if (b.status === 'generating' || !b.path) {
        return `<div class="cm-msg-img"><div class="cm-msg-tool">🖼 生成中... ${escapeHtml(b.prompt || '')}</div></div>`;
      }
      return `<div class="cm-msg-img"><a href="/api/codex-agent/file/view?path=${encodeURIComponent(b.path)}" target="_blank"><img src="/api/codex-agent/file/view?path=${encodeURIComponent(b.path)}" alt="${escapeAttr(b.prompt || '')}"></a></div>`;
    }
    if (b.type === 'todo') {
      const items = (b.items || []).map(i => `<li>${escapeHtml(i.status || '')} ${escapeHtml(i.step || i.text || '')}</li>`).join('');
      return `<div class="cm-msg-tool">📋 <ul>${items}</ul></div>`;
    }
    if (b.type === 'error') {
      return `<div class="cm-msg-error">❌ ${escapeHtml(b.text || '')}</div>`;
    }
    return '';
  }

  // ---------------- 杂项 ----------------
  function shortPath(p) {
    if (!p) return '';
    if (p.length <= 38) return p;
    return '…' + p.slice(-35);
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function escapeAttr(s) { return escapeHtml(s); }

  function onDocClick(e) {
    // 点外部关掉 popover
    if (state.projPopOpen) {
      const pop = $('#cm-proj-pop');
      const btn = $('#cm-proj');
      if (!pop.contains(e.target) && !btn.contains(e.target)) {
        state.projPopOpen = false;
        renderProjPop();
      }
    }
    if (state.histPopOpen) {
      const pop = $('#cm-hist-pop');
      const btn = $('#cm-history');
      if (!pop.contains(e.target) && !btn.contains(e.target)) {
        state.histPopOpen = false;
        renderHistPop();
      }
    }
  }

  // ---------------- 启动 ----------------
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', inject);
  } else {
    inject();
  }
})();
