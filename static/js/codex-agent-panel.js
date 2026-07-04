/* =========================================================================
 * Codex Agent Panel（独立 IIFE，挂在 canvas.html / smart-canvas.html）
 * 不动 canvas.js / smart-canvas.js / 原 html 主体
 *
 * 阶段 3.2 修复：
 *   - 修 [object Object]：所有 text 字段先 String() 防御
 *   - image block 空 path 不渲染 <img>，改占位
 *   - 切历史会话时调 /api/codex-agent/threads/replay 回放历史消息
 * ========================================================================= */

(function () {
  'use strict';

  const $ = (s) => document.querySelector(s);

  // ---------------- 状态 ----------------
  const state = {
    open: false,
    status: 'idle',
    projectDir: '',
    threadId: '',
    messages: [],         // [{role, blocks: [...]}]
    projects: [],
    sessions: [],
    attachments: [],      // [{url, name}] 待发给 Codex 的图附件
    projPopOpen: false,
    histPopOpen: false,
  };

  let currentAgentMsgId = null;
  let currentAgentText = '';
  let currentReasoningId = null;
  let currentReasoningText = '';
  let currentToolId = null;

  // ---------------- 工具：安全字符串化 ----------------
  function safeStr(v, fallback = '') {
    if (v == null) return fallback;
    if (typeof v === 'string') return v;
    if (typeof v === 'number' || typeof v === 'boolean') return String(v);
    try { return JSON.stringify(v); } catch { return fallback; }
  }
  function escapeHtml(s) {
    return safeStr(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function escapeAttr(s) { return escapeHtml(safeStr(s)); }

  // ---------------- 注入 UI ----------------
  function inject() {
    if (document.getElementById('cm-fab')) return;

    const fab = document.createElement('div');
    fab.id = 'cm-fab';
    fab.title = 'Codex Agent';
    fab.textContent = '💬';
    fab.addEventListener('click', togglePanel);
    document.body.appendChild(fab);

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
        <div class="cm-empty">选择一个项目文件夹，<br>然后开始和 Codex 对话。</div>
      </div>
      <div class="cm-foot">
        <div class="cm-attach">
          <button class="cm-attach-btn" id="cm-attach-canvas" title="已点选则加已选；否则加画布里所有图">🎨 画布图 (0)</button>
          <button class="cm-attach-btn" id="cm-attach-url" title="粘贴图 URL 或本地路径">＋ URL</button>
          <span class="cm-attach-list" id="cm-attach-list"></span>
        </div>
        <textarea class="cm-input" id="cm-input" placeholder="输入消息，回车发送（Shift+Enter 换行）" disabled></textarea>
        <div class="cm-foot-row">
          <span class="cm-foot-hint" id="cm-hint">未选项目</span>
          <button class="cm-send" id="cm-send" disabled>发送</button>
        </div>
      </div>
    `;
    document.body.appendChild(panel);

    $('#cm-close').addEventListener('click', togglePanel);
    $('#cm-proj').addEventListener('click', toggleProjPop);
    $('#cm-new').addEventListener('click', onNewSession);
    $('#cm-history').addEventListener('click', toggleHistPop);
    $('#cm-send').addEventListener('click', onSend);
    $('#cm-input').addEventListener('keydown', onInputKey);
    $('#cm-attach-canvas').addEventListener('click', onAttachFromCanvas);
    $('#cm-attach-url').addEventListener('click', onAttachFromUrl);
    document.addEventListener('click', onDocClick);
  }

  function togglePanel() {
    state.open = !state.open;
    $('#cm-panel').classList.toggle('cm-open', state.open);
    if (state.open) loadProjects();
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
    } catch (e) { console.error('loadProjects failed', e); }
  }

  function toggleProjPop() {
    state.projPopOpen = !state.projPopOpen;
    state.histPopOpen = false;
    renderProjPop();
  }

  function renderProjPop() {
    const pop = $('#cm-proj-pop');
    if (!state.projPopOpen) { pop.classList.remove('cm-popover-open'); return; }
    pop.classList.add('cm-popover-open');
    const html = ['<div class="cm-popover-pick" id="cm-pick-new">＋ 选择其他文件夹…</div>'];
    if (state.projects.length === 0) {
      html.push('<div class="cm-popover-empty">还没有项目，去 Codex 软件里聊过一次就有了</div>');
    } else {
      for (const p of state.projects) {
        const active = p.project_dir === state.projectDir ? ' cm-item-active' : '';
        html.push(`<div class="cm-popover-item${active}" data-dir="${escapeAttr(p.project_dir)}">📁 ${escapeHtml(p.project_dir)}<span class="cm-popover-meta">${p.session_count} 个 · 最近 ${p.last_active || '?'}</span></div>`);
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

  function pickDirDialog() {
    const p = prompt('输入项目文件夹绝对路径：', state.projectDir || '/Users/a000/Documents/');
    if (!p) return;
    selectProject(p);
    state.projPopOpen = false;
    renderProjPop();
  }

  // ---------------- 选项目 / 新对话 ----------------
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
      await loadSessions(dir);
      // 自动接上最近的会话历史（不点历史也能看到）
      if (state.sessions.length > 0 && state.sessions[0].session_id) {
        try {
          const rr = await fetch('/api/codex-agent/threads/replay?session_id=' + encodeURIComponent(state.sessions[0].session_id));
          if (rr.ok) {
            const dd = await rr.json();
            state.messages = dd.messages || [];
            renderBody();
          }
        } catch {}
      }
    } catch (e) {
      console.error('selectProject failed', e);
      state.status = 'error';
      alert('启动 Codex 失败：' + e.message);
    }
    setStatusUI();
    renderProjPop();
  }

  async function onNewSession() {
    if (!state.projectDir) { alert('先选个项目'); return; }
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

  // ---------------- 历史下拉 ----------------
  async function loadSessions(dir) {
    try {
      const r = await fetch('/api/codex-agent/sessions/list?project_dir=' + encodeURIComponent(dir));
      const d = await r.json();
      state.sessions = (d.by_project || {})[dir] || [];
    } catch (e) { console.error('loadSessions failed', e); state.sessions = []; }
  }

  function toggleHistPop() {
    if (!state.projectDir) { alert('先选个项目'); return; }
    state.histPopOpen = !state.histPopOpen;
    state.projPopOpen = false;
    renderHistPop();
  }

  function renderHistPop() {
    const pop = $('#cm-hist-pop');
    if (!state.histPopOpen) { pop.classList.remove('cm-popover-open'); return; }
    pop.classList.add('cm-popover-open');
    if (state.sessions.length === 0) {
      pop.innerHTML = '<div class="cm-popover-empty">该项目暂无历史会话</div>';
      return;
    }
    pop.innerHTML = state.sessions.map(s => `
      <div class="cm-popover-item" data-sid="${escapeAttr(s.session_id)}">
        💬 ${escapeHtml(s.preview || '(空)')}
        <span class="cm-popover-meta">${s.started_at} · ${s.model || '?'}</span>
      </div>
    `).join('');
    pop.querySelectorAll('.cm-popover-item').forEach(el => {
      el.addEventListener('click', () => {
        const sid = el.getAttribute('data-sid');
        state.histPopOpen = false;
        renderHistPop();
        switchSession(sid);
      });
    });
  }

  async function switchSession(sessionId) {
    if (!sessionId) return;
    state.status = 'busy';
    setStatusUI();
    try {
      // 1. resume thread
      const r = await fetch('/api/codex-agent/board/open', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_dir: state.projectDir, thread_id: sessionId }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || 'board/open (resume) failed');
      state.threadId = d.thread_id;
      state.status = 'ready';
      // 2. replay 历史
      await replaySession(sessionId);
    } catch (e) {
      console.error('switchSession failed', e);
      state.status = 'error';
      alert('恢复会话失败：' + e.message);
    }
    setStatusUI();
  }

  async function replaySession(sessionId) {
    try {
      const r = await fetch('/api/codex-agent/threads/replay?session_id=' + encodeURIComponent(sessionId));
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        console.warn('replay failed', d);
        state.messages = [];
        renderBody();
        return;
      }
      const d = await r.json();
      state.messages = d.messages || [];
      renderBody();
    } catch (e) {
      console.error('replaySession failed', e);
      state.messages = [];
      renderBody();
    }
  }

  // ---------------- 附件管理 ----------------
  function onAttachFromCanvas() {
    // 优先读 .node.selected（用户已点选的）；没有就 fallback 到所有 .image-node
    // 这样不依赖用户必须先点中 — 导进画布的图一键就能用
    let targets = Array.from(document.querySelectorAll('.node.selected'));
    let mode = 'selected';
    if (targets.length === 0) {
      // fallback：所有 image-node
      targets = Array.from(document.querySelectorAll('.image-node'));
      mode = 'all';
      if (targets.length === 0) {
        showHint('画布里没图节点（先导图）');
        return;
      }
    }

    let count = 0;
    let skippedNoImg = 0;
    targets.forEach(el => {
      const img = el.querySelector('img');
      if (!img) { skippedNoImg++; return; }
      const src = safeStr(img.src);
      if (!src || src.startsWith('data:')) return;
      try {
        const abs = new URL(src, window.location.origin).href;
        if (!state.attachments.find(a => a.url === abs)) {
          state.attachments.push({ url: abs, name: shortPath(src) });
          count++;
        }
      } catch {}
    });

    if (count === 0) {
      showHint(`找到 ${targets.length} 个节点但都没 <img>（${{selected:'已选', all:'画布所有'}[mode]}）`);
    } else if (mode === 'all' && skippedNoImg) {
      showHint(`已加 ${count} 张图（${skippedNoImg} 个非图片节点跳过）`);
    } else {
      showHint(`已加 ${count} 张图`);
    }
    renderAttach();
  }

  function onAttachFromUrl() {
    const u = prompt('输入图 URL 或本地绝对路径：');
    if (!u) return;
    const url = u.trim();
    if (!url) return;
    // 相对路径补全
    let final = url;
    if (url.startsWith('/') || (!url.startsWith('http') && !url.startsWith('file://'))) {
      try { final = new URL(url, window.location.origin).href; } catch {}
    }
    if (state.attachments.find(a => a.url === final)) {
      alert('已附加');
      return;
    }
    state.attachments.push({ url: final, name: shortPath(url) });
    renderAttach();
  }

  function removeAttach(idx) {
    state.attachments.splice(idx, 1);
    renderAttach();
  }

  function renderAttach() {
    const list = $('#cm-attach-list');
    const btn = $('#cm-attach-canvas');
    if (btn) btn.textContent = `🎨 画布图 (${state.attachments.length})`;
    if (!list) return;
    list.innerHTML = state.attachments.map((a, i) =>
      `<span class="cm-attach-badge" title="${escapeAttr(a.url)}">${escapeHtml(a.name)}<span class="cm-attach-x" data-i="${i}">×</span></span>`
    ).join('');
    list.querySelectorAll('.cm-attach-x').forEach(x => {
      x.addEventListener('click', e => {
        e.stopPropagation();
        removeAttach(parseInt(x.getAttribute('data-i'), 10));
      });
    });
  }

  // ---------------- 发消息 → SSE ----------------
  function onInputKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); onSend(); }
  }

  // 临时提示（不弹窗，用底部 hint 区）
  let hintTimer = null;
  function showHint(msg, ms = 4000) {
    const hint = $('#cm-hint');
    if (!hint) return;
    hint.textContent = msg;
    hint.style.color = '#f59e0b';
    if (hintTimer) clearTimeout(hintTimer);
    hintTimer = setTimeout(() => {
      hint.style.color = '';
      setStatusUI();
    }, ms);
  }

  async function onSend() {
    const input = $('#cm-input');
    const text = input.value.trim();
    if (!text || !state.projectDir || state.status === 'busy') return;

    // 收集 attachments（深拷贝后清空）
    const attachUrls = state.attachments.map(a => a.url);
    state.attachments = [];
    renderAttach();

    state.messages.push({ role: 'user', blocks: [{ type: 'text', text: safeStr(text) }] });
    if (attachUrls.length) {
      state.messages[state.messages.length - 1].blocks.push({ type: 'attach', count: attachUrls.length });
    }
    renderBody();
    input.value = '';

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
        body: JSON.stringify({ project_dir: state.projectDir, text, attachments: attachUrls }),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(safeStr(d.detail) || `HTTP ${r.status}`);
      }
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf('\n\n')) !== -1) {
          const block = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          parseSSEBlock(block, botMsg);
        }
      }
    } catch (e) {
      botMsg.blocks.push({ type: 'error', text: safeStr(e.message || e) });
      renderBody();
    }
    state.status = 'ready';
    setStatusUI();
  }

  function parseSSEBlock(block, botMsg) {
    for (const line of block.split('\n')) {
      if (!line.startsWith('data:')) continue;
      const json = line.slice(5).trim();
      if (!json || json === '[DONE]') continue;
      let msg;
      try { msg = JSON.parse(json); } catch { continue; }
      handleCodexEvent(msg, botMsg);
    }
  }

  function handleCodexEvent(msg, botMsg) {
    const method = safeStr(msg.method);
    const params = msg.params || {};

    if (method === 'item/started') {
      const item = params.item || {};
      const type = safeStr(item.type);

      if (type === 'agentMessage') {
        currentAgentMsgId = item.id;
        currentAgentText = safeStr(item.text);
        botMsg.blocks.push({ type: 'text', text: currentAgentText, id: item.id, streaming: true });
      } else if (type === 'reasoning') {
        currentReasoningId = item.id;
        currentReasoningText = safeStr(item.summary) || safeStr(item.text);
        botMsg.blocks.push({ type: 'thinking', text: currentReasoningText, id: item.id });
      } else if (type === 'commandExecution') {
        currentToolId = item.id;
        botMsg.blocks.push({
          type: 'tool', text: `🔧 ${safeStr(item.command) || '(命令)'}`,
          status: 'running', id: item.id,
        });
      } else if (type === 'imageGeneration') {
        botMsg.blocks.push({
          type: 'image', path: safeStr(item.savedPath) || safeStr(item.path) || '',
          prompt: safeStr(item.prompt), status: 'generating', id: item.id,
        });
      } else if (type === 'todoList' || type === 'plan') {
        const items = Array.isArray(item.items) ? item.items : [];
        botMsg.blocks.push({ type: 'todo', items });
      } else if (type === 'mcpToolCall' || type === 'webSearch' || type === 'fileChange') {
        const label = safeStr(item.name) || safeStr(item.query) || safeStr(item.path) || '';
        botMsg.blocks.push({ type: 'tool', text: `🔧 ${type}: ${label}`, status: 'running', id: item.id });
      }
      renderBody();
    } else if (method === 'item/agentMessage/delta' || method === 'item/reasoning/summaryTextDelta' || method === 'item/reasoning/textDelta') {
      const delta = safeStr(params.delta);
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
    } else if (method === 'item/commandExecution/outputDelta') {
      const out = safeStr(params.delta);
      const blk = botMsg.blocks.find(b => b.id === currentToolId && b.type === 'tool');
      if (blk) blk.text += out ? '\n' + out : '';
      renderBody();
    } else if (method === 'item/completed') {
      const item = params.item || {};
      const blk = botMsg.blocks.find(b => b.id === item.id);
      if (blk) {
        if (blk.type === 'text') {
          blk.text = safeStr(item.text) || blk.text;
          blk.streaming = false;
        } else if (blk.type === 'image') {
          blk.path = safeStr(item.savedPath) || safeStr(item.path) || blk.path;
          blk.status = 'done';
        } else if (blk.type === 'tool') {
          blk.status = 'done';
        }
      }
      renderBody();
    } else if (method === 'turn/completed') {
      botMsg.blocks.forEach(b => { if (b.streaming) b.streaming = false; });
      renderBody();
    } else if (method === 'error' || method === 'fatal' || method === 'turn/timeout') {
      const errText = safeStr(params.message) || safeStr(params.error) || JSON.stringify(params || {});
      botMsg.blocks.push({ type: 'error', text: errText });
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

    $('#cm-fab').classList.toggle('cm-fab-busy', state.status === 'busy');

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
    else if (state.status === 'busy') hint.textContent = 'Codex 正在思考…';
    else hint.textContent = `thread: ${(state.threadId || '').slice(0, 8)}…`;
  }

  function renderBody() {
    const body = $('#cm-body');
    if (state.messages.length === 0) {
      body.innerHTML = '<div class="cm-empty">选择一个项目文件夹，<br>然后开始和 Codex 对话。</div>';
      return;
    }
    body.innerHTML = state.messages.map(renderMessage).join('');
    body.scrollTop = body.scrollHeight;
  }

  function renderMessage(msg) {
    if (msg.role === 'user') {
      const userBlock = msg.blocks.find(b => b.type === 'text');
      const attachBlock = msg.blocks.find(b => b.type === 'attach');
      let html = '';
      if (attachBlock) {
        html += `<div class="cm-msg-attach">📎 ${escapeHtml(safeStr(attachBlock.count))} 个附件</div>`;
      }
      html += `<div class="cm-msg-user">${escapeHtml(safeStr(userBlock?.text))}</div>`;
      return `<div class="cm-msg">${html}</div>`;
    }
    return `<div class="cm-msg">${msg.blocks.map(renderBlock).join('')}</div>`;
  }

  function renderBlock(b) {
    const type = b.type;
    if (type === 'text') {
      return `<div class="cm-msg-bot">${escapeHtml(safeStr(b.text))}${b.streaming ? ' ▍' : ''}</div>`;
    }
    if (type === 'thinking') {
      const t = safeStr(b.text);
      const truncated = t.length > 200 ? '…' + t.slice(-200) : t;
      return `<div class="cm-thinking" title="${escapeAttr(t)}">${escapeHtml(truncated)}</div>`;
    }
    if (type === 'tool') {
      const txt = safeStr(b.text);
      return `<div class="cm-msg-tool">${escapeHtml(txt)}${b.status === 'running' ? ' ⏳' : ' ✅'}</div>`;
    }
    if (type === 'image') {
      const prompt = safeStr(b.prompt);
      const path = safeStr(b.path);
      if (!path) {
        return `<div class="cm-msg-img-failed">🖼 等待图片… ${escapeHtml(prompt)}</div>`;
      }
      const safePath = encodeURIComponent(path);
      return `<div class="cm-msg-img"><a href="/api/codex-agent/file/view?path=${safePath}" target="_blank"><img src="/api/codex-agent/file/view?path=${safePath}" alt="${escapeAttr(prompt)}"></a></div>`;
    }
    if (type === 'todo') {
      const items = (b.items || []).map(i =>
        `<li>${escapeHtml(safeStr(i.status))} ${escapeHtml(safeStr(i.step || i.text))}</li>`
      ).join('');
      return `<div class="cm-msg-tool">📋 <ul>${items}</ul></div>`;
    }
    if (type === 'error') {
      return `<div class="cm-msg-error">❌ ${escapeHtml(safeStr(b.text))}</div>`;
    }
    return '';
  }

  function shortPath(p) {
    if (!p) return '';
    if (p.length <= 32) return p;
    return '…' + p.slice(-30);
  }

  function onDocClick(e) {
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

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', inject);
  } else {
    inject();
  }
})();
