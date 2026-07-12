/* =========================================================================
 * Codex Agent Panel（独立 IIFE，挂在 canvas.html / smart-canvas.html）
 * 不动 canvas.js / smart-canvas.js / 原 html 主体
 *
 * Agent 面板负责自己的历史、恢复、任务状态和画布动作块。
 * Codex thread 只作为底层执行通道，不作为聊天 UI 的主历史来源。
 * ========================================================================= */

(function () {
  'use strict';

  const $ = (s) => document.querySelector(s);

  // ---------------- 状态 ----------------
  const state = {
    open: false,
    status: 'booting',
    projectDir: '',
    threadId: '',
    conversationId: '',
    messages: [],         // [{role, blocks: [...]}]
    projects: [],
    sessions: [],
    histAllSessions: [],
    histMode: 'current',
    historySort: 'last_used',
    projectSort: 'last_used',
    histSortMenuOpen: false,
    projectSortMenuOpen: false,
    histExpandedDirs: {},
    attachments: [],      // [{url, name, kind, nodeId, imageIndex, canvasKind}] 待发给 Codex 的素材附件
    projectsLoading: false,
    projectsLoaded: false,
    workdirReady: false,
    projectDeleteMode: false,
    workdirDialogOpen: false,
    workdirPresets: [],
    workdirPresetRoot: '',
    workdirChildren: [],
    workdirLoading: false,
    projPopOpen: false,
    histPopOpen: false,
    mentionOpen: false,
    mentionItems: [],
    mentionQuery: '',
    mentionStart: -1,
    commandOpen: false,
    commandItems: [],
    commandQuery: '',
    inputMode: 'auto',
    inputScope: 'auto',
    approvalPolicy: 'auto',
    busyStartedAt: 0,
    busyLabel: '',
    scrollTop: 0,
  };

  let currentAgentMsgId = null;
  let currentAgentText = '';
  let currentReasoningId = null;
  let currentReasoningText = '';
  let currentToolId = null;
  let pendingCanvasActionPromises = [];
  let currentTurnAbortController = null;
  let currentStreamReader = null;
  let currentBackendTaskId = '';
  let currentBackendTaskOffset = 0;
  let currentBackendManaged = false;
  let turnStopRequested = false;
  let activeTurnStatusBlock = null;
  let busyTimer = null;
  let autoSelectProjectInFlight = false;
  let panelOperation = '';
  let restoreInFlight = false;
  let panelStateSaveTimer = null;
  let panelStateSaveInFlight = false;
  let suppressPanelSave = false;
  let lastRecoveryAlertAt = 0;
  const addedImagePaths = new Set();
  const executedCanvasActionKeys = new Set();
  const panelSizeKey = 'codex-agent-panel-size-v1';
  const panelStatePrefix = 'codex-agent-panel-state-v5:';
  const panelRecentStateKey = 'codex-agent-panel-recent-state-v5';
  const recoveringStatuses = new Set(['booting', 'loading_projects', 'opening_project', 'loading_history', 'reconnecting_task']);
  const inputModes = [
    { id: 'operate', label: '操作画布' },
    { id: 'chat', label: '聊天' },
    { id: 'generate', label: '生成' },
    { id: 'organize', label: '整理' },
    { id: 'analyze', label: '分析' },
  ];
  const inputScopes = [
    { id: 'viewport', label: '当前视口' },
    { id: 'selected', label: '选中节点' },
    { id: 'canvas', label: '全画布' },
    { id: 'node', label: '指定节点' },
  ];
  const approvalPolicies = [
    { id: 'auto', label: '自动执行' },
    { id: 'confirm-risky', label: '高风险确认' },
    { id: 'confirm', label: '执行前确认' },
  ];
  const slashCommands = [
    { id: 'organize', label: '/整理', insert: '/整理 ', desc: '整理选中节点或当前视口内容' },
    { id: 'rename', label: '/重命名', insert: '/重命名 ', desc: '按规则重命名节点素材显示名' },
    { id: 'prompt', label: '/生成提示词', insert: '/生成提示词 ', desc: '把参考图或主题写成提示词节点' },
    { id: 'image', label: '/创建生图节点', insert: '/创建生图节点 ', desc: '创建智能画布生图节点' },
    { id: 'video', label: '/创建视频节点', insert: '/创建视频节点 ', desc: '创建视频生成节点' },
    { id: 'summarize', label: '/总结画布', insert: '/总结画布 ', desc: '总结当前视口或全画布内容' },
    { id: 'locate', label: '/定位', insert: '/定位 ', desc: '定位、选中或高亮节点' },
    { id: 'batch', label: '/批量处理', insert: '/批量处理 ', desc: '批量执行多步骤任务' },
  ];
  const contextLevelLabels = {
    0: '纯聊天',
    1: '轻量画布',
    2: '局部节点',
    3: '全画布',
  };

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
  function icon(name) { return `<i data-lucide="${escapeAttr(name)}" aria-hidden="true"></i>`; }
  function refreshPanelIcons() {
    if (window.lucide?.createIcons) window.lucide.createIcons({ attrs: { 'stroke-width': 1.8 } });
  }
  function workdirLabel(dir) { return safeStr(dir).trim() || '无目录'; }
  function workdirMetaLabel(dir) { return safeStr(dir).trim() || '无目录'; }
  function hasWorkdirSelection() { return Boolean(state.workdirReady); }
  function projectQueryParam(dir) { return encodeURIComponent(safeStr(dir)); }

  function isRecoveryErrorText(text) {
    return /^(恢复上次对话失败|恢复会话失败|后台任务接回失败|历史回放失败|启动 Codex 失败|新对话失败)/.test(safeStr(text).trim());
  }

  function isTransientStatusText(text) {
    const clean = normalizeAgentErrorText(text);
    return /^(Codex 正在重连|网络连接中断|请求异常|后台任务接回失败)/.test(clean);
  }

  function cleanPanelMessages(messages) {
    return (Array.isArray(messages) ? messages : []).map(msg => {
      if (!msg || typeof msg !== 'object') return null;
      const blocks = (Array.isArray(msg.blocks) ? msg.blocks : [])
        .filter(block => {
          if (!block) return false;
          if (block.type === 'transient_notice') return false;
          if (block.type === 'process' && !Array.isArray(block.steps)) return false;
          if (block.type === 'process' && !block.steps.length) return false;
          if (block.type === 'thinking' && !normalizeThinkingText(block.text)) return false;
          if (block.type === 'error' && (isRecoveryErrorText(block.text) || isTransientStatusText(block.text))) return false;
          return true;
        });
      if (!blocks.length) return null;
      return { ...msg, blocks };
    }).filter(Boolean);
  }

  function alertRecoveryFailure(message) {
    const text = safeStr(message || '恢复失败，请稍后重试');
    const now = Date.now();
    showHint(text, 6000);
    if (now - lastRecoveryAlertAt > 1500) {
      lastRecoveryAlertAt = now;
      alert(text);
    }
  }

  function appendSystemError(message) {
    const text = safeStr(message || '发生了一个 Agent 状态错误');
    state.messages.push({ role: 'bot', blocks: [{ type: 'error', text }] });
    renderBody();
    savePanelSnapshot({ immediate: true });
  }

  function appendSystemNotice(message) {
    const text = safeStr(message || '').trim();
    if (!text) return;
    state.messages.push({ role: 'bot', blocks: [{ type: 'system_notice', text }] });
    renderBody();
    savePanelSnapshot({ immediate: true });
  }

  function normalizeAgentErrorText(value) {
    let text = safeStr(value || '').trim();
    if (!text) return '请求异常，请稍后重试';
    for (let i = 0; i < 2; i += 1) {
      if (!/^\s*[\[{]/.test(text)) break;
      try {
        const parsed = JSON.parse(text);
        text = safeStr(parsed.message || parsed.error || parsed.additionalDetails || parsed.code || text);
        const info = parsed.codexErrorInfo || parsed.errorInfo || parsed.data;
        if (info && typeof info === 'object') {
          text += ' ' + safeStr(info.additionalDetails || info.message || info.responseStreamDisconnected || '');
        }
      } catch {
        break;
      }
    }
    if (/tls handshake eof|stream disconnected before completion|responseStreamDisconnected/i.test(text)) {
      return '网络连接中断，Codex 响应流提前断开。可以稍后重试，或检查当前网络/代理状态。';
    }
    if (/reconnecting\.\.\./i.test(text)) {
      return 'Codex 正在重连，请稍后重试。';
    }
    return text.replace(/\s+/g, ' ').slice(0, 260);
  }

  function pushErrorBlockOnce(botMsg, text) {
    const clean = normalizeAgentErrorText(text);
    const last = botMsg?.blocks?.[botMsg.blocks.length - 1];
    const type = isTransientStatusText(clean) ? 'transient_notice' : 'error';
    if (last && last.type === type && safeStr(last.text) === clean) return;
    botMsg.blocks.push({ type, text: clean });
  }

  function normalizeThinkingText(value) {
    const text = cleanAgentDisplayText(safeStr(value)).trim();
    if (!text || text === '[]' || text === '{}' || text === 'null' || text === 'undefined') return '';
    return text;
  }

  function ensureProcessBlock(botMsg) {
    // 工具组只合并与自己相邻的调用。这样新的文字回复会自然把下一组
    // 工具/命令放在它之后，而不是把整轮调用都堆到消息顶部。
    let block = null;
    for (let index = botMsg.blocks.length - 1; index >= 0; index -= 1) {
      const candidate = botMsg.blocks[index];
      // 思考会被统一渲染到消息顶部，临时状态与被清洗为空的协议文本也
      // 不属于可见时间线；它们不能把相邻工具调用拆成两个工具组。
      if (isInvisibleTimelineBlock(candidate)) continue;
      block = candidate;
      break;
    }
    if (block?.type !== 'process') block = null;
    if (!block) {
      block = {
        type: 'process',
        title: '工具与命令',
        status: 'running',
        startedAt: botMsg.startedAt || state.busyStartedAt || Date.now(),
        steps: [],
      };
      botMsg.blocks.push(block);
    } else if (block.status !== 'running' && state.status === 'busy') {
      block.status = 'running';
      delete block.endedAt;
    }
    if (!Array.isArray(block.steps)) block.steps = [];
    return block;
  }

  function isInvisibleTimelineBlock(block) {
    if (!block || typeof block !== 'object') return true;
    if (block.type === 'thinking' || block.type === 'agent_status') return true;
    if (block.type === 'text') return !cleanAgentDisplayText(safeStr(block.text)).trim();
    return false;
  }

  function finishProcessBlocks(botMsg, status = 'done') {
    (botMsg.blocks || []).forEach(block => {
      if (block.type !== 'process' || block.status !== 'running') return;
      block.status = status;
      block.endedAt = Date.now();
      (Array.isArray(block.steps) ? block.steps : []).forEach(step => {
        if (step && step.status === 'running') {
          step.status = status;
          step.endedAt = Date.now();
        }
      });
    });
    if (status !== 'running' && !botMsg.completedAt) botMsg.completedAt = Date.now();
  }

  function addProcessStep(botMsg, step = {}) {
    const block = ensureProcessBlock(botMsg);
    const next = {
      kind: safeStr(step.kind || 'step'),
      label: safeStr(step.label || '处理'),
      tool: safeStr(step.tool || ''),
      command: safeStr(step.command || ''),
      status: safeStr(step.status || 'running'),
      summary: safeStr(step.summary || ''),
      detail: safeStr(step.detail || ''),
      startedAt: step.startedAt || Date.now(),
      id: safeStr(step.id || ''),
    };
    block.steps.push(next);
    return next;
  }

  function findProcessStep(botMsg, id) {
    const key = safeStr(id);
    if (!key) return null;
    for (const block of (botMsg.blocks || [])) {
      if (block?.type !== 'process' || !Array.isArray(block.steps)) continue;
      const step = block.steps.find(item => safeStr(item.id) === key);
      if (step) return step;
    }
    return null;
  }

  function updateProcessToolResult(botMsg, params = {}, result = {}, nodes = []) {
    const tool = safeStr(params.tool || params.name || result.tool);
    let step = null;
    for (let blockIndex = botMsg.blocks.length - 1; blockIndex >= 0 && !step; blockIndex -= 1) {
      const steps = Array.isArray(botMsg.blocks[blockIndex]?.steps) ? botMsg.blocks[blockIndex].steps : [];
      for (let i = steps.length - 1; i >= 0; i -= 1) {
        const candidate = steps[i];
        if (!candidate || candidate.kind !== 'canvas_query') continue;
        if (!tool || safeStr(candidate.tool) === tool) {
          step = candidate;
          break;
        }
      }
    }
    if (!step) {
      step = addProcessStep(botMsg, {
        kind: 'canvas_query',
        label: '查询画布',
        tool,
        status: 'running',
      });
    }
    const ok = result.ok !== false;
    const nodeCount = Number(result.node_count ?? nodes.length ?? 0);
    step.status = ok ? 'done' : 'error';
    step.endedAt = Date.now();
    step.summary = ok
      ? `返回 ${nodeCount} 个节点`
      : `失败：${safeStr(result.message || '未知原因')}`;
    step.detail = tool ? `工具：${tool}` : '';
    return step;
  }

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
        <button class="cm-icon-btn" id="cm-new" title="新对话" aria-label="新对话">${icon('plus')}</button>
        <button class="cm-icon-btn" id="cm-history" title="历史会话" aria-label="历史会话">${icon('history')}</button>
        <button class="cm-icon-btn" id="cm-close" title="关闭" aria-label="关闭">${icon('x')}</button>
      </div>
      <div class="cm-popover" id="cm-proj-pop"></div>
      <div class="cm-popover" id="cm-hist-pop"></div>
      <div class="cm-workdir-dialog" id="cm-workdir-dialog"></div>
      <div class="cm-mention-pop" id="cm-mention-pop"></div>
      <div class="cm-command-pop" id="cm-command-pop"></div>
      <div class="cm-resize-handle" id="cm-resize" title="从左下角拖拽调整窗口大小"></div>
      <div class="cm-body" id="cm-body">
        <div class="cm-empty">选择一个项目文件夹，<br>然后开始和 Codex 对话。</div>
      </div>
      <div class="cm-foot">
        <div class="cm-composer">
          <div class="cm-ref-rail cm-attach-list" id="cm-attach-list"></div>
          <div class="cm-input cm-input-disabled" id="cm-input" contenteditable="false" data-placeholder="输入消息，回车发送（Shift+Enter 换行）"></div>
          <div class="cm-composer-bar">
            <div class="cm-toolbar-left">
              <button class="cm-tool-btn" id="cm-attach-canvas" title="添加当前画布选中节点">＋<span id="cm-attach-count">0</span></button>
              <button class="cm-tool-btn" id="cm-at" title="@ 引用附件或画布对象">@</button>
              <button class="cm-tool-btn" id="cm-slash" title="/ 命令">/</button>
            </div>
            <div class="cm-toolbar-right">
              <button class="cm-pill-btn" id="cm-approval" title="切换执行确认规则"></button>
              <button class="cm-send" id="cm-send" disabled>发送</button>
            </div>
          </div>
        </div>
        <div class="cm-foot-row">
          <span class="cm-foot-selection" id="cm-selection-hint">选中 0 节点</span>
          <span class="cm-foot-hint" id="cm-hint">未选项目</span>
        </div>
      </div>
    `;
    document.body.appendChild(panel);
    refreshPanelIcons();

    $('#cm-close').addEventListener('click', togglePanel);
    $('#cm-proj').addEventListener('click', (e) => {
      e.stopPropagation();
      toggleProjPop();
    });
    $('#cm-new').addEventListener('click', onNewSession);
    $('#cm-history').addEventListener('click', (e) => {
      // 图标 SVG 的点击会继续冒泡到 document；显式截断，避免刚打开的
      // 历史下拉被全局“点到外面关闭”逻辑立即收起。
      e.stopPropagation();
      toggleHistPop();
    });
    $('#cm-send').addEventListener('click', onSend);
    $('#cm-input').addEventListener('keydown', onInputKey);
    $('#cm-input').addEventListener('input', onInputAssist);
    $('#cm-input').addEventListener('paste', onInputPaste);
    $('#cm-attach-canvas').addEventListener('click', onAttachFromCanvas);
    $('#cm-at').addEventListener('click', openMentionShortcut);
    $('#cm-slash').addEventListener('click', openCommandShortcut);
    $('#cm-approval').addEventListener('click', () => cycleInputSetting('approvalPolicy', approvalPolicies));
    $('#cm-body').addEventListener('scroll', () => {
      state.scrollTop = Math.round($('#cm-body')?.scrollTop || 0);
      savePanelSnapshot();
    }, { passive: true });
    initPanelResize();
    document.addEventListener('click', onDocClick);
    restorePanelSnapshot();
    setStatusUI();
    renderAttach();
    window.setInterval(updateSelectionHint, 450);
    queueMicrotask(bootstrapPanelRestore);
  }

  function togglePanel() {
    state.open = !state.open;
    $('#cm-panel').classList.toggle('cm-open', state.open);
    if (state.open) loadProjects();
    savePanelSnapshot();
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

  function formatDurationHuman(ms) {
    const total = Math.max(0, Math.floor(Number(ms || 0) / 1000));
    const mins = Math.floor(total / 60);
    const secs = total % 60;
    if (mins > 0) return `${mins}m ${secs}s`;
    return `${secs}s`;
  }

  function isRecoveringStatus(status = state.status) {
    return recoveringStatuses.has(status);
  }

  function isLockedStatus(status = state.status) {
    return isRecoveringStatus(status) || status === 'busy';
  }

  function beginPanelOperation(name, label, options = {}) {
    if (panelOperation && !options.force) {
      showHint(state.busyLabel || '正在加载，请稍等');
      return false;
    }
    panelOperation = name;
    if (!state.busyStartedAt) state.busyStartedAt = Date.now();
    state.busyLabel = label || '正在加载';
    state.status = options.status || name || 'busy';
    if (!busyTimer) busyTimer = setInterval(setStatusUI, 1000);
    setStatusUI();
    return true;
  }

  function endPanelOperation(status = 'ready') {
    panelOperation = '';
    finishBusy(status);
  }

  function setRecovering(status, label) {
    state.status = status;
    if (!state.busyStartedAt) state.busyStartedAt = Date.now();
    state.busyLabel = label || '正在恢复';
    if (!busyTimer) busyTimer = setInterval(setStatusUI, 1000);
    setStatusUI();
  }

  function setBusy(label = '正在思考') {
    panelOperation = 'busy';
    if (!state.busyStartedAt) state.busyStartedAt = Date.now();
    state.busyLabel = label;
    state.status = 'busy';
    if (!busyTimer) {
      busyTimer = setInterval(setStatusUI, 1000);
    }
    setStatusUI();
    updateActiveTurnStatus(label);
  }

  function updateBusy(label) {
    if (state.status === 'busy' || isRecoveringStatus()) {
      state.busyLabel = label || state.busyLabel || '正在思考';
      setStatusUI();
      updateActiveTurnStatus(state.busyLabel);
    }
  }

  function liveTurnStatusLabel(label = '') {
    const text = safeStr(label);
    if (/发送|准备选中素材/.test(text)) return '发送中';
    if (/执行|运行|生成|提交|画布工具/.test(text)) return '运行中';
    return '思考中';
  }

  function updateActiveTurnStatus(label) {
    if (!activeTurnStatusBlock) return;
    activeTurnStatusBlock.text = liveTurnStatusLabel(label);
    // The block is deliberately transient, but it must be visible immediately
    // after send and update while the task is streaming.
    renderBody();
  }

  function clearActiveTurnStatus() {
    if (!activeTurnStatusBlock) return;
    state.messages.forEach(message => {
      if (!Array.isArray(message?.blocks)) return;
      message.blocks = message.blocks.filter(block => block !== activeTurnStatusBlock);
    });
    activeTurnStatusBlock = null;
  }

  function finishBusy(status = 'ready') {
    state.status = status;
    if (status !== 'busy') panelOperation = '';
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
  async function loadProjects(options = {}) {
    const wasLocked = isLockedStatus();
    const silent = Boolean(options.silent);
    if (!wasLocked && !silent) setRecovering('loading_projects', '正在加载项目目录');
    state.projectsLoading = true;
    renderProjPop();
    try {
      const qs = new URLSearchParams();
      const canvasId = currentCanvasId();
      if (canvasId) qs.set('canvas_id', canvasId);
      const r = await fetch('/api/codex-agent/history/projects' + (qs.toString() ? `?${qs}` : ''));
      const d = await r.json();
      const projects = Array.isArray(d.projects) ? d.projects : [];
      state.projects = projects
        .map(p => ({
          project_dir: safeStr(p.project_dir),
          session_count: Number(p.conversation_count || 0),
          last_active: p.last_conversation_at || p.updated_at || p.last_opened_at || '',
          created_at: p.created_at || p.createdAt || 0,
          thread_id: p.latest_thread_id || '',
          conversation_id: p.latest_conversation_id || '',
          source: 'canvas-agent',
          fixed: false,
        }))
        .filter((item, index, arr) => arr.findIndex(x => x.project_dir === item.project_dir) === index);
      if (!state.projects.some(p => p.project_dir === '')) {
        state.projects.push({
          project_dir: '',
          session_count: 0,
          last_active: '',
          created_at: 0,
          thread_id: '',
          conversation_id: '',
          source: 'no-project',
          fixed: true,
        });
      }
      sortProjectsInPlace(state.projects);
      const latest = state.projects.find(p => p.conversation_id || p.thread_id);
      if (!options.noAutoSelect && !state.workdirReady && state.open && latest && !autoSelectProjectInFlight && !restoreInFlight) {
        autoSelectProjectInFlight = true;
        selectProject(latest.project_dir, {
          threadId: latest.thread_id,
          conversationId: latest.conversation_id,
          silent: true,
          force: true,
        }).finally(() => {
          autoSelectProjectInFlight = false;
        });
      }
    } catch (e) { console.error('loadProjects failed', e); }
    finally {
      state.projectsLoading = false;
      state.projectsLoaded = true;
      renderProjPop();
      if (!wasLocked && !silent && state.status === 'loading_projects') finishBusy('ready');
    }
  }

  function toggleProjPop() {
    if (isLockedStatus()) { showHint(state.busyLabel || '正在加载，请稍等'); return; }
    state.projPopOpen = !state.projPopOpen;
    state.histPopOpen = false;
    state.mentionOpen = false;
    state.commandOpen = false;
    renderHistPop();
    closeMentionPicker();
    closeCommandPicker();
    if (state.projPopOpen) loadProjects({ silent: true, noAutoSelect: true });
    renderProjPop();
  }

  function renderProjPop() {
    const pop = $('#cm-proj-pop');
    if (!state.projPopOpen) { pop.classList.remove('cm-popover-open'); return; }
    pop.classList.add('cm-popover-open');
    const html = [];
    if (state.projectsLoading && !state.projectsLoaded) {
      html.push('<div class="cm-popover-empty">正在加载项目目录…</div>');
    } else if (isLockedStatus()) {
      html.push('<div class="cm-popover-empty">正在加载，请稍等…</div>');
    } else {
      html.push('<div class="cm-popover-section-title">当前画布的工作目录</div>');
      for (const p of state.projects) {
        const active = state.workdirReady && p.project_dir === state.projectDir ? ' cm-item-active' : '';
        const canDelete = state.projectDeleteMode && !p.fixed;
        const icon = p.project_dir ? '📁' : '∅';
        html.push(`
          <div class="cm-popover-item cm-workdir-item${active}" data-dir="${escapeAttr(p.project_dir)}" data-thread-id="${escapeAttr(p.thread_id)}" data-conversation-id="${escapeAttr(p.conversation_id)}">
            <span class="cm-workdir-main">${icon} ${escapeHtml(workdirLabel(p.project_dir))}</span>
            ${canDelete ? `<button class="cm-workdir-delete" data-dir="${escapeAttr(p.project_dir)}" title="从当前画布目录列表隐藏">×</button>` : ''}
            <span class="cm-popover-meta">${p.session_count} 个会话${p.last_active ? ' · 最近 ' + escapeHtml(p.last_active) : ''}</span>
          </div>
        `);
      }
      html.push(`
        <div class="cm-popover-footer cm-popover-icon-actions">
          <button class="cm-popover-action cm-popover-action-icon" id="cm-workdir-add" title="添加工作目录" aria-label="添加工作目录">${icon('folder-plus')}</button>
          <button class="cm-popover-action cm-popover-action-icon${state.projectDeleteMode ? ' cm-popover-action-active' : ''}" id="cm-workdir-manage" title="隐藏工作目录" aria-label="隐藏工作目录">${icon('folder-minus')}</button>
          <button class="cm-popover-action cm-popover-action-icon${state.projectSortMenuOpen ? ' cm-popover-action-active' : ''}" id="cm-workdir-sort" title="排序：${state.projectSort === 'created' ? '添加时间' : '最新使用时间'}" aria-label="排序">${icon('arrow-up-down')}</button>
          ${renderSortMenu('project')}
        </div>
      `);
    }
    pop.innerHTML = html.join('');
    pop.querySelectorAll('.cm-popover-item').forEach(el => {
      el.addEventListener('click', () => {
        if (state.projectDeleteMode) return;
        selectProject(el.getAttribute('data-dir'), { conversationId: el.getAttribute('data-conversation-id') || '' });
        state.projPopOpen = false;
        renderProjPop();
      });
    });
    pop.querySelectorAll('.cm-workdir-delete').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        hideProjectForCanvas(btn.getAttribute('data-dir'));
      });
    });
    $('#cm-workdir-add')?.addEventListener('click', (e) => {
      e.stopPropagation();
      openWorkdirDialog();
    });
    $('#cm-workdir-manage')?.addEventListener('click', (e) => {
      e.stopPropagation();
      state.projectDeleteMode = !state.projectDeleteMode;
      renderProjPop();
    });
    $('#cm-workdir-sort')?.addEventListener('click', (e) => {
      e.stopPropagation();
      state.projectSortMenuOpen = !state.projectSortMenuOpen;
      renderProjPop();
    });
    bindSortMenu(pop, 'project');
    refreshPanelIcons();
  }

  async function openWorkdirDialog() {
    if (isLockedStatus()) { showHint(state.busyLabel || '正在加载，请稍等'); return; }
    state.workdirDialogOpen = true;
    state.workdirLoading = true;
    renderWorkdirDialog();
    await loadWorkdirPresets();
    state.workdirLoading = false;
    renderWorkdirDialog();
  }

  function closeWorkdirDialog() {
    state.workdirDialogOpen = false;
    renderWorkdirDialog();
  }

  async function loadWorkdirPresets() {
    try {
      const r = await fetch('/api/codex-agent/workdirs/presets');
      const d = await r.json();
      state.workdirPresets = Array.isArray(d.presets) ? d.presets : [];
      if (!state.workdirPresetRoot && state.workdirPresets[0]) {
        state.workdirPresetRoot = state.workdirPresets[0];
        await loadWorkdirChildren(state.workdirPresetRoot);
      }
    } catch (e) {
      console.error('loadWorkdirPresets failed', e);
      state.workdirPresets = [];
    }
  }

  async function loadWorkdirChildren(root) {
    state.workdirPresetRoot = safeStr(root);
    state.workdirChildren = [];
    if (!state.workdirPresetRoot) return;
    try {
      const r = await fetch('/api/codex-agent/workdirs/children?root=' + encodeURIComponent(state.workdirPresetRoot));
      const d = await r.json();
      state.workdirChildren = Array.isArray(d.children) ? d.children : [];
    } catch (e) {
      console.error('loadWorkdirChildren failed', e);
      showHint('读取预设子目录失败');
    }
  }

  function renderWorkdirDialog() {
    const dialog = $('#cm-workdir-dialog');
    if (!dialog) return;
    if (!state.workdirDialogOpen) {
      dialog.classList.remove('cm-workdir-dialog-open');
      dialog.innerHTML = '';
      return;
    }
    dialog.classList.add('cm-workdir-dialog-open');
    dialog.innerHTML = `
      <div class="cm-dialog-backdrop" id="cm-workdir-close-backdrop"></div>
      <div class="cm-dialog-card">
        <div class="cm-dialog-head">
          <strong>添加工作目录</strong>
          <button class="cm-dialog-close" id="cm-workdir-close">×</button>
        </div>
        <label class="cm-dialog-label">绝对路径</label>
        <div class="cm-dialog-row">
          <input class="cm-dialog-input" id="cm-workdir-manual" placeholder="/Users/you/project" value="">
          <button class="cm-dialog-btn" id="cm-workdir-use-manual">使用</button>
        </div>
        <div class="cm-dialog-label">预设工作目录</div>
        ${state.workdirLoading ? '<div class="cm-dialog-muted">正在加载预设…</div>' : `
          <select class="cm-dialog-select" id="cm-workdir-preset">
            <option value="">选择预设根目录</option>
            ${state.workdirPresets.map(path => `<option value="${escapeAttr(path)}" ${path === state.workdirPresetRoot ? 'selected' : ''}>${escapeHtml(path)}</option>`).join('')}
          </select>
          <select class="cm-dialog-select" id="cm-workdir-child" ${state.workdirChildren.length ? '' : 'disabled'}>
            <option value="">选择直接子目录</option>
            ${state.workdirChildren.map(item => `<option value="${escapeAttr(item.path)}">${escapeHtml(item.name || item.path)}</option>`).join('')}
          </select>
        `}
        <div class="cm-dialog-row cm-dialog-row-stack">
          <input class="cm-dialog-input" id="cm-workdir-preset-input" placeholder="添加一个预设根目录">
          <button class="cm-dialog-btn" id="cm-workdir-add-preset">添加预设路径</button>
          <button class="cm-dialog-btn cm-dialog-btn-muted" id="cm-workdir-delete-preset" ${state.workdirPresetRoot ? '' : 'disabled'}>删除预设路径</button>
        </div>
      </div>
    `;
    $('#cm-workdir-close')?.addEventListener('click', closeWorkdirDialog);
    $('#cm-workdir-close-backdrop')?.addEventListener('click', closeWorkdirDialog);
    $('#cm-workdir-use-manual')?.addEventListener('click', () => {
      const input = $('#cm-workdir-manual');
      const path = safeStr(input?.value).trim();
      if (!path) { showHint('请输入绝对路径'); return; }
      if (!path.startsWith('/')) { showHint('工作目录需要填写服务器上的绝对路径'); return; }
      selectProjectFromDialog(path);
    });
    $('#cm-workdir-preset')?.addEventListener('change', async (e) => {
      await loadWorkdirChildren(e.target.value);
      renderWorkdirDialog();
    });
    $('#cm-workdir-child')?.addEventListener('change', (e) => {
      const path = safeStr(e.target.value).trim();
      if (path) selectProjectFromDialog(path);
    });
    $('#cm-workdir-add-preset')?.addEventListener('click', addWorkdirPreset);
    $('#cm-workdir-delete-preset')?.addEventListener('click', deleteWorkdirPreset);
  }

  async function selectProjectFromDialog(path) {
    closeWorkdirDialog();
    state.projPopOpen = false;
    renderProjPop();
    await selectProject(path, { force: false });
  }

  async function addWorkdirPreset() {
    const path = safeStr($('#cm-workdir-preset-input')?.value).trim();
    if (!path) { showHint('请输入预设根目录'); return; }
    if (!path.startsWith('/')) { showHint('预设路径需要填写服务器上的绝对路径'); return; }
    try {
      const r = await fetch('/api/codex-agent/workdirs/presets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || '添加预设失败');
      state.workdirPresets = Array.isArray(d.presets) ? d.presets : [];
      state.workdirPresetRoot = state.workdirPresets.includes(path) ? path : state.workdirPresets[state.workdirPresets.length - 1] || '';
      await loadWorkdirChildren(state.workdirPresetRoot);
      renderWorkdirDialog();
    } catch (e) {
      alert('添加预设路径失败：' + e.message);
    }
  }

  async function deleteWorkdirPreset() {
    if (!state.workdirPresetRoot) return;
    try {
      const r = await fetch('/api/codex-agent/workdirs/presets', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: state.workdirPresetRoot }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || '删除预设失败');
      state.workdirPresets = Array.isArray(d.presets) ? d.presets : [];
      state.workdirPresetRoot = state.workdirPresets[0] || '';
      await loadWorkdirChildren(state.workdirPresetRoot);
      renderWorkdirDialog();
    } catch (e) {
      alert('删除预设路径失败：' + e.message);
    }
  }

  async function hideProjectForCanvas(dir) {
    const canvasId = currentCanvasId();
    if (!canvasId) return;
    try {
      const r = await fetch('/api/codex-agent/history/project-visibility', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ canvas_id: canvasId, project_dir: safeStr(dir), hidden: true }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || '隐藏目录失败');
      await loadProjects();
    } catch (e) {
      alert('隐藏目录失败：' + e.message);
    }
  }

  async function unhideProjectForCanvas(dir) {
    const canvasId = currentCanvasId();
    if (!canvasId) return;
    try {
      await fetch('/api/codex-agent/history/project-visibility', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ canvas_id: canvasId, project_dir: safeStr(dir), hidden: false }),
      });
    } catch (e) {
      console.warn('unhideProjectForCanvas failed', e);
    }
  }

  // ---------------- 选项目 / 新对话 ----------------
  async function selectProject(dir, options = {}) {
    if (!beginPanelOperation('opening_project', '正在打开项目', { force: options.force, status: 'opening_project' })) return;
    const projectDir = safeStr(dir);
    try {
      let threadId = options.conversationId ? safeStr(options.threadId || '') : '';
      let restoredConversation = null;
      if (options.conversationId) {
        showConversationRecoveryPlaceholder('恢复对话中');
        restoredConversation = await fetchHistoryConversation(options.conversationId);
        if (restoredConversation) {
          threadId = safeStr(restoredConversation.threadId || restoredConversation.thread_id || threadId);
        }
      } else {
        state.conversationId = '';
        state.threadId = threadId;
      }
      let restoredApplied = false;
      await openExecutionThread(projectDir, threadId, () => {
        if (!restoredConversation) return;
        applyPanelStateSnapshot(restoredConversation, { silent: true });
        restoredApplied = true;
      });
      if (restoredConversation && !restoredApplied) {
        applyPanelStateSnapshot(restoredConversation, { silent: true });
        state.conversationId = safeStr(restoredConversation.conversationId || restoredConversation.conversation_id || options.conversationId);
      }
      state.workdirReady = true;
      await unhideProjectForCanvas(projectDir);
      updateBusy('正在加载历史');
      await loadSessions(projectDir);
      if (!options.conversationId && state.sessions.length > 0 && state.sessions[0].id) {
        const restored = await fetchHistoryConversation(state.sessions[0].id);
        if (restored) applyPanelStateSnapshot(restored, { silent: true });
      } else if (!options.conversationId) {
        state.conversationId = '';
        state.messages = [];
        currentBackendTaskId = '';
        currentBackendTaskOffset = 0;
        renderBody();
      }
      await loadAllSessions();
      await recoverBackendTurn({ force: true, keepStatus: true });
      endPanelOperation('ready');
      savePanelSnapshot({ immediate: true });
    } catch (e) {
      console.error('selectProject failed', e);
      if (!options.silent) alert('启动 Codex 失败：' + e.message);
      endPanelOperation('error');
    }
    renderProjPop();
  }

  async function onNewSession() {
    if (!hasWorkdirSelection()) { alert('先选工作目录，或选择“无目录”'); return; }
    if (!beginPanelOperation('opening_project', '正在新建会话', { status: 'opening_project' })) return;
    try {
      await fetch('/api/codex-agent/board/close', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_dir: state.projectDir,
          canvas_id: currentCanvasId(),
          conversation_id: state.conversationId || '',
          thread_id: state.threadId || '',
        }),
      });
    } catch {}
    try {
      state.conversationId = '';
      state.threadId = '';
      await openProjectSession(state.projectDir, '');
      state.workdirReady = true;
      state.messages = [];
      renderBody();
      await loadSessions(state.projectDir);
      await loadAllSessions();
      endPanelOperation('ready');
      savePanelSnapshot({ immediate: true });
    } catch (e) {
      alert('新对话失败：' + e.message);
      endPanelOperation('error');
    }
  }

  // ---------------- 历史下拉 ----------------
  async function loadSessions(dir) {
    try {
      const qs = new URLSearchParams();
      qs.set('project_dir', safeStr(dir));
      const canvasId = currentCanvasId();
      if (canvasId) qs.set('canvas_id', canvasId);
      const r = await fetch('/api/codex-agent/history/conversations?' + qs.toString());
      const d = await r.json();
      state.sessions = Array.isArray(d.conversations) ? d.conversations : [];
    } catch (e) { console.error('loadSessions failed', e); state.sessions = []; }
  }

  async function loadAllSessions() {
    try {
      const qs = new URLSearchParams();
      const canvasId = currentCanvasId();
      if (canvasId) qs.set('canvas_id', canvasId);
      const r = await fetch('/api/codex-agent/history/conversations' + (qs.toString() ? `?${qs}` : ''));
      const d = await r.json();
      state.histAllSessions = Array.isArray(d.conversations) ? d.conversations : [];
    } catch (e) { console.error('loadAllSessions failed', e); state.histAllSessions = []; }
  }

  async function refreshHistoryLists(options = {}) {
    if (!currentCanvasId()) return;
    try {
      await Promise.all([
        loadProjects({ silent: true, noAutoSelect: true }),
        hasWorkdirSelection() ? loadSessions(state.projectDir) : Promise.resolve(),
        loadAllSessions(),
      ]);
      if (state.histPopOpen) renderHistPop();
      if (state.projPopOpen) renderProjPop();
    } catch (e) {
      if (!options.quiet) console.warn('refreshHistoryLists failed', e);
    }
  }

  function timeValue(value) {
    if (typeof value === 'number') return Number.isFinite(value) ? value : 0;
    const raw = safeStr(value).trim();
    if (!raw) return 0;
    if (/^\d+$/.test(raw)) return Number(raw);
    const parsed = Date.parse(raw);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function sortByHistoryPreference(items, preference) {
    const field = preference === 'created' ? 'created_at' : 'updated_at';
    return [...(Array.isArray(items) ? items : [])].sort((a, b) => {
      const aTime = timeValue(a?.[field] || (preference === 'created' ? a?.createdAt : a?.last_active || a?.started_at));
      const bTime = timeValue(b?.[field] || (preference === 'created' ? b?.createdAt : b?.last_active || b?.started_at));
      return bTime - aTime;
    });
  }

  function sortProjectsInPlace(projects) {
    const sorted = sortByHistoryPreference(projects, state.projectSort);
    projects.splice(0, projects.length, ...sorted);
  }

  function renderSortMenu(target) {
    const preference = target === 'project' ? state.projectSort : state.historySort;
    const open = target === 'project' ? state.projectSortMenuOpen : state.histSortMenuOpen;
    if (!open) return '';
    return `<div class="cm-sort-menu" role="menu">
      <button type="button" data-sort-target="${target}" data-sort="created" class="${preference === 'created' ? 'cm-sort-selected' : ''}">按添加时间排序</button>
      <button type="button" data-sort-target="${target}" data-sort="last_used" class="${preference === 'last_used' ? 'cm-sort-selected' : ''}">按最新使用时间排序</button>
    </div>`;
  }

  function bindSortMenu(container, target) {
    container.querySelectorAll(`[data-sort-target="${target}"]`).forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        const preference = btn.getAttribute('data-sort') === 'created' ? 'created' : 'last_used';
        if (target === 'project') {
          state.projectSort = preference;
          state.projectSortMenuOpen = false;
          sortProjectsInPlace(state.projects);
          renderProjPop();
        } else {
          state.historySort = preference;
          state.histSortMenuOpen = false;
          renderHistPop();
        }
      });
    });
  }

  function toggleHistPop() {
    if (!hasWorkdirSelection()) { alert('先选工作目录，或选择“无目录”'); return; }
    if (isLockedStatus()) { showHint(state.busyLabel || '正在加载，请稍等'); return; }
    state.histPopOpen = !state.histPopOpen;
    state.projPopOpen = false;
    state.mentionOpen = false;
    state.commandOpen = false;
    state.histSortMenuOpen = false;
    renderProjPop();
    closeMentionPicker();
    closeCommandPicker();
    if (state.histPopOpen) loadAllSessions().finally(renderHistPop);
    renderHistPop();
  }

  function renderHistPop() {
    const pop = $('#cm-hist-pop');
    if (!state.histPopOpen) { pop.classList.remove('cm-popover-open'); return; }
    pop.classList.add('cm-popover-open');
    const currentActive = state.histMode !== 'all';
    const html = [`
      <div class="cm-hist-tabs">
        <button class="cm-hist-tab-icon${currentActive ? ' cm-hist-tab-active' : ''}" data-mode="current" title="当前工作目录" aria-label="当前工作目录">${icon('folder-open')}</button>
        <button class="cm-hist-tab-icon${!currentActive ? ' cm-hist-tab-active' : ''}" data-mode="all" title="全部工作目录" aria-label="全部工作目录">${icon('folders')}</button>
        <button class="cm-hist-tab-icon${state.histSortMenuOpen ? ' cm-hist-tab-active' : ''}" id="cm-history-sort" title="排序：${state.historySort === 'created' ? '添加时间' : '最新使用时间'}" aria-label="排序">${icon('arrow-up-down')}</button>
        ${renderSortMenu('history')}
      </div>
    `];
    if (currentActive) {
      const sessions = sortByHistoryPreference(state.sessions, state.historySort);
      if (sessions.length === 0) {
        html.push('<div class="cm-popover-empty">当前工作目录暂无历史会话</div>');
      } else {
        html.push(sessions.map(s => renderSessionItem(s)).join(''));
      }
    } else {
      const grouped = groupSessionsByProject(sortByHistoryPreference(state.histAllSessions, state.historySort));
      if (!grouped.length) {
        html.push('<div class="cm-popover-empty">当前画布暂无历史会话</div>');
      } else {
        grouped.forEach(group => {
          const key = group.projectDir;
          const expanded = state.histExpandedDirs[key] !== false;
          html.push(`
            <div class="cm-hist-group">
              <button class="cm-hist-group-head" data-dir="${escapeAttr(key)}">
                <span>${expanded ? '▾' : '▸'} ${escapeHtml(workdirMetaLabel(key))}</span>
                <em>${group.items.length}</em>
              </button>
              ${expanded ? group.items.map(s => renderSessionItem(s, true)).join('') : ''}
            </div>
          `);
        });
      }
    }
    pop.innerHTML = html.join('');
    pop.querySelectorAll('.cm-hist-tabs button').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        const mode = btn.getAttribute('data-mode');
        if (!mode) return;
        state.histMode = mode === 'all' ? 'all' : 'current';
        renderHistPop();
      });
    });
    $('#cm-history-sort')?.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      state.histSortMenuOpen = !state.histSortMenuOpen;
      renderHistPop();
    });
    bindSortMenu(pop, 'history');
    refreshPanelIcons();
    pop.querySelectorAll('.cm-hist-group-head').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        const key = btn.getAttribute('data-dir') || '';
        state.histExpandedDirs[key] = state.histExpandedDirs[key] === false ? true : false;
        renderHistPop();
      });
    });
    pop.querySelectorAll('.cm-popover-item').forEach(el => {
      el.addEventListener('click', () => {
        const sid = el.getAttribute('data-sid');
        const cid = el.getAttribute('data-cid');
        const dir = el.getAttribute('data-dir') || '';
        state.histPopOpen = false;
        renderHistPop();
        switchSessionFromHistory({ sessionId: sid, conversationId: cid, projectDir: dir });
      });
    });
  }

  function renderSessionItem(s, showDir = false) {
    const dir = safeStr(s.project_dir || s.cwd);
    return `
      <div class="cm-popover-item" data-sid="${escapeAttr(s.session_id)}" data-cid="${escapeAttr(s.id)}" data-dir="${escapeAttr(dir)}">
        💬 ${escapeHtml(sessionPreviewTitle(s))}
        <span class="cm-popover-meta">${showDir ? escapeHtml(workdirMetaLabel(dir)) + ' · ' : ''}${s.started_at || ''} · ${s.model || '?'}</span>
      </div>
    `;
  }

  function groupSessionsByProject(sessions) {
    const map = new Map();
    (Array.isArray(sessions) ? sessions : []).forEach(session => {
      const key = safeStr(session.project_dir || session.cwd);
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(session);
    });
    return Array.from(map.entries()).map(([projectDir, items]) => ({ projectDir, items }));
  }

  async function switchSessionFromHistory(item) {
    const dir = safeStr(item.projectDir);
    if (dir !== state.projectDir || !hasWorkdirSelection()) {
      await selectProject(dir, { threadId: item.sessionId, conversationId: item.conversationId });
      return;
    }
    await switchSession(item.sessionId, { conversationId: item.conversationId });
  }

  function sessionPreviewTitle(session) {
    const text = safeStr(session?.preview).trim();
    if (text && text !== '(空)' && !/^(已完成|完成|正在运行|后台 Agent 任务)$/.test(text)) return text;
    const title = safeStr(session?.title).trim();
    if (title && title !== '(空)' && !/^(已完成|完成|后台 Agent 任务|画布 Agent 对话)$/.test(title)) return title;
    const media = safeStr(session?.preview_media || session?.media_preview || session?.first_media || '').trim();
    if (media) return media;
    return '含图片/附件的对话';
  }

  async function switchSession(sessionId, options = {}) {
    if (!sessionId && !options.conversationId) return;
    if (!beginPanelOperation('loading_history', '正在恢复会话', { status: 'loading_history' })) return;
    try {
      let threadId = safeStr(sessionId || '');
      let restored = null;
      if (options.conversationId) {
        showConversationRecoveryPlaceholder('恢复对话中');
        restored = await fetchHistoryConversation(options.conversationId);
        if (restored) {
          threadId = safeStr(restored.threadId || restored.thread_id || threadId);
        }
      }
      let restoredApplied = false;
      await openExecutionThread(state.projectDir, threadId, () => {
        if (!restored) return;
        applyPanelStateSnapshot(restored, { silent: true });
        restoredApplied = true;
      });
      if (restored && !restoredApplied) {
        applyPanelStateSnapshot(restored, { silent: true });
        state.conversationId = safeStr(restored.conversationId || restored.conversation_id || options.conversationId);
      }
      state.workdirReady = true;
      updateBusy('正在加载历史');
      await loadSessions(state.projectDir);
      await loadAllSessions();
      await recoverBackendTurn({ force: true, keepStatus: true });
      endPanelOperation('ready');
      savePanelSnapshot({ immediate: true });
    } catch (e) {
      console.error('switchSession failed', e);
      alert('恢复会话失败：' + e.message);
      endPanelOperation('error');
    }
  }

  function showConversationRecoveryPlaceholder(label = '恢复对话中') {
    state.messages = [];
    currentBackendTaskId = '';
    currentBackendTaskOffset = 0;
    setRecovering('loading_history', label);
    renderBody({ preserveScroll: false });
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
    if (isLockedStatus()) { showHint(state.busyLabel || '正在加载，请稍等'); return; }
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
      const url = normalizeAttachmentUrl(item.url || item.path || item.src);
      const normalized = { ...item, url, refId: `ref_${state.attachments.length + 1}` };
      const key = attachmentIdentity(normalized);
      if (!key) return false;
      const existed = state.attachments.find(a => attachmentIdentity(a) === key);
      if (existed) return false;
      state.attachments.push(normalized);
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
    return normalizeAttachmentUrl(pathOrUrl);
  }

  function isLocalHostName(hostname) {
    const h = safeStr(hostname).toLowerCase();
    return h === 'localhost' || h === '127.0.0.1' || h === '::1' || h === '[::1]';
  }

  function isAppRelativeMediaPath(pathname) {
    const path = safeStr(pathname);
    return path.startsWith('/assets/')
      || path.startsWith('/output/')
      || path === '/api/view'
      || path === '/api/codex-agent/file/view';
  }

  function normalizeAttachmentUrl(value) {
    const raw = safeStr(value).trim();
    if (!raw) return '';
    if (raw.startsWith('data:') || raw.startsWith('blob:')) return raw;
    if (raw.startsWith('file://')) {
      let localPath = raw.slice('file://'.length);
      try { localPath = decodeURIComponent(localPath); } catch {}
      return '/api/codex-agent/file/view?path=' + encodeURIComponent(localPath);
    }
    if (/^https?:\/\//i.test(raw)) {
      try {
        const u = new URL(raw, window.location.href);
        if (u.origin === window.location.origin || isLocalHostName(u.hostname)) {
          if (isAppRelativeMediaPath(u.pathname)) return `${u.pathname}${u.search || ''}${u.hash || ''}`;
        }
      } catch {}
      return raw;
    }
    if (raw.startsWith('/')) {
      if (isAppRelativeMediaPath(raw.split('?', 1)[0])) return raw;
      return '/api/codex-agent/file/view?path=' + encodeURIComponent(raw);
    }
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

  function attachmentIdentity(item) {
    const data = item && typeof item === 'object' ? item : {};
    const nodeId = safeStr(data.nodeId || data.node_id);
    const kind = safeStr(data.kind || '');
    const imageIndex = data.imageIndex ?? data.image_index ?? '';
    const url = normalizeAttachmentUrl(data.url || data.path || data.src);
    if (nodeId && kind === 'prompt') return `prompt:${nodeId}`;
    if (nodeId && url) return `node:${nodeId}:${imageIndex}:${url}`;
    return url ? `url:${url}` : '';
  }

  function selectedNodeCount() {
    const native = window.SmartCanvasAgentApi;
    if (typeof native?.getSelectedNodeCount === 'function') return Number(native.getSelectedNodeCount() || 0);
    const context = native?.getContext?.();
    return Array.isArray(context?.selectedNodeIds) ? context.selectedNodeIds.length : CanvasAgentBridge.getSelectedAssets().length;
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
    if (!state.attachments.length) {
      list.hidden = true;
      list.innerHTML = '';
      savePanelSnapshot();
      return;
    }
    list.hidden = false;
    const itemHtml = state.attachments.map((a, i) => {
      const kind = safeStr(a.kind || mediaKindFromUrl(a.url));
      const preview = attachmentPreviewHtml(a, kind, 'cm-attach-kind');
      const promptTip = kind === 'prompt' ? `<span class="cm-ref-prompt-tip"><b>${escapeHtml(safeStr(a.nodeTitle || a.name || '提示词节点'))}</b><span>${escapeHtml(safeStr(a.text || a.prompt || ''))}</span></span>` : '';
      return `<div class="cm-attach-thumb" data-i="${i}" data-node-id="${escapeAttr(a.nodeId || a.node_id || '')}" draggable="true" title="引用 ${i + 1} · ${escapeAttr(a.name || a.nodeTitle || kind)}">
        ${preview}
        <span class="cm-attach-index">${i + 1}</span>
        <span class="cm-attach-x" data-i="${i}" title="移除">×</span>
        ${promptTip}
      </div>`;
    }).join('');
    list.innerHTML = `<div class="cm-ref-scroll">${itemHtml}</div>
      <div class="cm-ref-actions">
        <button class="cm-ref-action" type="button" data-ref-action="clear" title="清空引用">${icon('trash-2')}</button>
      </div>`;
    list.querySelector('[data-ref-action="clear"]')?.addEventListener('click', () => {
      state.attachments = [];
      renderAttach();
    });
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
    list.querySelectorAll('.cm-attach-thumb').forEach(thumb => {
      thumb.addEventListener('mouseenter', () => {
        const tip = thumb.querySelector('.cm-ref-prompt-tip');
        if (!tip) return;
        const rect = thumb.getBoundingClientRect();
        tip.style.left = `${Math.min(window.innerWidth - 205, rect.right + 7)}px`;
        tip.style.top = `${Math.min(window.innerHeight - 76, rect.top)}px`;
      });
    });
    list.addEventListener('dblclick', event => {
      const thumb = event.target.closest('.cm-attach-thumb');
      if (!thumb || !list.contains(thumb)) return;
      event.preventDefault();
      event.stopPropagation();
      const nodeId = safeStr(thumb.dataset.nodeId);
      const focused = nodeId ? window.SmartCanvasAgentApi?.focusNodes?.([nodeId]) : false;
      showHint(focused ? '已定位引用节点' : '未能定位引用节点');
    });
    refreshPanelIcons();
    savePanelSnapshot();
  }

  function attachmentPreviewHtml(item, kind = '', fallbackClass = 'cm-attach-kind') {
    const mediaKind = safeStr(kind || item.kind || mediaKindFromUrl(item.url));
    const url = escapeAttr(normalizeAttachmentUrl(item.url || item.path || item.src));
    const name = escapeAttr(item.name);
    if (mediaKind === 'image') {
      return `<img src="${url}" alt="${name}" loading="lazy">`;
    }
    if (mediaKind === 'video') {
      return `<video src="${url}" muted playsinline preload="metadata"></video><span class="cm-video-badge">▶</span>`;
    }
    if (mediaKind === 'prompt') {
      return `<span class="${fallbackClass} cm-attach-prompt-icon">${icon('scroll-text')}</span>`;
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

  function selectedLabel(list, id) {
    return list.find(item => item.id === id)?.label || list[0]?.label || '';
  }

  function cycleInputSetting(key, list) {
    if (isLockedStatus()) { showHint(state.busyLabel || '正在加载，请稍等'); return; }
    const current = list.findIndex(item => item.id === state[key]);
    const next = list[(current + 1 + list.length) % list.length];
    state[key] = next.id;
    setStatusUI();
    savePanelSnapshot({ immediate: true });
  }

  function openMentionShortcut() {
    if (isLockedStatus()) { showHint(state.busyLabel || '正在加载，请稍等'); return; }
    const input = $('#cm-input');
    input?.focus();
    if (!inputCaretPrefix().match(/@([^\s@]*)$/)) insertPlainText('@');
    onInputAssist();
  }

  function openCommandShortcut() {
    if (isLockedStatus()) { showHint(state.busyLabel || '正在加载，请稍等'); return; }
    const input = $('#cm-input');
    input?.focus();
    if (!inputCaretPrefix().match(/\/([^\s/]*)$/)) insertPlainText('/');
    onInputAssist();
  }

  // ---------------- 发消息 → SSE ----------------
  function onInputKey(e) {
    if (isRecoveringStatus()) {
      if (e.key === 'Enter') e.preventDefault();
      return;
    }
    if (state.mentionOpen || state.commandOpen) {
      if (e.key === 'Escape') { e.preventDefault(); closeAssistPickers(); return; }
      if (e.key === 'Enter' && !e.shiftKey) {
        if (state.mentionOpen) {
          const first = state.mentionItems[0];
          if (first) { e.preventDefault(); pickMentionItem(first); return; }
        }
        if (state.commandOpen) {
          const first = state.commandItems[0];
          if (first) { e.preventDefault(); pickCommandItem(first); return; }
        }
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); onSend(); }
  }

  async function onInputPaste(e) {
    if (isLockedStatus()) {
      e.preventDefault();
      e.stopPropagation();
      showHint(state.busyLabel || '正在加载，请稍等');
      return;
    }
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
      onInputAssist();
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

  function onInputAssist() {
    const before = inputCaretPrefix();
    const mentionMatch = before.match(/@([^\s@]*)$/);
    if (mentionMatch) {
      openMentionPickerFromMatch(mentionMatch, before);
      closeCommandPicker();
      return;
    }
    const commandMatch = before.match(/\/([^\s/]*)$/);
    if (commandMatch) {
      openCommandPickerFromMatch(commandMatch);
      closeMentionPicker();
      return;
    }
    closeAssistPickers();
  }

  function openMentionPickerFromMatch(match, before) {
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

  function openCommandPickerFromMatch(match) {
    state.commandQuery = safeStr(match[1]).toLowerCase();
    state.commandItems = slashCommands
      .filter(item => !state.commandQuery || `${item.label} ${item.desc}`.toLowerCase().includes(state.commandQuery))
      .slice(0, 10);
    renderCommandPicker();
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

  function renderCommandPicker() {
    const pop = $('#cm-command-pop');
    if (!pop) return;
    state.commandOpen = true;
    pop.classList.add('cm-command-open');
    if (!state.commandItems.length) {
      pop.innerHTML = '<div class="cm-command-empty">没有匹配的命令</div>';
      return;
    }
    pop.innerHTML = state.commandItems.map((item, i) => `
      <button class="cm-command-item" type="button" data-i="${i}">
        <b>${escapeHtml(item.label)}</b>
        <span>${escapeHtml(item.desc)}</span>
      </button>
    `).join('');
    pop.querySelectorAll('.cm-command-item').forEach(btn => {
      btn.addEventListener('mousedown', e => e.preventDefault());
      btn.addEventListener('click', () => {
        const item = state.commandItems[Number(btn.getAttribute('data-i'))];
        if (item) pickCommandItem(item);
      });
    });
  }

  function pickCommandItem(item) {
    const input = $('#cm-input');
    input?.focus();
    deleteCharsBeforeCaret(state.commandQuery.length + 1);
    insertPlainText(item.insert || item.label + ' ');
    closeCommandPicker();
    input?.focus();
  }

  function closeCommandPicker() {
    state.commandOpen = false;
    state.commandItems = [];
    state.commandQuery = '';
    const pop = $('#cm-command-pop');
    if (pop) {
      pop.classList.remove('cm-command-open');
      pop.innerHTML = '';
    }
  }

  function closeAssistPickers() {
    closeMentionPicker();
    closeCommandPicker();
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

  function currentCanvasId() {
    const ctx = CanvasAgentBridge.getContext();
    return safeStr(ctx?.native?.canvasId || ctx?.native?.canvas_id || new URLSearchParams(location.search).get('id') || '');
  }

  function panelSnapshotKey() {
    const id = currentCanvasId() || `${location.pathname}${location.search}`;
    return panelStatePrefix + id;
  }

  function compactMessagesForSnapshot(messages) {
    return cleanPanelMessages(messages).slice(-80).map(msg => ({
      role: msg.role === 'user' ? 'user' : 'bot',
      blocks: (Array.isArray(msg.blocks) ? msg.blocks : []).map(block => {
        const copy = { ...block };
        if ((copy.type === 'attach' || copy.type === 'attachment' || copy.type === 'media_preview') && Array.isArray(copy.items)) {
          copy.items = copy.items.map(normalizeAttachmentItem);
        }
        if (copy.streaming) {
          copy.streaming = false;
          copy.stale = true;
        }
        if (copy.status === 'running') copy.status = 'stale';
        if (typeof copy.text === 'string' && copy.text.length > 20000) copy.text = copy.text.slice(0, 20000) + '\n...';
        return copy;
      }),
    }));
  }

  function normalizeAttachmentItem(item) {
    if (!item || typeof item !== 'object') return item;
    return {
      ...item,
      url: normalizeAttachmentUrl(item.url || item.path || item.src),
    };
  }

  function currentPanelPayload() {
    const body = $('#cm-body');
    if (body) state.scrollTop = Math.round(body.scrollTop || 0);
    const messages = compactMessagesForSnapshot(state.messages);
    const hasConversationContent = messages.length > 0;
    return {
      projectDir: state.projectDir || '',
      project_dir: state.projectDir || '',
      workdirReady: Boolean(state.workdirReady),
      workdir_ready: Boolean(state.workdirReady),
      threadId: hasConversationContent ? (state.threadId || '') : '',
      thread_id: hasConversationContent ? (state.threadId || '') : '',
      conversationId: hasConversationContent ? (state.conversationId || '') : '',
      conversation_id: hasConversationContent ? (state.conversationId || '') : '',
      canvasTitle: document.title || '',
      canvas_title: document.title || '',
      status: state.status || 'ready',
      open: Boolean(state.open),
      canvasId: currentCanvasId(),
      canvas_id: currentCanvasId(),
      messages,
      attachments: (state.attachments || []).map(normalizeAttachmentItem),
      taskId: currentBackendTaskId || '',
      task_id: currentBackendTaskId || '',
      taskOffset: currentBackendTaskOffset || 0,
      task_offset: currentBackendTaskOffset || 0,
      scrollTop: state.scrollTop || 0,
      scroll_top: state.scrollTop || 0,
      inputMode: state.inputMode,
      input_mode: state.inputMode,
      inputScope: state.inputScope,
      input_scope: state.inputScope,
      approvalPolicy: state.approvalPolicy,
      approval_policy: state.approvalPolicy,
      savedAt: Date.now(),
    };
  }

  function savePanelSnapshot(options = {}) {
    if (suppressPanelSave) return;
    try {
      const hasState = Boolean(
        state.projectDir ||
        state.workdirReady ||
        state.threadId ||
        currentBackendTaskId ||
        state.messages.length ||
        state.attachments.length
      );
      if (!hasState) return;
      const payload = currentPanelPayload();
      localStorage.setItem(panelSnapshotKey(), JSON.stringify(payload));
      if (state.projectDir || state.threadId) {
        localStorage.setItem(panelRecentStateKey, JSON.stringify(payload));
      }
      schedulePanelStateSave(options.immediate ? 0 : 650);
    } catch {}
  }

  function restorePanelSnapshot() {
    let snap = null;
    try {
      snap = JSON.parse(localStorage.getItem(panelSnapshotKey()) || 'null');
      if (!snap || typeof snap !== 'object' || (!snap.projectDir && !snap.threadId && !snap.messages?.length)) {
        snap = JSON.parse(localStorage.getItem(panelRecentStateKey) || 'null');
      }
    } catch {}
    if (!snap || typeof snap !== 'object') return;
    state.projectDir = safeStr(snap.projectDir);
    state.workdirReady = Boolean(snap.workdirReady || snap.workdir_ready || snap.projectDir || snap.threadId || snap.messages?.length);
    state.threadId = safeStr(snap.threadId);
    state.conversationId = safeStr(snap.conversationId || snap.conversation_id);
    state.messages = cleanPanelMessages(snap.messages);
    state.attachments = Array.isArray(snap.attachments) ? snap.attachments.map(normalizeAttachmentItem) : [];
    currentBackendTaskId = safeStr(snap.taskId);
    currentBackendTaskOffset = Number(snap.taskOffset || 0) || 0;
    state.scrollTop = Number(snap.scrollTop || snap.scroll_top || 0) || 0;
    state.inputMode = 'auto';
    state.inputScope = 'auto';
    state.approvalPolicy = safeStr(snap.approvalPolicy || state.approvalPolicy);
    state.open = snap.open !== false;
    $('#cm-panel')?.classList.toggle('cm-open', state.open);
    suppressPanelSave = true;
    renderAttach();
    renderBody({ preserveScroll: true });
    suppressPanelSave = false;
  }

  function normalizePanelStatePayload(data) {
    const raw = data?.state || data || {};
    if (!raw || typeof raw !== 'object') return null;
    return {
      projectDir: safeStr(raw.projectDir || raw.project_dir),
      workdirReady: Boolean(raw.workdirReady || raw.workdir_ready || raw.projectDir || raw.project_dir !== undefined || raw.threadId || raw.thread_id || raw.conversationId || raw.conversation_id),
      threadId: safeStr(raw.threadId || raw.thread_id),
      conversationId: safeStr(raw.conversationId || raw.conversation_id),
      canvasId: safeStr(raw.canvasId || raw.canvas_id),
      open: raw.open !== false,
      messages: cleanPanelMessages(raw.messages),
      attachments: Array.isArray(raw.attachments) ? raw.attachments.map(normalizeAttachmentItem) : [],
      taskId: safeStr(raw.taskId || raw.task_id),
      taskOffset: Number(raw.taskOffset || raw.task_offset || 0) || 0,
      scrollTop: Number(raw.scrollTop || raw.scroll_top || 0) || 0,
      inputMode: safeStr(raw.inputMode || raw.input_mode),
      inputScope: safeStr(raw.inputScope || raw.input_scope),
      approvalPolicy: safeStr(raw.approvalPolicy || raw.approval_policy),
      updatedAt: Number(raw.updatedAt || raw.updated_at || 0) || 0,
    };
  }

  function applyPanelStateSnapshot(raw, options = {}) {
    const snap = normalizePanelStatePayload(raw);
    if (!snap) return false;
    state.projectDir = snap.projectDir;
    state.workdirReady = Boolean(snap.workdirReady || snap.projectDir || snap.threadId || snap.conversationId || snap.messages.length);
    if (snap.threadId) state.threadId = snap.threadId;
    if (snap.conversationId) state.conversationId = snap.conversationId;
    state.messages = snap.messages;
    state.attachments = snap.attachments.map(normalizeAttachmentItem);
    currentBackendTaskId = snap.taskId;
    currentBackendTaskOffset = snap.taskOffset;
    state.scrollTop = snap.scrollTop;
    state.inputMode = 'auto';
    state.inputScope = 'auto';
    if (snap.approvalPolicy) state.approvalPolicy = snap.approvalPolicy;
    state.open = snap.open !== false;
    $('#cm-panel')?.classList.toggle('cm-open', state.open);
    suppressPanelSave = Boolean(options.silent);
    renderAttach();
    renderBody({ preserveScroll: true });
    suppressPanelSave = false;
    setStatusUI();
    return true;
  }

  async function fetchPanelState(projectDir = state.projectDir, threadId = state.threadId) {
    const canvasId = currentCanvasId();
    if (!canvasId || (!state.workdirReady && !projectDir && !threadId)) return null;
    const qs = new URLSearchParams({ canvas_id: canvasId });
    if (state.workdirReady || projectDir) qs.set('project_dir', projectDir);
    if (threadId) qs.set('thread_id', threadId);
    const r = await fetch('/api/codex-agent/panel-state?' + qs.toString());
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.found) return null;
    return d.state || null;
  }

  async function fetchHistoryConversation(conversationId) {
    if (!conversationId) return null;
    const r = await fetch('/api/codex-agent/history/conversation?conversation_id=' + encodeURIComponent(conversationId));
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.found) return null;
    return d.state || null;
  }

  async function fetchLatestPanelState(projectDir = '') {
    const canvasId = currentCanvasId();
    if (!canvasId) return null;
    const qs = new URLSearchParams({ canvas_id: canvasId });
    if (state.workdirReady || projectDir) qs.set('project_dir', projectDir);
    const r = await fetch('/api/codex-agent/history/latest?' + qs.toString());
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.found) return null;
    return d.state || null;
  }

  function schedulePanelStateSave(delay = 650) {
    if (suppressPanelSave) return;
    if (!state.workdirReady && !state.projectDir && !state.threadId) return;
    if (!currentCanvasId()) return;
    if (panelStateSaveTimer) clearTimeout(panelStateSaveTimer);
    panelStateSaveTimer = setTimeout(savePanelStateToBackend, delay);
  }

  async function savePanelStateToBackend() {
    if (suppressPanelSave) return;
    const payload = currentPanelPayload();
    if (!payload.canvas_id || (!payload.project_dir && !payload.thread_id)) return;
    panelStateSaveInFlight = true;
    try {
      const r = await fetch('/api/codex-agent/panel-state', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const d = await r.json().catch(() => ({}));
      const saved = normalizePanelStatePayload(d);
      if (saved?.conversationId && !state.conversationId) {
        state.conversationId = saved.conversationId;
        try {
          const nextPayload = currentPanelPayload();
          localStorage.setItem(panelSnapshotKey(), JSON.stringify(nextPayload));
          localStorage.setItem(panelRecentStateKey, JSON.stringify(nextPayload));
        } catch {}
      }
      refreshHistoryLists({ quiet: true });
    } catch (e) {
      console.warn('save panel state failed', e);
    } finally {
      panelStateSaveInFlight = false;
    }
  }

  function wait(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  async function bootstrapPanelRestore() {
    if (restoreInFlight) return;
    restoreInFlight = true;
    const hadSnapshot = Boolean(state.projectDir || state.threadId || state.messages.length);
    try {
      setRecovering('booting', hadSnapshot ? '正在恢复上次对话' : '正在检查上次对话');
      let latest = null;
      try {
        latest = await fetchLatestPanelState(state.projectDir);
      } catch (e) {
        console.warn('latest panel state failed', e);
      }
      if (latest && (!state.workdirReady || !state.threadId || !state.messages.length)) {
        applyPanelStateSnapshot(latest, { silent: true });
      }
      await loadProjects();
      if (!state.workdirReady) {
        finishBusy('ready');
        restoreInFlight = false;
        return;
      }

      setRecovering('opening_project', '正在打开上次项目');
      await openExecutionThread(state.projectDir, state.threadId);
      state.workdirReady = true;

      setRecovering('loading_history', '正在加载会话列表');
      await loadSessions(state.projectDir);
      await loadAllSessions();

      const persisted = state.conversationId
        ? await fetchHistoryConversation(state.conversationId).catch(() => null)
        : await fetchLatestPanelState(state.projectDir).catch(() => null);
      if (persisted) {
        applyPanelStateSnapshot(persisted, { silent: true });
      }

      setRecovering('reconnecting_task', '正在接回后台任务');
      await recoverBackendTurn({ force: true, keepStatus: true });
      await savePanelStateToBackend();
      finishBusy('ready');
    } catch (e) {
      console.error('bootstrapPanelRestore failed', e);
      alertRecoveryFailure('恢复上次对话失败：' + safeStr(e.message || e));
      savePanelSnapshot({ immediate: true });
      await savePanelStateToBackend();
      finishBusy('error');
    } finally {
      restoreInFlight = false;
    }
  }

  async function openProjectSession(projectDir, threadId = '') {
    const r = await fetch('/api/codex-agent/board/open', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        project_dir: projectDir,
        thread_id: threadId || undefined,
        canvas_id: currentCanvasId(),
        conversation_id: state.conversationId || '',
      }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || 'board/open failed');
    state.projectDir = d.project_dir;
    state.workdirReady = true;
    state.threadId = d.thread_id;
    if (d.resume_warning) appendSystemNotice(safeStr(d.resume_warning));
    setStatusUI();
    return d;
  }

  async function openExecutionThread(projectDir, threadId = '', onRebuild = null) {
    try {
      return await openProjectSession(projectDir, threadId);
    } catch (e) {
      if (!threadId) throw e;
      if (typeof onRebuild === 'function') onRebuild();
      setRecovering('opening_project', '正在重建执行线程');
      return await openProjectSession(projectDir, '');
    }
  }

  function inferContextProfile(text, attachments = []) {
    const raw = safeStr(text).toLowerCase();
    const hasAttachments = Array.isArray(attachments) && attachments.length > 0;
    const mode = state.inputMode || 'auto';
    const scope = state.inputScope || 'auto';
    let intent = 'chat';
    let level = hasAttachments ? 2 : 0;
    const command = (raw.match(/(?:^|\s)(\/[^\s]+)/) || [])[1] || '';

    if (mode === 'chat') level = hasAttachments ? 2 : 0;
    if (mode === 'analyze') level = hasAttachments ? 2 : 1;
    if (mode === 'generate') level = 2;
    if (mode === 'organize') level = 2;
    if (scope === 'canvas') level = 3;
    if (scope === 'selected' || scope === 'node') level = Math.max(level, 2);

    if (/全画布|整个画布|所有节点|全部节点|总览|版图|整理全部|\/总结画布|\/批量处理/.test(raw)) {
      level = 3;
      intent = 'global_canvas';
    } else if (/当前|视口|眼前|这块|这片|左边|右边|上方|下方|附近|放到|移动|整理|重命名|分组|节点|画布|\/整理|\/重命名|\/定位/.test(raw)) {
      level = Math.max(level, 2);
      intent = 'canvas_operation';
    } else if (/生成|生图|视频|提示词|prompt|\/创建生图节点|\/创建视频节点|\/生成提示词/.test(raw)) {
      level = Math.max(level, 2);
      intent = 'generation';
    } else if (/这张图|图片|素材|分析|描述|读取|看一下/.test(raw) || hasAttachments) {
      level = Math.max(level, 2);
      intent = 'asset_analysis';
    } else if (mode === 'chat') {
      intent = 'chat';
    }

    return {
      level,
      label: contextLevelLabels[level] || '局部节点',
      intent,
      command,
      mode,
      scope,
      approvalPolicy: state.approvalPolicy,
      hasAttachments,
      attachmentCount: attachments.length,
    };
  }

  function nodeIntersectsVisible(node, visible, pad = 120) {
    if (!node || !visible) return false;
    const x = Number(node.x || 0);
    const y = Number(node.y || 0);
    const width = Math.max(1, Number(node.width || node.w || 260));
    const height = Math.max(1, Number(node.height || node.h || 220));
    return !(
      x + width + pad < Number(visible.x || 0) ||
      Number(visible.x || 0) + Number(visible.width || 0) + pad < x ||
      y + height + pad < Number(visible.y || 0) ||
      Number(visible.y || 0) + Number(visible.height || 0) + pad < y
    );
  }

  function compactNodeForContext(node) {
    if (!node || typeof node !== 'object') return null;
    const images = Array.isArray(node.images) ? node.images : [];
    return {
      id: safeStr(node.id),
      type: safeStr(node.type),
      title: safeStr(node.title).slice(0, 120),
      x: Math.round(Number(node.x || 0)),
      y: Math.round(Number(node.y || 0)),
      width: Math.round(Number(node.width || node.w || 0)),
      height: Math.round(Number(node.height || node.h || 0)),
      text: safeStr(node.text).slice(0, 300),
      images: images.slice(0, 6).map((img, index) => ({
        index,
        name: safeStr(img?.name).slice(0, 120),
        kind: safeStr(img?.kind || mediaKindFromUrl(img?.url || '')),
        url: safeStr(img?.url).slice(0, 500),
      })),
    };
  }

  function buildRoutedCanvasContext(text, attachments = []) {
    const base = CanvasAgentBridge.getContext() || {};
    const profile = inferContextProfile(text, attachments);
    const native = base.native && typeof base.native === 'object' ? base.native : {};
    const allNodes = Array.isArray(native.allNodes) ? native.allNodes : [];
    const selectedNodes = Array.isArray(native.selectedNodes) ? native.selectedNodes : [];
    const visible = native.visibleWorld || native.visible_world || null;
    const wantsGenerationContext = profile.intent === 'generation' || profile.mode === 'generate' || /生图|生成|视频|模型|provider|model/i.test(text);
    const attachmentNodeIds = new Set(
      (attachments || [])
        .map(item => safeStr(item.nodeId || item.node_id))
        .filter(Boolean)
    );
    let contextNodes = [];

    if (profile.level >= 3) {
      contextNodes = allNodes;
    } else if (profile.level === 2) {
      const byId = new Map();
      selectedNodes.forEach(node => { if (node?.id) byId.set(node.id, node); });
      allNodes.forEach(node => {
        if (attachmentNodeIds.has(safeStr(node?.id)) || nodeIntersectsVisible(node, visible, 160)) {
          if (node?.id) byId.set(node.id, node);
        }
      });
      contextNodes = Array.from(byId.values()).slice(0, 80);
    }

    const routedNative = {
      canvasId: native.canvasId || native.canvas_id || '',
      title: native.title || '',
      capturedAt: native.capturedAt || native.captured_at || Date.now(),
      visibleWorld: visible || null,
      selectedNodeIds: Array.isArray(native.selectedNodeIds) ? native.selectedNodeIds : [],
      selectedImage: native.selectedImage || null,
      selectedNodes: profile.level >= 2 ? selectedNodes.map(compactNodeForContext).filter(Boolean) : [],
      allNodes: profile.level >= 2 ? contextNodes.map(compactNodeForContext).filter(Boolean) : [],
      connections: profile.level >= 3 && Array.isArray(native.connections) ? native.connections.slice(0, 240) : [],
      imageGeneration: wantsGenerationContext ? native.imageGeneration || native.image_generation || null : null,
      videoGeneration: wantsGenerationContext ? native.videoGeneration || native.video_generation || null : null,
      nodeCounts: {
        total: allNodes.length,
        selected: selectedNodes.length,
        sent: contextNodes.length,
      },
    };

    return {
      ...base,
      selectedAssets: profile.level >= 2 ? base.selectedAssets || [] : [],
      selectedCount: profile.level >= 2 ? base.selectedCount || 0 : 0,
      native: routedNative,
      agentInput: {
        mode: 'auto',
        scope: 'auto',
        approvalPolicy: state.approvalPolicy,
      },
      contextProfile: profile,
    };
  }

  async function startBackendTurn(text, attachments, botMsg) {
    const canvasContext = buildRoutedCanvasContext(text, attachments);
    const profile = canvasContext.contextProfile || {};
    if (profile.label) updateBusy(`准备上下文：${profile.label}`);
    const body = JSON.stringify({
      project_dir: state.projectDir,
      text,
      attachments,
      canvas_context: canvasContext,
      canvas_id: currentCanvasId(),
      thread_id: state.threadId || '',
      conversation_id: state.conversationId || '',
    });
    let r = await fetch('/api/codex-agent/turn/background', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
    });
    let d = await r.json().catch(() => ({}));
    if (!r.ok && safeStr(d.detail).includes('session 未打开')) {
      await fetch('/api/codex-agent/board/open', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_dir: state.projectDir,
          thread_id: state.threadId || undefined,
          canvas_id: currentCanvasId(),
          conversation_id: state.conversationId || '',
        }),
      }).catch(() => null);
      r = await fetch('/api/codex-agent/turn/background', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
      });
      d = await r.json().catch(() => ({}));
    }
    if (!r.ok) throw new Error(safeStr(d.detail) || `HTTP ${r.status}`);
    if (d.thread_id) state.threadId = safeStr(d.thread_id);
    if (d.conversation_id && !state.conversationId) state.conversationId = safeStr(d.conversation_id);
    currentBackendTaskId = safeStr(d.task_id);
    currentBackendTaskOffset = 0;
    currentBackendManaged = true;
    updateBusy('后台 Agent 已启动');
    await pollBackendTurn(botMsg);
  }

  async function pollBackendTurn(botMsg) {
    while (currentBackendTaskId && !turnStopRequested) {
      const url = `/api/codex-agent/turn/status?task_id=${encodeURIComponent(currentBackendTaskId)}&after=${encodeURIComponent(currentBackendTaskOffset)}`;
      const r = await fetch(url);
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(safeStr(d.detail) || `HTTP ${r.status}`);
      const events = Array.isArray(d.events) ? d.events : [];
      events.forEach(event => handleCodexEvent(event, botMsg));
      currentBackendTaskOffset = Number(d.next_event_index || currentBackendTaskOffset + events.length) || 0;
      savePanelSnapshot();
      if (['completed', 'failed', 'stopped'].includes(safeStr(d.status))) {
        if (safeStr(d.status) === 'failed' && d.error) {
          pushErrorBlockOnce(botMsg, d.error);
          renderBody();
        }
        break;
      }
      await wait(900);
    }
  }

  async function recoverBackendTurn(options = {}) {
    if (!state.workdirReady || (state.status === 'busy' && !options.force)) return;
    const canvasId = currentCanvasId();
    if (!canvasId) return;
    try {
      if (currentBackendTaskId) {
        let botMsg = [...state.messages].reverse().find(msg => msg.role !== 'user');
        if (!botMsg) {
          botMsg = { role: 'bot', blocks: [] };
          state.messages.push(botMsg);
          renderBody();
        }
        if (options.keepStatus) setRecovering('reconnecting_task', '正在接回后台任务');
        else setBusy('正在接回后台任务');
        await pollBackendTurn(botMsg);
        if (!options.keepStatus) finishBusy('ready');
        return;
      }
      const qs = new URLSearchParams({
        project_dir: state.projectDir,
        canvas_id: canvasId,
      });
      if (state.conversationId) qs.set('conversation_id', state.conversationId);
      const url = `/api/codex-agent/turn/active?${qs.toString()}`;
      const r = await fetch(url);
      if (!r.ok) return;
      const d = await r.json();
      const task = d.task;
      if (!task?.task_id) return;
      currentBackendTaskId = safeStr(task.task_id);
      currentBackendTaskOffset = 0;
      currentBackendManaged = true;
      const botMsg = { role: 'bot', blocks: [{ type: 'tool', text: '已接回正在后台运行的 Agent 任务', status: 'running' }] };
      state.messages.push(botMsg);
      renderBody();
      if (options.keepStatus) setRecovering('reconnecting_task', '正在接回后台任务');
      else setBusy('正在接回后台任务');
      await pollBackendTurn(botMsg);
      if (!options.keepStatus) finishBusy('ready');
    } catch (e) {
      console.warn('recoverBackendTurn failed', e);
      alertRecoveryFailure('后台任务接回失败，已保留聊天记录：' + safeStr(e.message || e));
      if (!options.keepStatus) finishBusy('ready');
    } finally {
      currentBackendManaged = false;
      currentBackendTaskId = '';
      currentBackendTaskOffset = 0;
      savePanelSnapshot({ immediate: true });
    }
  }

  async function onSend() {
    if (state.status === 'busy') {
      stopCurrentTurn();
      return;
    }
    if (isRecoveringStatus()) {
      showHint(state.busyLabel || '正在恢复，请稍等');
      return;
    }
    const input = $('#cm-input');
    const text = inputText().trim();
    if (!text || !state.workdirReady) return;

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

    const botMsg = { role: 'bot', startedAt: Date.now(), blocks: [] };
    activeTurnStatusBlock = { type: 'agent_status', text: '发送中' };
    botMsg.blocks.push(activeTurnStatusBlock);
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
    currentBackendTaskId = '';
    currentBackendTaskOffset = 0;
    currentBackendManaged = true;
    currentTurnAbortController = null;

    try {
      await startBackendTurn(text, attachments, botMsg);
      if (!turnStopRequested && pendingCanvasActionPromises.length) {
        await Promise.allSettled(pendingCanvasActionPromises);
      }
      if (turnStopRequested) {
        finishProcessBlocks(botMsg, 'skipped');
        botMsg.blocks.push({ type: 'tool', text: '已停止当前回复', status: 'done' });
        renderBody();
      } else {
        finishProcessBlocks(botMsg, 'done');
        renderBody();
      }
    } catch (e) {
      if (turnStopRequested || e?.name === 'AbortError') {
        finishProcessBlocks(botMsg, 'skipped');
        botMsg.blocks.push({ type: 'tool', text: '已停止当前回复', status: 'done' });
      } else {
        finishProcessBlocks(botMsg, 'error');
        pushErrorBlockOnce(botMsg, e.message || e);
      }
      renderBody();
    }
    clearActiveTurnStatus();
    renderBody();
    currentTurnAttachments = [];
    currentBackendManaged = false;
    currentBackendTaskId = '';
    currentBackendTaskOffset = 0;
    savePanelSnapshot();
    finishBusy('ready');
  }

  function stopCurrentTurn() {
    turnStopRequested = true;
    updateBusy('正在停止');
    const taskId = currentBackendTaskId;
    if (taskId) {
      fetch('/api/codex-agent/turn/stop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: taskId }),
      }).catch(() => {});
    }
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

  function upsertProgressBlock(botMsg, data = {}) {
    const id = safeStr(data.id || data.task_id || 'agent-progress');
    let block = botMsg.blocks.find(b => b.type === 'progress' && safeStr(b.id) === id);
    if (!block) {
      block = { type: 'progress', id, title: '', text: '', status: 'running' };
      botMsg.blocks.push(block);
    }
    Object.assign(block, data, { id });
    return block;
  }

  function handleCodexEvent(msg, botMsg) {
    const method = safeStr(msg.method);
    const params = msg.params || {};

    if (method === 'task/started') {
      updateBusy('正在思考');
    } else if (method === 'warning') {
      const text = safeStr(params.message || params.warning);
      if (params.thread_id) state.threadId = safeStr(params.thread_id);
      if (text) botMsg.blocks.push({ type: 'system_notice', text });
      renderBody();
    } else if (method === 'canvas/tool_call') {
      const tool = safeStr(params.tool || params.name);
      updateBusy(params.query ? '正在查询画布' : '正在执行画布工具');
      addProcessStep(botMsg, {
        kind: params.query ? 'canvas_query' : 'canvas_tool',
        label: params.query ? '查询画布' : '执行画布工具',
        tool,
        status: 'running',
        detail: tool ? `工具：${tool}` : '',
      });
      renderBody();
    } else if (method === 'canvas/tool_result') {
      const tool = safeStr(params.tool || params.name || params.result?.tool);
      const result = params.result && typeof params.result === 'object' ? params.result : params;
      const ok = result.ok !== false;
      const changed = Number(result.changed || 0);
      const skipped = Number(result.skipped || 0);
      const nodes = Array.isArray(result.nodes)
        ? result.nodes
        : (Array.isArray(result.results) ? result.results.flatMap(res => Array.isArray(res.items) ? res.items : []) : []);
      const nodeCount = Number(result.node_count ?? nodes.length ?? 0);
      const text = ok
        ? (params.query
          ? `已查询 ${tool || '画布'}，返回 ${nodeCount} 个节点`
          : `已执行 ${tool || '画布工具'}，影响 ${changed} 个节点${skipped ? `，跳过 ${skipped} 项` : ''}`)
        : `${tool || '画布工具'} 失败：${safeStr(result.message || '未知原因')}`;
      if (params.query) {
        updateProcessToolResult(botMsg, params, result, nodes);
        renderBody();
        return;
      }
      const block = ok ? {
        type: 'canvas_action_result',
        text,
        status: 'done',
        changed,
        skipped,
        results: Array.isArray(result.results) ? result.results : [],
        nodes: nodes.slice(0, 6),
        locator_title: nodes.length ? '受影响节点' : '',
      } : {
        type: 'error',
        title: params.query ? '画布查询' : '画布工具',
        tool,
        text,
        status: 'error',
        nodes: nodes.slice(0, 6),
      };
      botMsg.blocks.push(block);
      renderBody();
      if (ok && changed > 0 && window.SmartCanvasAgentApi?.refreshFromServer) {
        window.SmartCanvasAgentApi.refreshFromServer();
      }
    } else if (method === 'canvas/tool_followup') {
      updateBusy('正在基于画布查询继续回答');
    } else if (method === 'canvas/action_pending') {
      const approvalId = safeStr(params.approval_id);
      botMsg.blocks.push({
        type: 'choice',
        title: params.tool === 'generate_images' || params.tool === 'generate_videos' ? '生成任务确认' : (params.risk === 'high' ? '高风险画布动作需要确认' : '画布动作需要确认'),
        text: safeStr(params.reason || '确认后才会修改画布'),
        status: 'pending',
        task_id: safeStr(params.task_id || currentBackendTaskId),
        approval_id: approvalId,
        risk: safeStr(params.risk || 'normal'),
        actions: Array.isArray(params.actions) ? params.actions : [],
        options: Array.isArray(params.options) ? params.options : [
          { label: '执行', value: 'approve', action: 'resolve_canvas_action' },
          { label: '跳过', value: 'skip', action: 'resolve_canvas_action' },
        ],
      });
      renderBody();
    } else if (method === 'canvas/action_result') {
      const approvalId = safeStr(params.approval_id);
      if (approvalId && hasCanvasActionResultForApproval(approvalId)) return;
      const changed = Number(params.changed || 0);
      const skipped = Number(params.skipped || 0);
      const ok = params.ok !== false;
      const resultItems = (Array.isArray(params.results) ? params.results : [])
        .flatMap(res => Array.isArray(res.items) ? res.items : [])
        .filter(item => item && (item.id || item.type))
        .slice(0, 3);
      const itemHint = resultItems.length
        ? '：' + resultItems.map(item => `${safeStr(item.title || item.name || item.type || '节点')} @ ${Math.round(Number(item.x || 0))},${Math.round(Number(item.y || 0))}`).join('；')
        : '';
      const text = ok
        ? `后端已执行画布动作，影响 ${changed} 个节点${skipped ? `，跳过 ${skipped} 项` : ''}${itemHint}`
        : `后端画布动作未执行：${safeStr(params.message || '未知原因')}`;
      botMsg.blocks.push(ok ? {
        type: 'canvas_action_result',
        text,
        status: 'done',
        changed,
        skipped,
        approval_id: approvalId,
        results: Array.isArray(params.results) ? params.results : [],
        nodes: resultItems,
      } : {
        type: 'error',
        text,
      });
      if (ok && resultItems.length) {
        const actionBlock = botMsg.blocks[botMsg.blocks.length - 1];
        if (actionBlock?.type === 'canvas_action_result') {
          actionBlock.locator_title = '受影响节点';
        }
      }
      renderBody();
      if (changed > 0 && window.SmartCanvasAgentApi?.refreshFromServer) {
        window.SmartCanvasAgentApi.refreshFromServer();
      }
    } else if (method === 'task/completed') {
      updateBusy('完成');
      botMsg.blocks.forEach(b => { if (b.streaming) b.streaming = false; });
      renderBody();
    } else if (method === 'item/started') {
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
        currentReasoningText = normalizeThinkingText(item.summary) || normalizeThinkingText(item.text);
        if (currentReasoningText) botMsg.blocks.push({ type: 'thinking', text: currentReasoningText, id: item.id });
      } else if (type === 'commandExecution') {
        updateBusy('正在执行命令');
        currentToolId = item.id;
        addProcessStep(botMsg, {
          kind: 'command',
          label: '命令执行',
          command: safeStr(item.command) || '(命令)',
          detail: safeStr(item.command) || '(命令)',
          status: 'running',
          id: item.id,
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
        currentToolId = item.id;
        addProcessStep(botMsg, {
          kind: type,
          label: type === 'fileChange' ? '文件变更' : '工具调用',
          tool: type,
          detail: label,
          status: 'running',
          id: item.id,
        });
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
        if (blk) {
          blk.text = normalizeThinkingText(currentReasoningText);
        } else if (normalizeThinkingText(currentReasoningText)) {
          botMsg.blocks.push({ type: 'thinking', text: normalizeThinkingText(currentReasoningText), id: currentReasoningId });
        }
      }
      renderBody();
    } else if (method === 'item/commandExecution/outputDelta') {
      updateBusy('正在执行命令');
      const out = safeStr(params.delta);
      const step = findProcessStep(botMsg, currentToolId);
      if (step) step.output = `${safeStr(step.output)}${out ? `\n${out}` : ''}`.trim();
      renderBody();
    } else if (method === 'item/completed') {
      const item = params.item || {};
      const blk = botMsg.blocks.find(b => b.id === item.id);
      if (blk) {
        if (blk.type === 'text') {
          blk.text = safeStr(item.text) || blk.text;
          blk.streaming = false;
          if (!currentBackendManaged) {
            const actionPromise = executeCanvasActionsFromText(blk.text, botMsg);
            pendingCanvasActionPromises.push(actionPromise);
          }
          blk.text = cleanAgentDisplayText(blk.text);
        } else if (blk.type === 'image') {
          blk.path = safeStr(item.savedPath) || safeStr(item.path) || blk.path;
          blk.status = 'done';
          if (!currentBackendManaged) addGeneratedImageToCanvas(blk.path, blk.prompt);
        } else if (blk.type === 'tool' || blk.type === 'tool_call') {
          blk.status = 'done';
        }
      }
      const processStep = findProcessStep(botMsg, item.id);
      if (processStep) {
        processStep.status = 'done';
        processStep.endedAt = Date.now();
        processStep.summary = safeStr(processStep.summary || '完成');
      }
      if (safeStr(item.type) === 'imageGeneration') {
        const path = safeStr(item.savedPath) || safeStr(item.path);
        if (path && !currentBackendManaged) addGeneratedImageToCanvas(path, safeStr(item.prompt));
      }
      renderBody();
    } else if (method === 'turn/completed') {
      updateBusy('完成');
      botMsg.blocks.forEach(b => { if (b.streaming) b.streaming = false; });
      finishProcessBlocks(botMsg, 'done');
      renderBody();
    } else if (method === 'error' || method === 'fatal' || method === 'turn/timeout') {
      updateBusy('请求异常');
      finishProcessBlocks(botMsg, 'error');
      const errText = safeStr(params.message) || safeStr(params.error) || JSON.stringify(params || {});
      pushErrorBlockOnce(botMsg, errText);
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
      .replace(/```(?:canvas_agent_tool|canvas-agent-tool|canvas_tool|canvas-tool)\s*[\s\S]*?```/gi, '')
      .replace(/<(?:canvas_agent_tool|canvas_tool)>[\s\S]*?<\/(?:canvas_agent_tool|canvas_tool)>/gi, '')
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
    else if (state.status === 'busy' || isRecoveringStatus()) dot.classList.add('cm-status-busy');
    else if (state.status === 'error') dot.classList.add('cm-status-error');

    $('#cm-fab').classList.toggle('cm-fab-busy', state.status === 'busy' || isRecoveringStatus());

    const proj = $('#cm-proj');
    if (state.workdirReady && state.projectDir) {
      proj.innerHTML = `<span title="${escapeAttr(state.projectDir)}">📁 ${escapeHtml(shortPath(state.projectDir))}</span>`;
    } else if (state.workdirReady) {
      proj.innerHTML = '<span title="无目录">∅ 无目录</span>';
    } else {
      proj.innerHTML = '<span class="cm-proj-empty">— 工作目录 —</span>';
    }

    const input = $('#cm-input');
    const send = $('#cm-send');
    const projBtn = $('#cm-proj');
    const newBtn = $('#cm-new');
    const histBtn = $('#cm-history');
    const attachBtn = $('#cm-attach-canvas');
    const atBtn = $('#cm-at');
    const slashBtn = $('#cm-slash');
    const modeBtn = $('#cm-mode');
    const scopeBtn = $('#cm-scope');
    const approvalBtn = $('#cm-approval');
    const recovering = isRecoveringStatus();
    const enabled = state.workdirReady && !recovering;
    if (projBtn) projBtn.disabled = recovering;
    if (newBtn) newBtn.disabled = !enabled || state.status === 'busy';
    if (histBtn) histBtn.disabled = !enabled || state.status === 'busy';
    if (attachBtn) attachBtn.disabled = !enabled || state.status === 'busy';
    if (atBtn) atBtn.disabled = !enabled || state.status === 'busy';
    if (slashBtn) slashBtn.disabled = !enabled || state.status === 'busy';
    if (modeBtn) {
      modeBtn.textContent = selectedLabel(inputModes, state.inputMode);
      modeBtn.disabled = recovering;
    }
    if (scopeBtn) {
      scopeBtn.textContent = selectedLabel(inputScopes, state.inputScope);
      scopeBtn.disabled = recovering;
    }
    if (approvalBtn) {
      approvalBtn.textContent = selectedLabel(approvalPolicies, state.approvalPolicy);
      approvalBtn.disabled = recovering;
    }
    input.contentEditable = enabled && state.status !== 'busy' ? 'true' : 'false';
    input.classList.toggle('cm-input-disabled', !enabled || state.status === 'busy');
    send.disabled = !enabled && state.status !== 'busy';
    send.textContent = recovering ? '恢复中' : (state.status === 'busy' ? '停止' : '发送');
    send.classList.toggle('cm-send-stop', state.status === 'busy');

    const hint = $('#cm-hint');
    hint.classList.remove('cm-foot-hint-busy');
    hint.textContent = state.workdirReady ? workdirLabel(state.projectDir) : '未选工作目录';
    updateSelectionHint();
  }

  function updateSelectionHint() {
    const target = $('#cm-selection-hint');
    if (!target) return;
    target.textContent = isRecoveringStatus()
      ? (state.busyLabel || '恢复对话中')
      : `选中 ${selectedNodeCount()} 节点`;
  }

  function renderBusyHint(elapsed, label) {
    return `<span class="cm-busy-pill" aria-label="${escapeAttr(`${elapsed} · ${label}`)}">
      <span class="cm-busy-pulse" aria-hidden="true"></span>
      <span class="cm-busy-time">${escapeHtml(elapsed)}</span>
      <span class="cm-busy-label">${escapeHtml(label)}</span>
      <span class="cm-busy-dots" aria-hidden="true"><i></i><i></i><i></i></span>
    </span>`;
  }

  function renderBody(options = {}) {
    const body = $('#cm-body');
    if (state.messages.length === 0) {
      body.innerHTML = state.workdirReady
        ? '<div class="cm-empty">当前会话还没有消息。</div>'
        : '<div class="cm-empty">选择工作目录，或选择“无目录”，<br>然后开始和 Codex 对话。</div>';
      savePanelSnapshot();
      return;
    }
    const oldScroll = options.preserveScroll ? (state.scrollTop || body.scrollTop || 0) : 0;
    body.innerHTML = state.messages.map(renderMessage).join('');
    refreshPanelIcons();
    bindCodeCopyButtons(body);
    bindMessageActionButtons(body);
    body.scrollTop = options.preserveScroll ? oldScroll : body.scrollHeight;
    state.scrollTop = Math.round(body.scrollTop || 0);
    savePanelSnapshot();
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
    const blocks = coalesceBotBlocksForRender(msg.blocks || []);
    const thinkingBlocks = blocks.filter(block => block.type === 'thinking');
    const contentBlocks = blocks.filter(block => block.type !== 'thinking');
    const mergedThinking = thinkingBlocks.map(block => normalizeThinkingText(block.text)).filter(Boolean).join('\n\n');
    const thinkingHtml = mergedThinking ? renderBlock({ type: 'thinking', text: mergedThinking }) : '';
    const summaryHtml = renderTurnSummary(msg, blocks);
    const blocksHtml = contentBlocks.map(renderBlock).filter(Boolean).join('');
    return `<div class="cm-msg cm-msg-bot-wrap">${thinkingHtml}${blocksHtml}${summaryHtml}</div>`;
  }

  function renderTurnSummary(msg, blocks) {
    const processBlocks = blocks.filter(block => block.type === 'process' && Array.isArray(block.steps) && block.steps.length);
    if (!processBlocks.length) return '';
    const startedCandidates = [Number(msg.startedAt || 0), ...processBlocks.map(block => Number(block.startedAt || 0))].filter(Boolean);
    const endedCandidates = [Number(msg.completedAt || msg.endedAt || 0), ...processBlocks.map(block => Number(block.endedAt || 0))].filter(Boolean);
    const started = startedCandidates.length ? Math.min(...startedCandidates) : 0;
    const ended = endedCandidates.length ? Math.max(...endedCandidates) : 0;
    const duration = started && ended ? formatDurationHuman(Math.max(0, ended - started)) : '';
    const completedAt = ended ? new Date(ended).toLocaleString() : '';
    return `<div class="cm-turn-summary" tabindex="0" title="${escapeAttr(completedAt ? `完成于 ${completedAt}` : '本轮对话正在处理')}">
      <span class="cm-turn-summary-icon">${icon('clock-3')}</span><span>${ended ? '已处理' : '处理中'}${duration ? ` ${escapeHtml(duration)}` : ''}</span>
      ${completedAt ? `<small>完成于 ${escapeHtml(completedAt)}</small>` : ''}
    </div>`;
  }

  function coalesceBotBlocksForRender(blocks) {
    const out = [];
    (Array.isArray(blocks) ? blocks : []).forEach(block => {
      if (!block || typeof block !== 'object') return;
      if (block.type === 'process') {
        let previous = null;
        for (let index = out.length - 1; index >= 0; index -= 1) {
          if (isInvisibleTimelineBlock(out[index])) continue;
          previous = out[index];
          break;
        }
        if (previous?.type === 'process') {
          previous.status = block.status || previous.status;
          previous.startedAt = previous.startedAt || block.startedAt || 0;
          previous.endedAt = block.endedAt || previous.endedAt || 0;
          previous.steps.push(...(Array.isArray(block.steps) ? block.steps : []));
        } else {
          out.push({ ...block, steps: [...(Array.isArray(block.steps) ? block.steps : [])] });
        }
        return;
      }
      if (block.type === 'tool_call' || block.type === 'tool_result') {
        let target = null;
        for (let index = out.length - 1; index >= 0; index -= 1) {
          if (isInvisibleTimelineBlock(out[index])) continue;
          target = out[index];
          break;
        }
        if (target?.type !== 'process') {
          target = { type: 'process', title: '工具与命令', status: block.status || 'done', steps: [] };
          out.push(target);
        }
        target.steps.push({
          kind: block.type,
          label: block.title || block.tool || '工具调用',
          tool: block.tool || block.command || '',
          command: block.command || '',
          detail: block.text || block.message || '',
          status: block.status || 'done',
        });
        return;
      }
      out.push(block);
    });
    return out;
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
      const t = normalizeThinkingText(b.text);
      if (!t) return '';
      const truncated = t.length > 200 ? '…' + t.slice(-200) : t;
      const status = b.status === 'stale' || b.stale ? '上次刷新前正在思考' : '思考';
      return `<details class="cm-thinking" title="${escapeAttr(t)}"><summary>${escapeHtml(status)}：${escapeHtml(truncated)}</summary><div>${escapeHtml(t)}</div></details>`;
    }
    if (type === 'tool') {
      const txt = safeStr(b.text);
      const isAction = /画布动作|影响\s*\d+\s*个节点|后端已执行/.test(txt);
      const cls = isAction ? ' cm-msg-tool-action' : '';
      const mark = b.status === 'running' ? ' ⏳' : (b.status === 'stale' || b.stale ? ' · 上次刷新前进行中' : ' ✅');
      return `<div class="cm-msg-tool${cls}">${escapeHtml(txt)}${mark}</div>`;
    }
    if (type === 'system_notice') {
      const text = safeStr(b.text).trim();
      if (!text) return '';
      return `<div class="cm-system-notice">${escapeHtml(text)}</div>`;
    }
    if (type === 'transient_notice') {
      const text = safeStr(b.text).trim();
      if (!text) return '';
      return `<div class="cm-transient-notice">${escapeHtml(text)}</div>`;
    }
    if (type === 'agent_status') {
      const text = safeStr(b.text).trim();
      if (!text) return '';
      return `<div class="cm-agent-status-line"><span class="cm-agent-status-dot" aria-hidden="true"></span>${escapeHtml(text)}</div>`;
    }
    if (type === 'tool_call' || type === 'tool_result') {
      return renderToolBlock(b);
    }
    if (type === 'process') {
      return renderProcessBlock(b);
    }
    if (type === 'canvas_action_result') {
      return renderCanvasActionResultBlock(b);
    }
    if (type === 'progress') {
      return renderProgressBlock(b);
    }
    if (type === 'node_locator') {
      return renderNodeLocatorBlock(b);
    }
    if (type === 'choice') {
      return renderChoiceBlock(b);
    }
    if (type === 'parameter_form') {
      return renderParameterFormBlock(b);
    }
    if (type === 'media_preview') {
      return renderMediaPreviewBlock(b);
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
      return `<div class="cm-msg-error">❌ ${escapeHtml(normalizeAgentErrorText(b.text))}</div>`;
    }
    return '';
  }

  function blockStatusLabel(status) {
    const s = safeStr(status);
    if (s === 'running') return '运行中';
    if (s === 'stale') return '刷新前进行中';
    if (s === 'error' || s === 'failed') return '失败';
    if (s === 'pending') return '待确认';
    if (s === 'approved') return '已执行';
    if (s === 'done' || s === 'completed') return '完成';
    if (s === 'skipped') return '跳过';
    return s || '记录';
  }

  function renderToolBlock(b) {
    const running = b.status === 'running';
    const title = safeStr(b.title || b.tool || (b.type === 'tool_result' ? '工具结果' : '工具调用'));
    const text = safeStr(b.command || b.text || b.message);
    const summary = `${title}${running ? ' · 运行中' : ''}`;
    return `<details class="cm-process cm-tool-inline${running ? ' cm-process-running' : ''}">
      <summary><span>${escapeHtml(summary)}</span></summary>
      ${text ? `<pre class="cm-process-pre">${escapeHtml(text)}</pre>` : '<div class="cm-process-empty">暂无详情</div>'}
    </details>`;
  }

  function renderProcessBlock(b) {
    const steps = Array.isArray(b.steps) ? b.steps.filter(Boolean) : [];
    if (!steps.length) return '';
    const running = b.status === 'running';
    const commandCount = steps.filter(step => safeStr(step.kind) === 'command').length;
    const toolCount = steps.length - commandCount;
    const summaryParts = [];
    if (commandCount) summaryParts.push(`${running ? '正在执行' : '已执行'} ${commandCount} 项命令执行`);
    if (toolCount) summaryParts.push(`${commandCount ? '' : (running ? '正在执行 ' : '已执行 ')}${toolCount} 项工具调用`);
    const summary = summaryParts.join('，') || (running ? '正在处理' : '已处理');
    const rows = steps.map(step => {
      const status = blockStatusLabel(step.status);
      const tool = safeStr(step.tool);
      const command = safeStr(step.command);
      const summaryText = safeStr(step.summary);
      const label = safeStr(step.label || '处理');
      const rawDetail = safeStr(step.output || step.detail);
      const repeatedToolDetail = tool && /^(?:工具\s*[:：]\s*)/.test(rawDetail) && rawDetail.replace(/^(?:工具\s*[:：]\s*)/, '').trim() === tool;
      const detail = rawDetail === command || rawDetail === tool || repeatedToolDetail ? '' : rawDetail;
      const stepIcon = safeStr(step.kind) === 'command'
        ? 'terminal'
        : (safeStr(step.kind) === 'canvas_query' || safeStr(step.kind) === 'webSearch' ? 'search' : 'wrench');
      return `<div class="cm-process-step">
        <div class="cm-process-step-head">
          ${icon(stepIcon)}
          <span>${escapeHtml(label)}</span>
          <em>${escapeHtml(summaryText || status)}</em>
        </div>
        ${tool || command ? `<code class="cm-process-step-command">${escapeHtml(tool || command)}</code>` : ''}
        ${detail ? `<small>${escapeHtml(detail)}</small>` : ''}
      </div>`;
    }).join('');
    return `<details class="cm-process${running ? ' cm-process-running' : ''}" title="展开查看详情">
      <summary><span>${escapeHtml(summary)}</span></summary>
      <div class="cm-process-detail">
        ${rows}
      </div>
    </details>`;
  }

  function renderCanvasActionResultBlock(b) {
    const changed = Number(b.changed || 0);
    const skipped = Number(b.skipped || 0);
    const resultRows = Array.isArray(b.results) ? b.results : [];
    const resultNodes = resultRows.flatMap(row => Array.isArray(row?.items) ? row.items : []).filter(Boolean);
    const nodes = (resultNodes.length ? resultNodes : (Array.isArray(b.nodes) ? b.nodes : [])).slice(0, 8);
    const countByKind = new Map();
    const nodeKind = (type, node) => {
      const action = safeStr(type).toLowerCase();
      if (/generate.*video|video_generation/.test(action)) return '视频节点';
      if (/generate.*image|image_generation/.test(action)) return '生图节点';
      if (/add_text|create_text/.test(action)) return '文本节点';
      if (/add_prompt|create_prompt/.test(action)) return '提示词节点';
      if (/add_loop|create_loop/.test(action)) return '循环节点';
      if (/add_media|add_image|add_video|create_media/.test(action)) return '媒体节点';
      if (/group/.test(action)) return '分组节点';
      const nodeType = safeStr(node?.type).toLowerCase();
      if (nodeType === 'smart-loop') return '循环节点';
      if (nodeType === 'smart-prompt') return safeStr(node?.title).toLowerCase() === 'text' ? '文本节点' : '提示词节点';
      return nodeType === 'smart-image' ? '图片节点' : '节点';
    };
    resultRows.forEach(row => {
      const items = Array.isArray(row?.items) ? row.items : [];
      items.forEach(node => {
        const kind = nodeKind(row?.type, node);
        countByKind.set(kind, (countByKind.get(kind) || 0) + 1);
      });
    });
    if (!countByKind.size) nodes.forEach(node => {
      const kind = nodeKind('', node);
      countByKind.set(kind, (countByKind.get(kind) || 0) + 1);
    });
    const countText = [...countByKind.entries()].map(([kind, count]) => `${count}个${kind}`).join('，');
    const generationOnly = [...countByKind.keys()].every(kind => kind === '生图节点' || kind === '视频节点');
    const summary = countText
      ? `${generationOnly ? '已生成' : '已创建'} ${countText}`
      : `已完成画布操作${changed ? `，影响 ${changed} 个节点` : ''}${skipped ? `，跳过 ${skipped} 项` : ''}`;
    const nodeRows = nodes.map(node => {
      const id = safeStr(node.id);
      const label = safeStr(node.title || node.name || node.type || '节点');
      const xy = `${Math.round(Number(node.x || 0))}, ${Math.round(Number(node.y || 0))}`;
      return `<button class="cm-node-chip" data-node-id="${escapeAttr(id)}" title="定位节点">${escapeHtml(label)} <span>@ ${escapeHtml(xy)}</span></button>`;
    }).join('');
    return `<details class="cm-agent-block cm-canvas-action-block" open>
      <summary title="展开或折叠画布动作">
        <span class="cm-canvas-action-icon">${icon('sparkles')}</span>
        <b>${escapeHtml(summary)}</b>
        <em>影响 ${changed} / 跳过 ${skipped}</em>
      </summary>
      ${nodeRows ? `<div class="cm-node-chip-row">${nodeRows}</div>` : ''}
    </details>`;
  }

  function renderProgressBlock(b) {
    const total = Number(b.total || 0);
    const current = Number(b.current || b.progress?.current || 0);
    const percent = Number.isFinite(Number(b.percent)) ? Number(b.percent) : (total > 0 ? Math.round(current / total * 100) : 0);
    const clamped = Math.max(0, Math.min(100, percent || 0));
    const title = safeStr(b.title || '任务进度');
    const text = safeStr(b.text || blockStatusLabel(b.status));
    return `<div class="cm-agent-block cm-progress-block">
      <div class="cm-block-head"><span>任务</span><b>${escapeHtml(title)}</b><em>${escapeHtml(blockStatusLabel(b.status))}</em></div>
      <div class="cm-progress-track"><i style="width:${clamped}%"></i></div>
      <div class="cm-block-text">${escapeHtml(text)}${total ? ` · ${current}/${total}` : ''}</div>
    </div>`;
  }

  function renderNodeLocatorBlock(b) {
    const nodes = Array.isArray(b.nodes) ? b.nodes.filter(n => n && n.id).slice(0, 8) : [];
    if (!nodes.length) return '';
    const title = safeStr(b.title || '节点定位');
    const chips = nodes.map(node => {
      const id = safeStr(node.id);
      const label = safeStr(node.title || node.name || node.type || '节点');
      return `<button class="cm-node-chip" data-node-id="${escapeAttr(id)}">${escapeHtml(label)}</button>`;
    }).join('');
    return `<div class="cm-agent-block cm-node-locator-block">
      <div class="cm-block-head"><span>定位</span><b>${escapeHtml(title)}</b><em>${nodes.length} 个</em></div>
      <div class="cm-node-chip-row">${chips}</div>
    </div>`;
  }

  function renderChoiceBlock(b) {
    const options = Array.isArray(b.options) ? b.options : [];
    const title = safeStr(b.title || b.text || '请选择');
    const approvalId = safeStr(b.approval_id);
    const taskId = safeStr(b.task_id);
    const buttons = options.map(opt => {
      const label = typeof opt === 'string' ? opt : safeStr(opt.label || opt.text || opt.value);
      const value = typeof opt === 'string' ? opt : safeStr(opt.value || opt.id || label);
      const action = typeof opt === 'string' ? '' : safeStr(opt.action);
      return `<button class="cm-choice-btn" type="button" data-choice-action="${escapeAttr(action)}" data-choice-value="${escapeAttr(value)}" data-task-id="${escapeAttr(taskId)}" data-approval-id="${escapeAttr(approvalId)}"${b.status && b.status !== 'pending' ? ' disabled' : ''}>${escapeHtml(label)}</button>`;
    }).join('');
    const actionRows = Array.isArray(b.actions) ? b.actions.slice(0, 6).map(action => {
      const type = safeStr(action.type || 'action');
      const count = Number(action.count || 1);
      const scope = safeStr(action.scope);
      return `<li>${escapeHtml(type)}${count > 1 ? ` · ${count} 项` : ''}${scope ? ` · ${escapeHtml(scope)}` : ''}</li>`;
    }).join('') : '';
    return `<div class="cm-agent-block cm-choice-block">
      <div class="cm-block-head"><span>确认</span><b>${escapeHtml(title)}</b><em>${escapeHtml(blockStatusLabel(b.status || 'pending'))}</em></div>
      ${b.text ? `<div class="cm-block-text">${escapeHtml(safeStr(b.text))}</div>` : ''}
      ${actionRows ? `<ul class="cm-choice-actions">${actionRows}</ul>` : ''}
      <div class="cm-choice-row">${buttons || '<span class="cm-block-muted">暂无选项</span>'}</div>
    </div>`;
  }

  function renderParameterFormBlock(b) {
    const fields = Array.isArray(b.fields) ? b.fields : [];
    const rows = fields.slice(0, 8).map(field => {
      const label = safeStr(field.label || field.name || field.id);
      const value = safeStr(field.value ?? field.default ?? '');
      return `<div class="cm-form-row"><span>${escapeHtml(label)}</span><code>${escapeHtml(value || '待定')}</code></div>`;
    }).join('');
    return `<div class="cm-agent-block cm-form-block">
      <div class="cm-block-head"><span>参数</span><b>${escapeHtml(safeStr(b.title || '参数确认'))}</b></div>
      ${rows || '<div class="cm-block-muted">暂无参数</div>'}
    </div>`;
  }

  function renderMediaPreviewBlock(b) {
    const items = Array.isArray(b.items) ? b.items : [];
    if (!items.length) return '';
    return `<div class="cm-agent-block cm-media-preview-block">${renderMessageAttachments(items, items.length)}</div>`;
  }

  function bindMessageActionButtons(root) {
    root.querySelectorAll('.cm-node-chip[data-node-id]').forEach(btn => {
      btn.addEventListener('click', e => {
        e.preventDefault();
        const id = btn.getAttribute('data-node-id');
        if (!id) return;
        if (window.SmartCanvasAgentApi?.focusNodes) {
          const ok = window.SmartCanvasAgentApi.focusNodes([id]);
          showHint(ok ? '已定位节点' : '没找到这个节点');
        } else {
          showHint('当前画布还不支持节点定位');
        }
      });
    });
    root.querySelectorAll('.cm-choice-btn[data-choice-action="resolve_canvas_action"]').forEach(btn => {
      btn.addEventListener('click', e => {
        e.preventDefault();
        const taskId = btn.getAttribute('data-task-id') || '';
        const approvalId = btn.getAttribute('data-approval-id') || '';
        const decision = btn.getAttribute('data-choice-value') || 'approve';
        if (!taskId || !approvalId) {
          showHint('确认信息不完整，无法执行');
          return;
        }
        resolveCanvasActionApproval(taskId, approvalId, decision, btn);
      });
    });
  }

  async function resolveCanvasActionApproval(taskId, approvalId, decision, btn) {
    const blockEl = btn?.closest?.('.cm-choice-block');
    if (blockEl) blockEl.classList.add('cm-block-running');
    try {
      const r = await fetch('/api/codex-agent/action/resolve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: taskId, approval_id: approvalId, decision }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(safeStr(d.detail) || `HTTP ${r.status}`);
      markApprovalBlockResolved(approvalId, ['approve', 'run', 'execute', 'run_generation', 'create_nodes'].includes(decision) ? 'approved' : 'skipped');
      const result = d.result || {};
      if (d.decision === 'approved') {
        appendCanvasActionResultBlock(result);
        if (Number(result.changed || 0) > 0 && window.SmartCanvasAgentApi?.refreshFromServer) {
          window.SmartCanvasAgentApi.refreshFromServer();
        }
      } else {
        showHint('已跳过画布动作');
      }
      renderBody();
      savePanelSnapshot({ immediate: true });
    } catch (e) {
      showHint('处理确认失败：' + safeStr(e.message || e), 6000);
      if (blockEl) blockEl.classList.remove('cm-block-running');
    }
  }

  function markApprovalBlockResolved(approvalId, status) {
    state.messages.forEach(msg => {
      (msg.blocks || []).forEach(block => {
        if (block.type === 'choice' && safeStr(block.approval_id) === approvalId) {
          block.status = status;
          block.decision = status;
        }
      });
    });
  }

  function appendCanvasActionResultBlock(params = {}) {
    const approvalId = safeStr(params.approval_id);
    if (approvalId && hasCanvasActionResultForApproval(approvalId)) return;
    const changed = Number(params.changed || 0);
    const skipped = Number(params.skipped || 0);
    const resultItems = (Array.isArray(params.results) ? params.results : [])
      .flatMap(res => Array.isArray(res.items) ? res.items : [])
      .filter(item => item && (item.id || item.type))
      .slice(0, 3);
    const text = `已执行画布动作，影响 ${changed} 个节点${skipped ? `，跳过 ${skipped} 项` : ''}`;
    const botMsg = state.messages[state.messages.length - 1]?.role === 'bot'
      ? state.messages[state.messages.length - 1]
      : (state.messages.push({ role: 'bot', blocks: [] }), state.messages[state.messages.length - 1]);
    botMsg.blocks.push({
      type: 'canvas_action_result',
      text,
      status: 'done',
      changed,
      skipped,
      approval_id: approvalId,
      results: Array.isArray(params.results) ? params.results : [],
      nodes: resultItems,
      locator_title: resultItems.length ? '受影响节点' : '',
    });
  }

  function hasCanvasActionResultForApproval(approvalId) {
    return state.messages.some(msg => (msg.blocks || []).some(block =>
      block.type === 'canvas_action_result' && safeStr(block.approval_id) === approvalId
    ));
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

      const promptBlock = collectReusableTextBlock(lines, i);
      if (promptBlock) {
        html.push(`<p><strong>${escapeHtml(promptBlock.label)}：</strong></p>`);
        html.push(renderCodeBlock(promptBlock.text, promptBlock.lang));
        i = promptBlock.nextIndex;
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

  function collectReusableTextBlock(lines, index) {
    const first = safeStr(lines[index]);
    const hit = first.match(/^\s*((?:反推|最终|完整|优化后|上游|生图|视频|负面)?提示词|Prompt|prompt)\s*[:：]\s*(.*)$/);
    if (!hit) return null;

    const label = hit[1];
    const collected = [];
    if (hit[2]) collected.push(hit[2]);
    let i = index + 1;
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^(#{1,4})\s+/.test(lines[i]) &&
      !/^\s*>\s?/.test(lines[i]) &&
      !/^\s*[-*]\s+/.test(lines[i]) &&
      !/^\s*\d+\.\s+/.test(lines[i]) &&
      !/^\s*((?:反推|最终|完整|优化后|上游|生图|视频|负面)?提示词|Prompt|prompt)\s*[:：]/.test(lines[i])
    ) {
      collected.push(lines[i]);
      i++;
    }

    const text = collected.join('\n').trim();
    if (text.length < 32 && !/[,，。；;]/.test(text)) return null;
    return {
      label,
      text,
      lang: /^prompt$/i.test(label) ? 'text' : 'text',
      nextIndex: i,
    };
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

  function closeTopPopovers() {
    if (state.projPopOpen) {
      state.projPopOpen = false;
      renderProjPop();
    }
    if (state.histPopOpen) {
      state.histPopOpen = false;
      renderHistPop();
    }
  }

  function onDocClick(e) {
    if (state.projPopOpen || state.histPopOpen) {
      const projPop = $('#cm-proj-pop');
      const histPop = $('#cm-hist-pop');
      const projBtn = $('#cm-proj');
      const histBtn = $('#cm-history');
      const insideTopPopover =
        projPop?.contains(e.target) ||
        histPop?.contains(e.target) ||
        projBtn?.contains(e.target) ||
        histBtn?.contains(e.target);
      if (!insideTopPopover) closeTopPopovers();
    }
    if (state.workdirDialogOpen) {
      const dialog = $('#cm-workdir-dialog .cm-dialog-card');
      if (dialog && !dialog.contains(e.target) && !e.target.closest('#cm-workdir-add')) {
        closeWorkdirDialog();
      }
    }
    if (state.mentionOpen) {
      const pop = $('#cm-mention-pop');
      const input = $('#cm-input');
      const btn = $('#cm-at');
      if (pop && !pop.contains(e.target) && input && !input.contains(e.target) && btn && !btn.contains(e.target)) {
        closeMentionPicker();
      }
    }
    if (state.commandOpen) {
      const pop = $('#cm-command-pop');
      const input = $('#cm-input');
      const btn = $('#cm-slash');
      if (pop && !pop.contains(e.target) && input && !input.contains(e.target) && btn && !btn.contains(e.target)) {
        closeCommandPicker();
      }
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', inject);
  } else {
    inject();
  }
})();
