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
    attachments: [],      // [{url, name, kind, nodeId, imageIndex, canvasKind}] 待发给 Codex 的素材附件
    projPopOpen: false,
    histPopOpen: false,
    mentionOpen: false,
    mentionItems: [],
    mentionQuery: '',
    mentionStart: -1,
    busyStartedAt: 0,
    busyLabel: '',
  };

  let currentAgentMsgId = null;
  let currentAgentText = '';
  let currentReasoningId = null;
  let currentReasoningText = '';
  let currentToolId = null;
  let pendingCanvasActionPromises = [];
  let currentTurnAbortController = null;
  let currentStreamReader = null;
  let turnStopRequested = false;
  let busyTimer = null;
  const addedImagePaths = new Set();
  const executedCanvasActionKeys = new Set();
  const panelSizeKey = 'codex-agent-panel-size-v1';

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

  function mediaKindFromUrl(url, fallback = 'image') {
    const clean = safeStr(url).split('?', 1)[0].split('#', 1)[0].toLowerCase();
    if (/\.(mp4|mov|webm|m4v|avi|mkv)$/.test(clean)) return 'video';
    if (/\.(mp3|wav|m4a|aac|ogg|flac)$/.test(clean)) return 'audio';
    return fallback || 'image';
  }

  function canvasKind() {
    if (document.querySelector('.image-node')) return 'smart';
    if (document.querySelector('.node')) return 'classic';
    return 'unknown';
  }

  function nodeTitleFromEl(nodeEl) {
    if (!nodeEl) return '';
    const title =
      nodeEl.querySelector('.node-title,.smart-node-title,.image-name-badge,.node-name,.current-canvas-title')?.textContent ||
      nodeEl.getAttribute('title') ||
      '';
    return safeStr(title).trim().slice(0, 80);
  }

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
      <div class="cm-mention-pop" id="cm-mention-pop"></div>
      <div class="cm-resize-handle" id="cm-resize" title="从左下角拖拽调整窗口大小"></div>
      <div class="cm-body" id="cm-body">
        <div class="cm-empty">选择一个项目文件夹，<br>然后开始和 Codex 对话。</div>
      </div>
      <div class="cm-foot">
        <div class="cm-attach">
          <button class="cm-attach-btn" id="cm-attach-canvas" title="添加画布已选素材">🎯 <span id="cm-attach-count">0</span></button>
          <span class="cm-attach-list" id="cm-attach-list"></span>
        </div>
        <div class="cm-input cm-input-disabled" id="cm-input" contenteditable="false" data-placeholder="输入消息，回车发送（Shift+Enter 换行）"></div>
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
    $('#cm-input').addEventListener('input', onInputMention);
    $('#cm-input').addEventListener('paste', onInputPaste);
    $('#cm-attach-canvas').addEventListener('click', onAttachFromCanvas);
    initPanelResize();
    document.addEventListener('click', onDocClick);
  }

  function togglePanel() {
    state.open = !state.open;
    $('#cm-panel').classList.toggle('cm-open', state.open);
    if (state.open) loadProjects();
  }

  function initPanelResize() {
    const panel = $('#cm-panel');
    const handle = $('#cm-resize');
    try {
      const saved = JSON.parse(localStorage.getItem(panelSizeKey) || '{}');
      if (saved.width) panel.style.width = `${saved.width}px`;
      if (saved.height) panel.style.height = `${saved.height}px`;
    } catch {}
    if (!handle) return;
    handle.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      const rect = panel.getBoundingClientRect();
      const startX = e.clientX;
      const startY = e.clientY;
      const startWidth = rect.width;
      const startHeight = rect.height;
      const minWidth = 320;
      const minHeight = 420;
      const maxWidth = Math.max(minWidth, window.innerWidth - 32);
      const maxHeight = Math.max(minHeight, window.innerHeight - rect.top - 16);
      handle.setPointerCapture?.(e.pointerId);
      document.body.classList.add('cm-resizing');

      const onMove = (ev) => {
        const width = Math.max(minWidth, Math.min(maxWidth, startWidth + (startX - ev.clientX)));
        const height = Math.max(minHeight, Math.min(maxHeight, startHeight + (ev.clientY - startY)));
        panel.style.width = `${Math.round(width)}px`;
        panel.style.height = `${Math.round(height)}px`;
      };
      const onUp = () => {
        document.removeEventListener('pointermove', onMove);
        document.removeEventListener('pointerup', onUp);
        document.body.classList.remove('cm-resizing');
        const next = panel.getBoundingClientRect();
        try {
          localStorage.setItem(panelSizeKey, JSON.stringify({
            width: Math.round(next.width),
            height: Math.round(next.height),
          }));
        } catch {}
      };
      document.addEventListener('pointermove', onMove);
      document.addEventListener('pointerup', onUp, { once: true });
    });
  }

  function formatElapsed(ms) {
    const total = Math.max(0, Math.floor(ms / 1000));
    const mm = String(Math.floor(total / 60)).padStart(2, '0');
    const ss = String(total % 60).padStart(2, '0');
    return `${mm}:${ss}`;
  }

  function setBusy(label = '正在思考') {
    if (!state.busyStartedAt) state.busyStartedAt = Date.now();
    state.busyLabel = label;
    state.status = 'busy';
    if (!busyTimer) {
      busyTimer = setInterval(setStatusUI, 1000);
    }
    setStatusUI();
  }

  function updateBusy(label) {
    if (state.status === 'busy') {
      state.busyLabel = label || state.busyLabel || '正在思考';
      setStatusUI();
    }
  }

  function finishBusy(status = 'ready') {
    state.status = status;
    state.busyStartedAt = 0;
    state.busyLabel = '';
    currentTurnAbortController = null;
    currentStreamReader = null;
    turnStopRequested = false;
    if (busyTimer) {
      clearInterval(busyTimer);
      busyTimer = null;
    }
    setStatusUI();
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
    setBusy('正在打开项目');
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
      updateBusy('正在加载历史');
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
      finishBusy('ready');
    } catch (e) {
      console.error('selectProject failed', e);
      alert('启动 Codex 失败：' + e.message);
      finishBusy('error');
    }
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
    setBusy('正在新建会话');
    try {
      const r = await fetch('/api/codex-agent/board/open', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_dir: state.projectDir }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || 'board/open failed');
      state.threadId = d.thread_id;
      state.messages = [];
      renderBody();
      finishBusy('ready');
    } catch (e) {
      alert('新对话失败：' + e.message);
      finishBusy('error');
    }
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
        💬 ${escapeHtml(sessionPreviewTitle(s))}
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

  function sessionPreviewTitle(session) {
    const text = safeStr(session?.preview).trim();
    if (text && text !== '(空)') return text;
    const media = safeStr(session?.preview_media || session?.media_preview || session?.first_media || '').trim();
    if (media) return media;
    return '含图片/附件的对话';
  }

  async function switchSession(sessionId) {
    if (!sessionId) return;
    setBusy('正在恢复会话');
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
      // 2. replay 历史
      updateBusy('正在加载历史');
      await replaySession(sessionId);
      finishBusy('ready');
    } catch (e) {
      console.error('switchSession failed', e);
      alert('恢复会话失败：' + e.message);
      finishBusy('error');
    }
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
  function selectedCanvasAssets() {
    const nativeApi = window.SmartCanvasAgentApi;
    if (nativeApi && typeof nativeApi.getSelectedAssets === 'function') {
      const items = nativeApi.getSelectedAssets() || [];
      if (items.length) return items;
    }
    const selectors = [
      '.node.selected',
      '.image-node.selected',
      '.thumb-item.image-selected',
      '.image-wrap.image-selected',
    ];
    const roots = Array.from(document.querySelectorAll(selectors.join(',')));
    const seen = new Set();
    const items = [];
    roots.forEach(root => {
      const nodeEl = root.closest('.image-node,.node') || root;
      const mediaEls = [];
      if (root.matches('img,video,audio')) mediaEls.push(root);
      mediaEls.push(...Array.from(root.querySelectorAll('img,video,audio')));
      mediaEls.forEach(media => {
        const src = safeStr(
          media.dataset.originalSrc ||
          media.dataset.sourceUrl ||
          media.dataset.url ||
          media.currentSrc ||
          media.src
        );
        if (!src || src.startsWith('data:') || seen.has(src)) return;
        seen.add(src);
        const itemRoot = media.closest('[data-image-index]') || root;
        const indexRaw = itemRoot?.dataset?.imageIndex;
        const kind = media.tagName === 'VIDEO' ? 'video' : media.tagName === 'AUDIO' ? 'audio' : mediaKindFromUrl(src, 'image');
        items.push({
          refId: `ref_${items.length + 1}`,
          url: src,
          name: media.dataset.name || itemRoot?.dataset?.name || extractName(src),
          kind,
          nodeId: nodeEl?.dataset?.id || itemRoot?.dataset?.refNodeId || '',
          imageIndex: indexRaw === undefined || indexRaw === '' ? '' : Number(indexRaw),
          nodeTitle: nodeTitleFromEl(nodeEl),
          canvasKind: canvasKind(),
        });
      });
    });
    return items;
  }

  function allCanvasAssets() {
    const nativeApi = window.SmartCanvasAgentApi;
    if (nativeApi && typeof nativeApi.getAllAssets === 'function') {
      const items = nativeApi.getAllAssets() || [];
      if (items.length) return items;
    }
    const seen = new Set();
    const items = [];
    const mediaEls = Array.from(document.querySelectorAll('.image-node img,.image-node video,.image-node audio,.node img,.node video,.node audio,.thumb-item img,.image-wrap img,.thumb-item video,.image-wrap video'));
    mediaEls.forEach(media => {
      const src = safeStr(
        media.dataset.originalSrc ||
        media.dataset.sourceUrl ||
        media.dataset.url ||
        media.currentSrc ||
        media.src
      );
      if (!src || src.startsWith('data:') || seen.has(src)) return;
      seen.add(src);
      const root = media.closest('.thumb-item,.image-wrap,.image-node,.node') || media;
      const nodeEl = media.closest('.image-node,.node') || root;
      const indexRaw = root?.dataset?.imageIndex ?? root?.dataset?.refImageIndex;
      const kind = media.tagName === 'VIDEO' ? 'video' : media.tagName === 'AUDIO' ? 'audio' : mediaKindFromUrl(src, 'image');
      items.push({
        refId: `ref_${items.length + 1}`,
        url: src,
        name: media.dataset.name || root?.dataset?.name || extractName(src),
        kind,
        nodeId: nodeEl?.dataset?.id || root?.dataset?.refNodeId || '',
        imageIndex: indexRaw === undefined || indexRaw === '' ? '' : Number(indexRaw),
        nodeTitle: nodeTitleFromEl(nodeEl),
        canvasKind: canvasKind(),
      });
    });
    return items;
  }

  const CanvasAgentBridge = {
    getSelectedAssets: selectedCanvasAssets,
    getAllAssets: allCanvasAssets,
    getContext() {
      const nativeContext = window.SmartCanvasAgentApi?.getContext?.();
      const selectedAssets = selectedCanvasAssets();
      return {
        canvasKind: canvasKind(),
        native: nativeContext || null,
        selectedAssets,
        selectedCount: selectedAssets.length,
        url: location.href,
        title: document.title || '',
      };
    },
    addImageNodeFromPath(path, prompt = '') {
      return addGeneratedImageToCanvas(path, prompt);
    },
    addMediaNodes(items, options = {}) {
      return addMediaNodesToCanvas(items, options);
    },
    addPromptNodes(items, options = {}) {
      return addPromptNodesToCanvas(items, options);
    },
    addLoopNodes(items, options = {}) {
      return addLoopNodesToCanvas(items, options);
    },
    groupNodes(items, options = {}) {
      return groupNodesOnCanvas(items, options);
    },
    ungroupNodes(items, options = {}) {
      return ungroupNodesOnCanvas(items, options);
    },
    renameNodes(items, options = {}) {
      return renameNodesOnCanvas(items, options);
    },
    moveNodes(items, options = {}) {
      return moveNodesOnCanvas(items, options);
    },
    arrangeNodes(items, options = {}) {
      return arrangeNodesOnCanvas(items, options);
    },
    generateImageNodes(items, options = {}) {
      return generateImageNodesToCanvas(items, options);
    },
    generateVideoNodes(items, options = {}) {
      return generateVideoNodesToCanvas(items, options);
    },
  };
  window.CanvasAgentBridge = CanvasAgentBridge;

  function onAttachFromCanvas() {
    const assets = CanvasAgentBridge.getSelectedAssets();
    if (assets.length === 0) {
      showHint('画布里没选中节点（先在画布里点选/框选）');
      return;
    }
    let count = 0;
    assets.forEach(item => {
      if (addAttachment(item)) count++;
    });
    if (count === 0) {
      showHint(`已选 ${assets.length} 个素材，但都已经在附件里`);
    } else {
      showHint(`已加 ${count} 个画布素材`);
    }
  }

  function addAttachment(item) {
    try {
      const abs = new URL(item.url, window.location.origin).href;
      const existed = state.attachments.find(a => a.url === abs);
      if (existed) return false;
      state.attachments.push({ ...item, url: abs, refId: `ref_${state.attachments.length + 1}` });
      renderAttach();
      return true;
    } catch {
      return false;
    }
  }

  function addUploadedAssetToCanvas(item) {
    try {
      if (!item || !item.url) return false;
      if (window.SmartCanvasAgentApi?.addMediaNodes) {
        const created = window.SmartCanvasAgentApi.addMediaNodes([item], { cols: 1 });
        return Array.isArray(created) ? created.length > 0 : Boolean(created);
      }
      if (typeof window.appendImagesToSmartNode === 'function') {
        window.appendImagesToSmartNode([item], '', { forceNew: true });
        return true;
      }
      if (typeof window.addNode === 'function' && typeof window.uid === 'function') {
        const p = typeof window.defaultPoint === 'function' ? window.defaultPoint(160, 40) : { x: 0, y: 0 };
        window.addNode({
          id: window.uid(item.kind === 'video' ? 'vid' : 'img'),
          type: item.kind === 'video' ? 'video' : 'image',
          x: p.x,
          y: p.y,
          url: item.url,
          name: item.name || extractName(item.url),
          mediaKind: item.kind || 'image',
        });
        return true;
      }
      if (typeof window.addImageNode === 'function' && item.kind !== 'video') {
        const node = window.addImageNode();
        if (node) {
          node.url = item.url;
          node.name = item.name || extractName(item.url);
          node.mediaKind = 'image';
          if (typeof window.render === 'function') window.render();
          if (typeof window.scheduleSave === 'function') window.scheduleSave();
          return true;
        }
      }
    } catch (e) {
      console.warn('addUploadedAssetToCanvas failed', e);
    }
    return false;
  }

  function canvasUrlForPath(pathOrUrl) {
    const raw = safeStr(pathOrUrl).trim();
    if (!raw) return '';
    if (/^(https?:|data:|\/)/i.test(raw)) return raw;
    return '/api/codex-agent/file/view?path=' + encodeURIComponent(raw);
  }

  function normalizeCanvasMediaItem(item) {
    if (!item) return null;
    const data = typeof item === 'string' ? { url: item } : { ...item };
    const raw = safeStr(data.url || data.path || data.src).trim();
    if (!raw) return null;
    const url = canvasUrlForPath(raw);
    return {
      ...data,
      url,
      name: data.name || extractName(raw) || extractName(url) || 'asset',
      kind: data.kind || mediaKindFromUrl(raw, 'image'),
    };
  }

  function addMediaNodesToCanvas(items, options = {}) {
    const list = (Array.isArray(items) ? items : [items]).map(normalizeCanvasMediaItem).filter(Boolean);
    if (!list.length) return [];
    if (window.SmartCanvasAgentApi?.addMediaNodes) {
      return window.SmartCanvasAgentApi.addMediaNodes(list, options) || [];
    }
    const created = [];
    list.forEach(item => {
      if (addUploadedAssetToCanvas(item)) created.push(item);
    });
    return created;
  }

  function addPromptNodesToCanvas(items, options = {}) {
    const list = (Array.isArray(items) ? items : [items])
      .map(normalizeCanvasNodeItem)
      .filter(item => item && (item.text || item.title));
    if (!list.length) return [];
    if (window.SmartCanvasAgentApi?.addPromptNodes) {
      return window.SmartCanvasAgentApi.addPromptNodes(list, options) || [];
    }
    return [];
  }

  function addLoopNodesToCanvas(items, options = {}) {
    const list = (Array.isArray(items) ? items : [items])
      .map(normalizeCanvasNodeItem)
      .filter(Boolean);
    if (!list.length) return [];
    if (window.SmartCanvasAgentApi?.addLoopNodes) {
      return window.SmartCanvasAgentApi.addLoopNodes(list, options) || [];
    }
    return [];
  }

  function attachmentFromReferenceValue(value) {
    const text = safeStr(value).trim();
    if (!text) return null;
    const m = text.match(/^@?ref[_-]?(\d+)$/i) || text.match(/^@?图\s*(\d+)$/);
    if (!m) return null;
    const idx = Math.max(0, Number(m[1] || m[2]) - 1);
    return currentTurnAttachments[idx] || state.attachments[idx] || null;
  }

  function nodeIdFromReferenceValue(value) {
    const item = attachmentFromReferenceValue(value);
    return safeStr(item?.nodeId || item?.node_id);
  }

  function normalizeCanvasNodeTargetItem(item) {
    if (!item) return null;
    const data = typeof item === 'string' ? { ref: item } : { ...item };
    const refValue = data.ref || data.ref_id || data.refId || data.target || data.target_ref || data.targetRef;
    const refItem = attachmentFromReferenceValue(refValue);
    const refNodeId = safeStr(refItem?.nodeId || refItem?.node_id) || nodeIdFromReferenceValue(refValue);
    if (refNodeId && !data.node_id && !data.nodeId && !data.id) data.node_id = refNodeId;
    if (refItem && data.image_index == null && data.imageIndex == null) data.image_index = refItem.imageIndex ?? refItem.image_index ?? 0;
    if (Array.isArray(data.refs) && !Array.isArray(data.node_ids)) {
      const nodeIds = data.refs.map(nodeIdFromReferenceValue).filter(Boolean);
      if (nodeIds.length) data.node_ids = nodeIds;
    }
    return data;
  }

  function renameNodesOnCanvas(items, options = {}) {
    const list = (Array.isArray(items) ? items : [items]).map(normalizeCanvasNodeTargetItem).filter(Boolean);
    if (!list.length) return [];
    if (window.SmartCanvasAgentApi?.renameNodes) {
      return window.SmartCanvasAgentApi.renameNodes(list, options) || [];
    }
    throw new Error('当前画布还不支持 Agent 改名节点');
  }

  function moveNodesOnCanvas(items, options = {}) {
    const list = (Array.isArray(items) ? items : [items]).map(normalizeCanvasNodeTargetItem).filter(Boolean);
    if (!list.length) return [];
    if (window.SmartCanvasAgentApi?.moveNodes) {
      return window.SmartCanvasAgentApi.moveNodes(list, options) || [];
    }
    throw new Error('当前画布还不支持 Agent 移动节点');
  }

  function arrangeNodesOnCanvas(items, options = {}) {
    const list = (Array.isArray(items) ? items : [items]).map(normalizeCanvasNodeTargetItem).filter(Boolean);
    if (window.SmartCanvasAgentApi?.arrangeNodes) {
      return window.SmartCanvasAgentApi.arrangeNodes(list, options) || [];
    }
    throw new Error('当前画布还不支持 Agent 整理节点');
  }

  function groupNodesOnCanvas(items, options = {}) {
    const list = (Array.isArray(items) ? items : [items]).map(normalizeCanvasNodeTargetItem).filter(Boolean);
    if (window.SmartCanvasAgentApi?.groupNodes) {
      return window.SmartCanvasAgentApi.groupNodes(list, options) || [];
    }
    throw new Error('当前画布还不支持 Agent 分组节点');
  }

  function ungroupNodesOnCanvas(items, options = {}) {
    const list = (Array.isArray(items) ? items : [items]).map(normalizeCanvasNodeTargetItem).filter(Boolean);
    if (window.SmartCanvasAgentApi?.ungroupNodes) {
      return window.SmartCanvasAgentApi.ungroupNodes(list, options) || [];
    }
    throw new Error('当前画布还不支持 Agent 取消分组');
  }

  async function generateImageNodesToCanvas(items, options = {}) {
    const list = (Array.isArray(items) ? items : [items])
      .map(item => typeof item === 'string' ? { prompt: item } : item)
      .map(normalizeCanvasGenerateItem)
      .filter(item => item && (item.prompt || item.text));
    if (!list.length) return [];
    if (window.SmartCanvasAgentApi?.generateImageNodes) {
      return await window.SmartCanvasAgentApi.generateImageNodes(list, normalizeCanvasActionOptions(options)) || [];
    }
    throw new Error('当前画布还不支持 Agent 生图');
  }

  async function generateVideoNodesToCanvas(items, options = {}) {
    const list = (Array.isArray(items) ? items : [items])
      .map(item => typeof item === 'string' ? { prompt: item } : item)
      .map(normalizeCanvasGenerateItem)
      .filter(item => item && (item.prompt || item.text));
    if (!list.length) return [];
    if (window.SmartCanvasAgentApi?.generateVideoNodes) {
      return await window.SmartCanvasAgentApi.generateVideoNodes(list, normalizeCanvasActionOptions(options)) || [];
    }
    throw new Error('当前画布还不支持 Agent 生视频');
  }

  function attachmentIndexFromRef(value) {
    const text = safeStr(value).trim();
    if (!text) return -1;
    let m = text.match(/^@?ref[_-]?(\d+)$/i) || text.match(/^@?图\s*(\d+)$/);
    if (m) return Math.max(0, Number(m[1]) - 1);
    const zh = { 一:1, 二:2, 两:2, 三:3, 四:4, 五:5, 六:6, 七:7, 八:8, 九:9, 十:10 };
    m = text.match(/^@?(?:第)?([一二两三四五六七八九十])张?图?$/);
    if (m && zh[m[1]]) return zh[m[1]] - 1;
    m = text.match(/^@?图([一二两三四五六七八九十])$/);
    if (m && zh[m[1]]) return zh[m[1]] - 1;
    return -1;
  }

  function attachmentForRef(value) {
    const idx = attachmentIndexFromRef(value);
    if (idx < 0) return null;
    return currentTurnAttachments[idx] || state.attachments[idx] || null;
  }

  function normalizeCanvasReference(ref) {
    if (!ref) return null;
    if (typeof ref === 'string') {
      const attachment = attachmentForRef(ref);
      if (attachment) return { ...attachment };
      return { url: ref, name: extractName(ref) || ref, kind: mediaKindFromUrl(ref, 'image') };
    }
    const data = { ...ref };
    const raw = safeStr(data.url || data.path || data.src || data.ref || data.refId || data.ref_id).trim();
    const attachment = attachmentForRef(raw || data.refId || data.ref_id);
    if (attachment) {
      const rawIsAttachmentRef = attachmentIndexFromRef(raw) >= 0;
      return {
        ...attachment,
        ...data,
        url: rawIsAttachmentRef ? attachment.url : (data.url || data.path || data.src || attachment.url),
        name: data.name || attachment.name,
        kind: data.kind || attachment.kind || 'image',
      };
    }
    if (!raw) return null;
    return {
      ...data,
      url: data.url || data.path || data.src || raw,
      name: data.name || extractName(raw) || 'reference',
      kind: data.kind || mediaKindFromUrl(raw, 'image'),
    };
  }

  function normalizeCanvasReferences(refs) {
    return (Array.isArray(refs) ? refs : [refs]).map(normalizeCanvasReference).filter(ref => ref?.url);
  }

  function referenceTokensFromText(text) {
    const refs = [];
    const seen = new Set();
    safeStr(text).replace(/@?ref[_-]?(\d+)|@?图\s*(\d+)/gi, (_all, a, b) => {
      const idx = Math.max(0, Number(a || b) - 1);
      if (!Number.isFinite(idx) || seen.has(idx)) return '';
      const item = currentTurnAttachments[idx] || state.attachments[idx];
      if (item) {
        seen.add(idx);
        refs.push({ ...item });
      }
      return '';
    });
    return refs;
  }

  function normalizeCanvasActionOptions(options = {}) {
    const data = options && typeof options === 'object' ? { ...options } : {};
    const refs = normalizeCanvasReferences(data.reference_images || data.references || data.refs || []);
    if (refs.length) {
      data.reference_images = refs;
      delete data.references;
      delete data.refs;
    }
    return data;
  }

  function normalizeCanvasGenerateItem(item) {
    if (!item) return null;
    const data = { ...item };
    const explicitRefs = normalizeCanvasReferences(data.reference_images || data.references || data.refs || data.ref || data.refId || data.ref_id || []);
    const textRefs = explicitRefs.length ? [] : referenceTokensFromText(data.prompt || data.text || '');
    const refs = explicitRefs.length ? explicitRefs : textRefs;
    if (refs.length) {
      data.reference_images = refs;
      delete data.references;
      delete data.refs;
      delete data.ref;
      delete data.refId;
      delete data.ref_id;
    }
    return data;
  }

  function normalizeCanvasNodeItem(item) {
    if (!item) return null;
    const data = typeof item === 'string' ? { text: item } : { ...item };
    const explicitRefs = normalizeCanvasReferences(data.reference_images || data.references || data.refs || data.ref || data.refId || data.ref_id || []);
    const textRefs = explicitRefs.length ? [] : referenceTokensFromText(data.prompt || data.text || data.variablePrompt || '');
    const refs = explicitRefs.length ? explicitRefs : textRefs;
    if (refs.length) {
      data.reference_images = refs;
      delete data.references;
      delete data.refs;
      delete data.ref;
      delete data.refId;
      delete data.ref_id;
    }
    return data;
  }

  function normalizeAttachmentRefs() {
    state.attachments.forEach((item, index) => {
      item.refId = `ref_${index + 1}`;
    });
  }

  function removeAttach(idx) {
    state.attachments.splice(idx, 1);
    normalizeAttachmentRefs();
    renderAttach();
  }

  function moveAttach(fromIdx, toIdx) {
    if (fromIdx === toIdx || fromIdx < 0 || toIdx < 0 || fromIdx >= state.attachments.length || toIdx >= state.attachments.length) return;
    const [item] = state.attachments.splice(fromIdx, 1);
    state.attachments.splice(toIdx, 0, item);
    normalizeAttachmentRefs();
    renderAttach();
  }

  function renderAttach() {
    const list = $('#cm-attach-list');
    const btn = $('#cm-attach-canvas');
    const count = $('#cm-attach-count');
    if (count) count.textContent = String(state.attachments.length);
    if (!list) return;
    list.innerHTML = state.attachments.map((a, i) => {
      const kind = safeStr(a.kind || mediaKindFromUrl(a.url));
      const preview = attachmentPreviewHtml(a, kind, 'cm-attach-kind');
      return `<div class="cm-attach-thumb" data-i="${i}" draggable="true" title="图${i + 1} · ${escapeAttr(a.name)} · ${escapeAttr(a.url)}">
        ${preview}
        <span class="cm-attach-index">${i + 1}</span>
        <span class="cm-attach-x" data-i="${i}" title="移除">×</span>
      </div>`;
    }).join('');
    list.querySelectorAll('.cm-attach-thumb').forEach(thumb => {
      thumb.addEventListener('dragstart', e => {
        const i = thumb.getAttribute('data-i');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', i);
        thumb.classList.add('cm-attach-dragging');
      });
      thumb.addEventListener('dragend', () => {
        thumb.classList.remove('cm-attach-dragging');
      });
      thumb.addEventListener('dragover', e => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        thumb.classList.add('cm-attach-drop');
      });
      thumb.addEventListener('dragleave', () => {
        thumb.classList.remove('cm-attach-drop');
      });
      thumb.addEventListener('drop', e => {
        e.preventDefault();
        thumb.classList.remove('cm-attach-drop');
        const from = Number(e.dataTransfer.getData('text/plain'));
        const to = Number(thumb.getAttribute('data-i'));
        moveAttach(from, to);
      });
    });
    list.querySelectorAll('.cm-attach-x').forEach(x => {
      x.addEventListener('click', e => {
        e.stopPropagation();
        e.preventDefault();
        removeAttach(parseInt(x.getAttribute('data-i'), 10));
      });
    });
  }

  function attachmentPreviewHtml(item, kind = '', fallbackClass = 'cm-attach-kind') {
    const mediaKind = safeStr(kind || item.kind || mediaKindFromUrl(item.url));
    const url = escapeAttr(item.url);
    const name = escapeAttr(item.name);
    if (mediaKind === 'image') {
      return `<img src="${url}" alt="${name}" loading="lazy">`;
    }
    if (mediaKind === 'video') {
      return `<video src="${url}" muted playsinline preload="metadata"></video><span class="cm-video-badge">▶</span>`;
    }
    return `<span class="${fallbackClass}">${escapeHtml(mediaKind.toUpperCase().slice(0, 5))}</span>`;
  }

  function inputText() {
    const input = $('#cm-input');
    return safeStr(input?.innerText || '').replace(/\u00a0/g, ' ');
  }

  function clearInput() {
    const input = $('#cm-input');
    if (input) input.innerHTML = '';
  }

  function inputCaretPrefix() {
    const input = $('#cm-input');
    const sel = window.getSelection();
    if (!input || !sel || sel.rangeCount === 0) return inputText();
    const range = sel.getRangeAt(0);
    if (!input.contains(range.startContainer)) return inputText();
    const prefix = range.cloneRange();
    prefix.selectNodeContents(input);
    prefix.setEnd(range.startContainer, range.startOffset);
    return safeStr(prefix.toString()).replace(/\u00a0/g, ' ');
  }

  function deleteCharsBeforeCaret(count) {
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount || count <= 0) return false;
    try {
      sel.modify('extend', 'backward', 'character');
      for (let i = 1; i < count; i++) sel.modify('extend', 'backward', 'character');
      sel.getRangeAt(0).deleteContents();
      sel.collapseToEnd();
      return true;
    } catch {
      return false;
    }
  }

  function insertInputToken(text) {
    const safe = escapeHtml(text);
    const html = `<span class="cm-input-token" contenteditable="false" data-token-text="${escapeAttr(text)}">${safe}</span>&nbsp;`;
    document.execCommand('insertHTML', false, html);
  }

  function restoreInputRange(range) {
    const input = $('#cm-input');
    if (!range || !input) return false;
    try {
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      return true;
    } catch {
      return false;
    }
  }

  function insertPlainText(text) {
    document.execCommand('insertText', false, safeStr(text));
  }

  // ---------------- 发消息 → SSE ----------------
  function onInputKey(e) {
    if (state.mentionOpen) {
      if (e.key === 'Escape') { e.preventDefault(); closeMentionPicker(); return; }
      if (e.key === 'Enter' && !e.shiftKey) {
        const first = state.mentionItems[0];
        if (first) { e.preventDefault(); pickMentionItem(first); return; }
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); onSend(); }
  }

  async function onInputPaste(e) {
    const clipboard = e.clipboardData;
    if (!clipboard) return;
    // Agent 输入框自己接管粘贴，避免事件继续冒泡到画布的全局 paste 逻辑，
    // 否则图片会被 Agent 和画布各上传/放置一次。
    e.stopPropagation();
    const files = uniqueClipboardFiles(Array.from(clipboard.files || []).filter(file => file && file.type && file.type.startsWith('image/')));
    const itemFiles = Array.from(clipboard.items || [])
      .filter(item => item.kind === 'file' && String(item.type || '').startsWith('image/'))
      .map(item => item.getAsFile?.())
      .filter(Boolean);
    const imageFiles = files.length ? files : uniqueClipboardFiles(itemFiles);

    if (imageFiles.length) {
      e.preventDefault();
      const sel = window.getSelection();
      const input = $('#cm-input');
      const range = sel && sel.rangeCount && input?.contains(sel.getRangeAt(0).startContainer)
        ? sel.getRangeAt(0).cloneRange()
        : null;
      await uploadPastedImages(imageFiles, range);
      return;
    }

    const text = clipboard.getData('text/plain');
    if (text) {
      e.preventDefault();
      insertPlainText(text);
      onInputMention();
    }
  }

  function uniqueClipboardFiles(files) {
    const seen = new Set();
    const out = [];
    files.forEach(file => {
      const key = [
        safeStr(file.name),
        safeStr(file.type),
        safeStr(file.size),
        safeStr(file.lastModified),
      ].join('|');
      if (seen.has(key)) return;
      seen.add(key);
      out.push(file);
    });
    return out;
  }

  async function uploadPastedImages(files, pasteRange = null) {
    if (!files.length) return;
    setBusy('正在粘贴图片');
    try {
      const form = new FormData();
      files.forEach((file, index) => {
        const ext = file.type === 'image/jpeg' ? 'jpg' : file.type === 'image/webp' ? 'webp' : file.type === 'image/gif' ? 'gif' : 'png';
        const name = file.name || `pasted-${Date.now()}-${index + 1}.${ext}`;
        form.append('files', file, name);
      });
      const r = await fetch('/api/ai/upload', { method: 'POST', body: form });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(safeStr(data.detail) || `HTTP ${r.status}`);
      const uploaded = Array.isArray(data.files) ? data.files : [];
      const assets = uploaded.map(item => ({
          url: item.url,
          name: item.name || extractName(item.url),
          kind: item.kind || 'image',
          canvasKind: canvasKind(),
      }));
      if (assets.length === 1) addUploadedAssetToCanvas(assets[0]);
      else if (assets.length > 1) addMediaNodesToCanvas(assets);
      assets.forEach(asset => {
        addAttachment(asset);
        const index = state.attachments.findIndex(a => a.url === new URL(asset.url, window.location.origin).href);
        $('#cm-input')?.focus();
        restoreInputRange(pasteRange);
        insertInputToken(`@图${index >= 0 ? index + 1 : state.attachments.length}`);
        const sel = window.getSelection();
        if (sel && sel.rangeCount) pasteRange = sel.getRangeAt(0).cloneRange();
      });
      showHint(uploaded.length ? `已粘贴 ${uploaded.length} 张图片` : '剪贴板图片为空');
    } catch (err) {
      console.error('uploadPastedImages failed', err);
      showHint('粘贴图片失败：' + safeStr(err.message || err), 6000);
    } finally {
      finishBusy('ready');
      $('#cm-input')?.focus();
    }
  }

  function onInputMention() {
    const before = inputCaretPrefix();
    const match = before.match(/@([^\s@]*)$/);
    if (!match) {
      closeMentionPicker();
      return;
    }
    state.mentionStart = before.length - match[1].length - 1;
    state.mentionQuery = match[1].toLowerCase();
    const candidates = state.attachments;
    state.mentionItems = candidates
      .filter(item => {
        const hay = `${item.name || ''} ${item.nodeTitle || ''} ${item.kind || ''}`.toLowerCase();
        return !state.mentionQuery || hay.includes(state.mentionQuery);
      })
      .slice(0, 24);
    renderMentionPicker();
  }

  function renderMentionPicker() {
    const pop = $('#cm-mention-pop');
    if (!pop) return;
    state.mentionOpen = true;
    pop.classList.add('cm-mention-open');
    if (!state.mentionItems.length) {
      pop.innerHTML = '<div class="cm-mention-empty">输入框上方还没有附件</div>';
      return;
    }
    pop.innerHTML = state.mentionItems.map((item, i) => {
      const kind = safeStr(item.kind || mediaKindFromUrl(item.url));
      const attachIndex = state.attachments.findIndex(a => a.url === item.url);
      const label = `图${attachIndex >= 0 ? attachIndex + 1 : i + 1}`;
      const media = attachmentPreviewHtml(item, kind, 'cm-mention-kind');
      return `<button class="cm-mention-item" type="button" data-i="${i}">
        <span class="cm-mention-thumb">${media}</span>
        <span class="cm-mention-label">${escapeHtml(label)}</span>
      </button>`;
    }).join('');
    pop.querySelectorAll('.cm-mention-item').forEach(btn => {
      btn.addEventListener('mousedown', e => e.preventDefault());
      btn.addEventListener('click', () => {
        const item = state.mentionItems[Number(btn.getAttribute('data-i'))];
        if (item) pickMentionItem(item);
      });
    });
  }

  function pickMentionItem(item) {
    const input = $('#cm-input');
    const index = state.attachments.findIndex(a => a.url === item.url);
    const token = `@图${index >= 0 ? index + 1 : 1} `;
    input.focus();
    deleteCharsBeforeCaret(state.mentionQuery.length + 1);
    insertInputToken(token.trim());
    addAttachment(item);
    closeMentionPicker();
    input.focus();
  }

  function closeMentionPicker() {
    state.mentionOpen = false;
    state.mentionItems = [];
    state.mentionQuery = '';
    state.mentionStart = -1;
    const pop = $('#cm-mention-pop');
    if (pop) {
      pop.classList.remove('cm-mention-open');
      pop.innerHTML = '';
    }
  }

  // 临时提示（不弹窗，用底部 hint 区）
  let hintTimer = null;
  let currentTurnAttachments = [];
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
    if (state.status === 'busy') {
      stopCurrentTurn();
      return;
    }
    const input = $('#cm-input');
    const text = inputText().trim();
    if (!text || !state.projectDir) return;

    // 收集 attachments（深拷贝后清空）
    const attachments = state.attachments.map((a, index) => ({
      ...a,
      refId: a.refId || `ref_${index + 1}`,
    }));
    currentTurnAttachments = attachments.map(item => ({ ...item }));
    state.attachments = [];
    renderAttach();

    state.messages.push({ role: 'user', blocks: [{ type: 'text', text: safeStr(text) }] });
    if (attachments.length) {
      state.messages[state.messages.length - 1].blocks.push({ type: 'attach', items: attachments });
    }
    renderBody();
    clearInput();

    const botMsg = { role: 'bot', blocks: [] };
    state.messages.push(botMsg);
    renderBody();
    closeMentionPicker();

    setBusy(attachments.length ? '正在准备选中素材' : '正在发送');

    currentAgentMsgId = null;
    currentAgentText = '';
    currentReasoningId = null;
    currentReasoningText = '';
    currentToolId = null;
    pendingCanvasActionPromises = [];
    turnStopRequested = false;
    currentTurnAbortController = new AbortController();

    try {
      const r = await fetch('/api/codex-agent/turn', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_dir: state.projectDir, text, attachments, canvas_context: CanvasAgentBridge.getContext() }),
        signal: currentTurnAbortController.signal,
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(safeStr(d.detail) || `HTTP ${r.status}`);
      }
      updateBusy('等待 Codex 响应');
      const reader = r.body.getReader();
      currentStreamReader = reader;
      const decoder = new TextDecoder();
      let buf = '';
      while (true) {
        if (turnStopRequested) break;
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
      if (!turnStopRequested && pendingCanvasActionPromises.length) {
        await Promise.allSettled(pendingCanvasActionPromises);
      }
      if (turnStopRequested) {
        botMsg.blocks.push({ type: 'tool', text: '已停止当前回复', status: 'done' });
        renderBody();
      }
    } catch (e) {
      botMsg.blocks.push(turnStopRequested || e?.name === 'AbortError'
        ? { type: 'tool', text: '已停止当前回复', status: 'done' }
        : { type: 'error', text: safeStr(e.message || e) });
      renderBody();
    }
    currentTurnAttachments = [];
    finishBusy('ready');
  }

  function stopCurrentTurn() {
    turnStopRequested = true;
    updateBusy('正在停止');
    try { currentTurnAbortController?.abort(); } catch {}
    try { currentStreamReader?.cancel?.(); } catch {}
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
        updateBusy('正在回复');
        currentAgentMsgId = item.id;
        currentAgentText = safeStr(item.text);
        botMsg.blocks.push({ type: 'text', text: currentAgentText, id: item.id, streaming: true });
      } else if (type === 'reasoning') {
        updateBusy('正在读取/思考');
        currentReasoningId = item.id;
        currentReasoningText = safeStr(item.summary) || safeStr(item.text);
        botMsg.blocks.push({ type: 'thinking', text: currentReasoningText, id: item.id });
      } else if (type === 'commandExecution') {
        updateBusy('正在执行命令');
        currentToolId = item.id;
        botMsg.blocks.push({
          type: 'tool', text: `🔧 ${safeStr(item.command) || '(命令)'}`,
          status: 'running', id: item.id,
        });
      } else if (type === 'imageGeneration') {
        updateBusy('正在生成图片');
        botMsg.blocks.push({
          type: 'image', path: safeStr(item.savedPath) || safeStr(item.path) || '',
          prompt: safeStr(item.prompt), status: 'generating', id: item.id,
        });
      } else if (type === 'todoList' || type === 'plan') {
        updateBusy('正在更新计划');
        const items = Array.isArray(item.items) ? item.items : [];
        botMsg.blocks.push({ type: 'todo', items });
      } else if (type === 'mcpToolCall' || type === 'webSearch' || type === 'fileChange') {
        updateBusy(type === 'fileChange' ? '正在改文件' : '正在调用工具');
        const label = safeStr(item.name) || safeStr(item.query) || safeStr(item.path) || '';
        botMsg.blocks.push({ type: 'tool', text: `🔧 ${type}: ${label}`, status: 'running', id: item.id });
      }
      renderBody();
    } else if (method === 'item/agentMessage/delta' || method === 'item/reasoning/summaryTextDelta' || method === 'item/reasoning/textDelta') {
      const delta = safeStr(params.delta);
      if (method === 'item/agentMessage/delta') {
        updateBusy('正在回复');
        currentAgentText += delta;
        const blk = botMsg.blocks.find(b => b.id === currentAgentMsgId && b.type === 'text');
        if (blk) blk.text = currentAgentText;
      } else {
        updateBusy('正在读取/思考');
        currentReasoningText += delta;
        const blk = botMsg.blocks.find(b => b.id === currentReasoningId && b.type === 'thinking');
        if (blk) blk.text = currentReasoningText;
      }
      renderBody();
    } else if (method === 'item/commandExecution/outputDelta') {
      updateBusy('正在执行命令');
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
          const actionPromise = executeCanvasActionsFromText(blk.text, botMsg);
          pendingCanvasActionPromises.push(actionPromise);
          blk.text = cleanAgentDisplayText(blk.text);
        } else if (blk.type === 'image') {
          blk.path = safeStr(item.savedPath) || safeStr(item.path) || blk.path;
          blk.status = 'done';
          addGeneratedImageToCanvas(blk.path, blk.prompt);
        } else if (blk.type === 'tool') {
          blk.status = 'done';
        }
      }
      if (safeStr(item.type) === 'imageGeneration') {
        const path = safeStr(item.savedPath) || safeStr(item.path);
        if (path) addGeneratedImageToCanvas(path, safeStr(item.prompt));
      }
      renderBody();
    } else if (method === 'turn/completed') {
      updateBusy('完成');
      botMsg.blocks.forEach(b => { if (b.streaming) b.streaming = false; });
      renderBody();
    } else if (method === 'error' || method === 'fatal' || method === 'turn/timeout') {
      updateBusy('请求异常');
      const errText = safeStr(params.message) || safeStr(params.error) || JSON.stringify(params || {});
      botMsg.blocks.push({ type: 'error', text: errText });
      renderBody();
    }
  }

  function addGeneratedImageToCanvas(path, prompt = '') {
    path = safeStr(path);
    if (!path || addedImagePaths.has(path)) return false;
    const url = '/api/codex-agent/file/view?path=' + encodeURIComponent(path);
    const name = extractName(path) || extractName(url) || 'codex-image.png';
    const item = { url, name, kind: 'image', prompt: safeStr(prompt) };

    try {
      if (typeof window.appendImagesToSmartNode === 'function') {
        window.appendImagesToSmartNode([item], '', { forceNew: true });
        addedImagePaths.add(path);
        showHint('已把生成图片放到智能画布');
        return true;
      }
      if (typeof window.addNode === 'function' && typeof window.uid === 'function') {
        const p = typeof window.defaultPoint === 'function' ? window.defaultPoint(160, 40) : { x: 0, y: 0 };
        window.addNode({ id: window.uid('img'), type: 'image', x: p.x, y: p.y, url, name, mediaKind: 'image' });
        addedImagePaths.add(path);
        showHint('已把生成图片放到画布');
        return true;
      }
      if (typeof window.addImageNode === 'function') {
        const node = window.addImageNode();
        if (node) {
          node.url = url;
          node.name = name;
          node.mediaKind = 'image';
          if (typeof window.render === 'function') window.render();
          if (typeof window.scheduleSave === 'function') window.scheduleSave();
          addedImagePaths.add(path);
          showHint('已把生成图片放到画布');
          return true;
        }
      }
    } catch (e) {
      console.warn('addGeneratedImageToCanvas failed', e);
      showHint('生成图已完成，但自动放入画布失败');
      return false;
    }
    return false;
  }

  function extractCanvasActionBlocks(text) {
    const raw = safeStr(text);
    const blocks = [];
    raw.replace(/```(?:canvas_agent_action|canvas-agent-action)\s*([\s\S]*?)```/gi, (_, body) => {
      blocks.push(body.trim());
      return '';
    });
    raw.replace(/<canvas_agent_action>([\s\S]*?)<\/canvas_agent_action>/gi, (_, body) => {
      blocks.push(body.trim());
      return '';
    });
    return blocks;
  }

  function stripCanvasActionBlocks(text) {
    return safeStr(text)
      .replace(/```(?:canvas_agent_action|canvas-agent-action)\s*[\s\S]*?```/gi, '')
      .replace(/<canvas_agent_action>[\s\S]*?<\/canvas_agent_action>/gi, '')
      .trim();
  }

  function stripInternalContextBlocks(text) {
    return safeStr(text)
      .replace(/<skill\b[\s\S]*?<\/skill>/gi, '')
      .replace(/<(?:environment_context|app-context|permissions instructions|collaboration_mode|skills_instructions|plugins_instructions)>[\s\S]*?<\/(?:environment_context|app-context|permissions instructions|collaboration_mode|skills_instructions|plugins_instructions)>/gi, '')
      .trim();
  }

  function cleanAgentDisplayText(text) {
    return stripInternalContextBlocks(stripCanvasActionBlocks(text));
  }

  async function executeCanvasActionsFromText(text, botMsg) {
    const blocks = extractCanvasActionBlocks(text);
    if (!blocks.length) return;
    let totalCreated = 0;
    for (const raw of blocks) {
      if (turnStopRequested) break;
      const key = raw;
      if (executedCanvasActionKeys.has(key)) continue;
      executedCanvasActionKeys.add(key);
      let payload = null;
      try { payload = JSON.parse(raw); } catch (e) {
        botMsg.blocks.push({ type: 'error', text: '画布动作 JSON 解析失败：' + safeStr(e.message || e) });
        renderBody();
        continue;
      }
      const actions = Array.isArray(payload) ? payload : (Array.isArray(payload.actions) ? payload.actions : [payload]);
      for (const action of actions) {
        if (turnStopRequested) break;
        try {
          const created = await executeCanvasAction(action || {});
          totalCreated += Array.isArray(created) ? created.length : (created ? 1 : 0);
        } catch (e) {
          botMsg.blocks.push({ type: 'error', text: '画布动作执行失败：' + safeStr(e.message || e) });
          renderBody();
        }
      }
    }
    if (totalCreated > 0) {
      botMsg.blocks.push({ type: 'tool', text: `已执行画布动作，影响 ${totalCreated} 个节点`, status: 'done' });
      showHint(`已执行画布动作，影响 ${totalCreated} 个节点`);
      renderBody();
    }
  }

  async function executeCanvasAction(action) {
    const type = safeStr(action.type || action.action).toLowerCase().replace(/-/g, '_');
    const options = action.options || {};
    if (type === 'remember_preference' || type === 'remember_preferences' || type === 'save_preference') {
      const note = safeStr(action.note || action.text || action.content || action.preference);
      if (!note) return [];
      const res = await fetch('/api/codex-agent/preferences/remember', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note }),
      });
      if (!res.ok) {
        const text = await res.text().catch(() => '');
        throw new Error(text || '保存偏好失败');
      }
      showHint('已记住画布偏好');
      return [];
    }
    if (type === 'generate_image' || type === 'generate_images' || type === 'create_image' || type === 'create_images') {
      updateBusy('正在画布生图');
      const items = action.items || action.prompts || action.images || [action];
      return await generateImageNodesToCanvas(items, options);
    }
    if (type === 'generate_video' || type === 'generate_videos' || type === 'create_video' || type === 'create_videos') {
      updateBusy('正在画布生视频');
      const items = action.items || action.prompts || action.videos || [action];
      return await generateVideoNodesToCanvas(items, options);
    }
    if (type === 'add_media' || type === 'add_image' || type === 'add_video' || type === 'add_media_nodes') {
      const items = action.items || action.media || action.images || action.videos || [action];
      return addMediaNodesToCanvas(items, options);
    }
    if (type === 'add_prompt' || type === 'add_text' || type === 'add_prompt_nodes') {
      const items = action.items || action.prompts || action.texts || [action];
      return addPromptNodesToCanvas(items, options);
    }
    if (type === 'add_loop' || type === 'add_loop_nodes') {
      const items = action.items || action.loops || [action];
      return addLoopNodesToCanvas(items, options);
    }
    if (type === 'group_node' || type === 'group_nodes' || type === 'create_group' || type === 'create_group_node') {
      const items = action.items || action.nodes || action.targets || [];
      return groupNodesOnCanvas(items, options);
    }
    if (type === 'ungroup_node' || type === 'ungroup_nodes' || type === 'split_group' || type === 'split_group_node') {
      const items = action.items || action.nodes || action.targets || [];
      return ungroupNodesOnCanvas(items, options);
    }
    if (type === 'rename_node' || type === 'rename_nodes' || type === 'set_node_title' || type === 'set_node_titles') {
      const items = action.items || action.nodes || action.targets || [action];
      return renameNodesOnCanvas(items, options);
    }
    if (type === 'move_node' || type === 'move_nodes' || type === 'position_node' || type === 'position_nodes') {
      const items = action.items || action.nodes || action.targets || [action];
      return moveNodesOnCanvas(items, options);
    }
    if (type === 'arrange_node' || type === 'arrange_nodes' || type === 'layout_nodes' || type === 'organize_nodes') {
      const items = action.items || action.nodes || action.targets || [];
      return arrangeNodesOnCanvas(items, options);
    }
    if (type === 'add_nodes') {
      const nodes = Array.isArray(action.nodes) ? action.nodes : (Array.isArray(action.items) ? action.items : []);
      const created = [];
      const nodeKind = node => safeStr(node.type || node.kind).toLowerCase().replace(/-/g, '_');
      const media = nodes.filter(node => ['image', 'video', 'media', 'smart_image'].includes(nodeKind(node)));
      const prompts = nodes.filter(node => ['prompt', 'text', 'smart_prompt', 'jimeng'].includes(nodeKind(node)));
      const loops = nodes.filter(node => ['loop', 'smart_loop'].includes(nodeKind(node)));
      created.push(...addMediaNodesToCanvas(media, options));
      created.push(...addPromptNodesToCanvas(prompts, options));
      created.push(...addLoopNodesToCanvas(loops, options));
      return created;
    }
    return [];
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
    const enabled = !!state.projectDir;
    input.contentEditable = state.projectDir ? 'true' : 'false';
    input.classList.toggle('cm-input-disabled', !state.projectDir);
    send.disabled = !enabled;
    send.textContent = state.status === 'busy' ? '停止' : '发送';
    send.classList.toggle('cm-send-stop', state.status === 'busy');

    const hint = $('#cm-hint');
    hint.classList.toggle('cm-foot-hint-busy', state.status === 'busy');
    if (!state.projectDir) hint.textContent = '未选项目';
    else if (state.status === 'busy') {
      const elapsed = state.busyStartedAt ? formatElapsed(Date.now() - state.busyStartedAt) : '00:00';
      hint.innerHTML = renderBusyHint(elapsed, state.busyLabel || '正在思考');
    }
    else hint.textContent = `thread: ${(state.threadId || '').slice(0, 8)}…`;
  }

  function renderBusyHint(elapsed, label) {
    return `<span class="cm-busy-pill" aria-label="${escapeAttr(`${elapsed} · ${label}`)}">
      <span class="cm-busy-pulse" aria-hidden="true"></span>
      <span class="cm-busy-time">${escapeHtml(elapsed)}</span>
      <span class="cm-busy-label">${escapeHtml(label)}</span>
      <span class="cm-busy-dots" aria-hidden="true"><i></i><i></i><i></i></span>
    </span>`;
  }

  function renderBody() {
    const body = $('#cm-body');
    if (state.messages.length === 0) {
      body.innerHTML = '<div class="cm-empty">选择一个项目文件夹，<br>然后开始和 Codex 对话。</div>';
      return;
    }
    body.innerHTML = state.messages.map(renderMessage).join('');
    bindCodeCopyButtons(body);
    body.scrollTop = body.scrollHeight;
  }

  function renderMessage(msg) {
    if (msg.role === 'user') {
      const userBlock = msg.blocks.find(b => b.type === 'text');
      const attachBlock = msg.blocks.find(b => b.type === 'attach');
      const normalized = normalizeUserDisplayText(safeStr(userBlock?.text));
      const attachItems = (attachBlock?.items && attachBlock.items.length) ? attachBlock.items : normalized.refs;
      let html = '';
      if (attachBlock || attachItems.length) {
        html += renderMessageAttachments(attachItems || [], attachBlock?.count || attachItems.length || 0);
      }
      html += `<div class="cm-msg-user">${escapeHtml(normalized.text)}</div>`;
      return `<div class="cm-msg cm-msg-user-wrap">${html}</div>`;
    }
    const blocksHtml = msg.blocks.map(renderBlock).filter(Boolean).join('');
    return `<div class="cm-msg cm-msg-bot-wrap">${blocksHtml}</div>`;
  }

  function normalizeUserDisplayText(text) {
    const raw = stripInternalContextBlocks(text);
    const refs = [];
    if (!raw.includes('canvas_agent_context')) return { text: raw, refs };

    let selectedCtx = raw.includes('selected_refs:') ? raw.split('selected_refs:', 2)[1] : '';
    ['\ncurrent_canvas_image_generation_defaults:', '\navailable_image_providers:', '\ncurrent_canvas_video_generation_defaults:', '\navailable_video_providers:'].forEach(marker => {
      if (selectedCtx.includes(marker)) selectedCtx = selectedCtx.split(marker, 1)[0];
    });
    const refRe = /-\s*id:\s*([^\n]+)([\s\S]*?)(?=\n-\s*id:|$)/g;
    let m;
    while ((m = refRe.exec(selectedCtx)) !== null) {
      const block = m[2] || '';
      const get = (key) => {
        const hit = block.match(new RegExp(`\\n\\s*${key}:\\s*([^\\n]*)`));
        return hit ? hit[1].trim() : '';
      };
      const sourcePath = get('source_path');
      const localPath = get('local_path');
      const url = get('url');
      const path = sourcePath || localPath || url;
      if (!path) continue;
      refs.push({
        refId: m[1].trim(),
        name: get('name') || extractName(path),
        kind: get('kind') || mediaKindFromUrl(path),
        url: path ? `/api/codex-agent/file/view?path=${encodeURIComponent(path)}` : '',
        source_path: sourcePath,
        local_path: localPath,
        canvasKind: get('canvas_kind'),
        nodeId: get('node_id'),
        imageIndex: get('image_index'),
        nodeTitle: get('node_title'),
      });
    }

    let clean = raw.replace(/<canvas_agent_context>[\s\S]*?<\/canvas_agent_context>/g, '').trim();
    clean = clean.replace(/^用户请求：\s*/u, '').trim();
    if (!clean && raw.includes('用户请求：')) clean = raw.split('用户请求：').pop().trim();
    return { text: clean || raw, refs };
  }

  function renderMessageAttachments(items, fallbackCount = 0) {
    if (!Array.isArray(items) || !items.length) {
      return `<div class="cm-msg-attach">📎 ${escapeHtml(safeStr(fallbackCount))} 个附件</div>`;
    }
    const thumbs = items.map((a, i) => {
      const kind = safeStr(a.kind || mediaKindFromUrl(a.url));
      const preview = attachmentPreviewHtml(a, kind, 'cm-attach-kind');
      return `<span class="cm-msg-attach-thumb" title="图${i + 1} · ${escapeAttr(a.name)}">
        ${preview}
        <span class="cm-msg-attach-index">${i + 1}</span>
      </span>`;
    }).join('');
    return `<div class="cm-msg-attach-list">${thumbs}</div>`;
  }

  function renderBlock(b) {
    const type = b.type;
    if (type === 'text') {
      const text = cleanAgentDisplayText(safeStr(b.text));
      if (!text) return '';
      return `<div class="cm-msg-bot cm-md">${renderMarkdown(text)}${b.streaming ? '<span class="cm-stream-caret"> ▍</span>' : ''}</div>`;
    }
    if (type === 'thinking') {
      const t = cleanAgentDisplayText(safeStr(b.text));
      if (!t) return '';
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

  function bindCodeCopyButtons(root) {
    root.querySelectorAll('.cm-code-copy').forEach(btn => {
      btn.addEventListener('click', async e => {
        e.preventDefault();
        e.stopPropagation();
        const box = btn.closest('.cm-code-block');
        const code = box?.querySelector('code')?.innerText || '';
        if (!code) return;
        copyTextFromButton(btn, code);
      });
    });
    root.querySelectorAll('.cm-code-expand').forEach(btn => {
      btn.addEventListener('click', e => {
        e.preventDefault();
        e.stopPropagation();
        const box = btn.closest('.cm-code-block');
        const code = box?.querySelector('code')?.innerText || '';
        const label = box?.querySelector('.cm-code-lang')?.innerText || 'code';
        if (code) openCodeModal(code, label);
      });
    });
    root.querySelectorAll('.cm-code-toggle').forEach(btn => {
      btn.addEventListener('click', e => {
        e.preventDefault();
        e.stopPropagation();
        const box = btn.closest('.cm-code-block');
        if (!box) return;
        const willCollapse = !box.classList.contains('cm-code-collapsed');
        box.classList.toggle('cm-code-collapsed', willCollapse);
        btn.textContent = willCollapse ? '▾' : '▴';
        btn.title = willCollapse ? '展开代码块' : '收起代码块';
        btn.setAttribute('aria-expanded', willCollapse ? 'false' : 'true');
      });
    });
  }

  async function copyTextFromButton(btn, text) {
    try {
      await navigator.clipboard.writeText(text);
      setCopyButtonState(btn, '已复制');
    } catch {
      if (fallbackCopyText(text)) setCopyButtonState(btn, '已复制');
      else setCopyButtonState(btn, '复制失败');
    }
  }

  function setCopyButtonState(btn, text) {
    const old = btn.textContent;
    btn.textContent = text === '已复制' ? '✓' : text === '复制失败' ? '!' : text;
    btn.disabled = true;
    setTimeout(() => {
      btn.textContent = old || '⧉';
      btn.disabled = false;
    }, 1200);
  }

  function fallbackCopyText(text) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } catch {
      ok = false;
    }
    ta.remove();
    return ok;
  }

  function renderMarkdown(text) {
    const raw = safeStr(text).replace(/\r\n/g, '\n');
    if (!raw.trim()) return '';

    const parts = [];
    const re = /```([^\n`]*)\n?([\s\S]*?)```/g;
    let last = 0;
    let m;
    while ((m = re.exec(raw)) !== null) {
      if (m.index > last) parts.push(renderMarkdownText(raw.slice(last, m.index)));
      parts.push(renderCodeBlock(m[2] || '', m[1] || ''));
      last = re.lastIndex;
    }
    if (last < raw.length) parts.push(renderMarkdownText(raw.slice(last)));
    return parts.join('');
  }

  function renderCodeBlock(code, lang) {
    const label = safeStr(lang).trim().split(/\s+/)[0] || 'code';
    const displayLabel = label.toLowerCase() === 'text' ? 'Prompt' : label;
    const clean = code.replace(/\n$/, '');
    const lineCount = clean ? clean.split('\n').length : 0;
    const summary = `${lineCount || 1} 行 · ${clean.length} 字符`;
    return `<div class="cm-code-block">
      <div class="cm-code-head">
        <span class="cm-code-title">
          <span class="cm-code-lang">${escapeHtml(displayLabel)}</span>
          <span class="cm-code-summary">${escapeHtml(summary)}</span>
        </span>
        <span class="cm-code-actions">
          <button type="button" class="cm-code-toggle cm-icon-btn" aria-expanded="true" title="收起代码块">▴</button>
          <button type="button" class="cm-code-expand cm-icon-btn" title="放大查看">⛶</button>
          <button type="button" class="cm-code-copy cm-icon-btn" title="复制">⧉</button>
        </span>
      </div>
      <pre><code>${escapeHtml(clean)}</code></pre>
    </div>`;
  }

  function openCodeModal(code, label = 'code') {
    const modal = ensureCodeModal();
    modal.querySelector('.cm-code-modal-title').textContent = safeStr(label) || 'code';
    modal.querySelector('.cm-code-modal-code').textContent = safeStr(code);
    modal.classList.add('cm-code-modal-open');
    modal.setAttribute('aria-hidden', 'false');
  }

  function closeCodeModal() {
    const modal = document.querySelector('.cm-code-modal');
    if (!modal) return;
    modal.classList.remove('cm-code-modal-open');
    modal.setAttribute('aria-hidden', 'true');
  }

  function ensureCodeModal() {
    let modal = document.querySelector('.cm-code-modal');
    if (modal) return modal;
    modal = document.createElement('div');
    modal.className = 'cm-code-modal';
    modal.setAttribute('aria-hidden', 'true');
    modal.innerHTML = `
      <div class="cm-code-modal-backdrop"></div>
      <div class="cm-code-modal-dialog" role="dialog" aria-modal="true" aria-label="代码块内容">
        <div class="cm-code-modal-head">
          <span class="cm-code-modal-title">code</span>
          <span class="cm-code-modal-actions">
            <button type="button" class="cm-code-modal-copy">复制</button>
            <button type="button" class="cm-code-modal-close" title="关闭">×</button>
          </span>
        </div>
        <pre class="cm-code-modal-pre"><code class="cm-code-modal-code"></code></pre>
      </div>`;
    document.body.appendChild(modal);
    modal.querySelector('.cm-code-modal-backdrop').addEventListener('click', closeCodeModal);
    modal.querySelector('.cm-code-modal-close').addEventListener('click', closeCodeModal);
    modal.querySelector('.cm-code-modal-copy').addEventListener('click', () => {
      const btn = modal.querySelector('.cm-code-modal-copy');
      const code = modal.querySelector('.cm-code-modal-code')?.innerText || '';
      if (code) copyTextFromButton(btn, code);
    });
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && modal.classList.contains('cm-code-modal-open')) closeCodeModal();
    });
    return modal;
  }

  function renderMarkdownText(text) {
    const src = safeStr(text).trim();
    if (!src) return '';
    const lines = src.split('\n');
    const html = [];

    for (let i = 0; i < lines.length;) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }

      const heading = line.match(/^(#{1,4})\s+(.+)$/);
      if (heading) {
        const level = heading[1].length;
        html.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
        i++;
        continue;
      }

      if (/^\s*>\s?/.test(line)) {
        const quote = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          quote.push(lines[i].replace(/^\s*>\s?/, ''));
          i++;
        }
        html.push(`<blockquote>${renderInlineMarkdown(quote.join('\n')).replace(/\n/g, '<br>')}</blockquote>`);
        continue;
      }

      if (/^\s*[-*]\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
          items.push(`<li>${renderInlineMarkdown(lines[i].replace(/^\s*[-*]\s+/, ''))}</li>`);
          i++;
        }
        html.push(`<ul>${items.join('')}</ul>`);
        continue;
      }

      if (/^\s*\d+\.\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
          items.push(`<li>${renderInlineMarkdown(lines[i].replace(/^\s*\d+\.\s+/, ''))}</li>`);
          i++;
        }
        html.push(`<ol>${items.join('')}</ol>`);
        continue;
      }

      const para = [line];
      i++;
      while (
        i < lines.length &&
        lines[i].trim() &&
        !/^(#{1,4})\s+/.test(lines[i]) &&
        !/^\s*>\s?/.test(lines[i]) &&
        !/^\s*[-*]\s+/.test(lines[i]) &&
        !/^\s*\d+\.\s+/.test(lines[i])
      ) {
        para.push(lines[i]);
        i++;
      }
      html.push(`<p>${renderInlineMarkdown(para.join('\n')).replace(/\n/g, '<br>')}</p>`);
    }

    return html.join('');
  }

  function renderInlineMarkdown(text) {
    const codeSpans = [];
    let html = escapeHtml(safeStr(text)).replace(/`([^`\n]+)`/g, (_, code) => {
      const key = `@@CM_CODE_${codeSpans.length}@@`;
      codeSpans.push(`<code>${code}</code>`);
      return key;
    });

    html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/__([^_]+)__/g, '<strong>$1</strong>');
    html = html.replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    html = html.replace(/(^|[\s(])_([^_\n]+)_/g, '$1<em>$2</em>');

    codeSpans.forEach((code, i) => {
      html = html.replace(`@@CM_CODE_${i}@@`, code);
    });
    return html;
  }

  function shortPath(p) {
    if (!p) return '';
    if (p.length <= 32) return p;
    return '…' + p.slice(-30);
  }

  function extractName(url) {
    if (!url) return 'image';
    try {
      const u = new URL(url, window.location.origin);
      const p = u.pathname;
      const last = p.split('/').filter(Boolean).pop() || 'image';
      // 去掉常见的扩展名前缀（如 ai_ref_xxxxx.png）—— 直接用最后一段
      return decodeURIComponent(last).slice(0, 40);
    } catch {
      return shortPath(url);
    }
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
    if (state.mentionOpen) {
      const pop = $('#cm-mention-pop');
      const input = $('#cm-input');
      if (pop && !pop.contains(e.target) && input && !input.contains(e.target)) {
        closeMentionPicker();
      }
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', inject);
  } else {
    inject();
  }
})();
