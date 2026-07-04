# Infinite-Canvas × Codex Agent 改造方案

> 最后更新：2026-07-04  
> 状态：方案确定，阶段 1 实施中  
> 目标：在 Infinite-Canvas 画布里内嵌一个"Codex Agent"侧栏，作为本地 Codex CLI 的前端壳

---

## 1. 项目背景

### 1.1 现状
- `main.py` 单文件 FastAPI 后端，**16255 行**，单 git 仓库在 `hero8152/Infinite-Canvas`
- 已有 `codex` provider 走 `run_codex_cli()`（`codex exec` 一次性命令），不支持流式
- 静态资源在 `static/`，HTML 单文件 + vanilla JS 风格（`canvas.js` 14552 行 / `smart-canvas.js` 16797 行）
- 已有的 `gpt-chat.html` 是聊天 UI 雏形

### 1.2 目标
在画布里加一个**可隐藏的右侧栏**，作为 Codex CLI 的前端壳：
- 项目文件夹切换 + 多 session 管理
- 流式聊天（thinking / tool / image / text 块）
- 选中节点当附件、生成图片自动落画布
- 多 session 历史、计划清单（TodoFloat）
- **不发明自己的存档机制**，完全跟 Codex CLI 自己的行为一致

### 1.3 核心原则（最重要）
> **画布是 Codex 的前端壳，不是 Codex 的替代品。**
> - 会话存档 → 读 `~/.codex/sessions/`（Codex 自己管）
> - Skills → 读 `~/.codex/skills/.system/` + `<project>/.agents/skills/`
> - 生成的图 → Codex 直接写到 cwd（项目目录），后端只做"事件转发 + URL 暴露"
> - 跨会话 → Codex app-server 的 `thread/resume`

---

## 2. 文件改动清单

### 2.1 改动文件（追加方式，不动现有代码）

| # | 文件 | 改法 | 行数估计 | 备注 |
|---|---|---|---|---|
| 1 | `main.py` | 末尾追加（16256 行之后） | +800 ~ +1200 | 加 `CodexAppServerSession` + 路由组 |
| 2 | `static/gpt-chat.html` | `</body>` 前 +1 行 | +1 | 加载新 JS |
| 3 | `AGENTS.md`（项目根） | 末尾 +1 段 | +10 | 告诉其他 agent 不要碰 codex-agent 模块 |
| 4 | `docs/agent-mode-design.md` | 新建（本文档） | +400 | 方案说明 |

### 2.2 新建文件

| # | 文件 | 行数估计 | 备注 |
|---|---|---|---|
| 5 | `static/js/codex-agent-panel.js` | ~1500 | 侧栏 UI 完整实现（IIFE 独立） |
| 6 | `static/css/codex-agent-panel.css` | ~300 | 样式（`cm-` 前缀避免冲突） |

### 2.3 完全不动的文件（重要！）

```
❌ static/canvas.js              (14552 行)
❌ static/smart-canvas.js        (16797 行)
❌ 任何现有的 Python 函数（包括 is_codex_provider / codex_chat_text / run_codex_cli 等）
❌ data/ 下任何配置
❌ requirements.txt
❌ 任何 .html（除 gpt-chat.html 末尾 +1 行）
```

### 2.4 原作者更新时冲突预测

| 改动 | 冲突概率 | 理由 |
|---|---|---|
| `main.py` 末尾追加 | 极低 | 16256 行后是文件末尾，原作者改动在前面 |
| `static/gpt-chat.html` +1 行 | 极低 | 行末插入 |
| 新建 `static/js/codex-agent-panel.js` | **0** | 原作者永远不会动 |
| 新建 `static/css/codex-agent-panel.css` | **0** | 同上 |
| `AGENTS.md` +1 段 | 低 | 原作者不动这个文件 |

---

## 3. Codex 集成设计

### 3.1 Codex CLI 实际行为（已验证）

```
~/.codex/
├── sessions/<年>/<月>/<日>/rollout-<ISO时间>-<uuid>.jsonl    ← 按时间分，不按项目分
├── skills/.system/<skill-name>/SKILL.md                        ← 系统技能
├── config.toml                                                 ← Codex 配置
└── ...

<项目目录>/                                                     ← Codex cwd
├── .agents/skills/<skill-name>/SKILL.md                        ← 项目级技能（Codex 自动扫）
├── .git/
└── (Codex 生成的图直接写在这里，路径由 Codex 自己定)
```

**关键事实**（从用户 Mac 上的 `~/.codex/sessions/2026/07/04/*.jsonl` 验证）：
1. Codex 会话存档**全部在 `~/.codex/sessions/`**，按时间分，不按项目分
2. 每个 jsonl 文件的每条消息里有 `cwd` 字段（标识该项目目录）
3. 第一个事件是 `session_meta`，含 `session_id` (= threadId)
4. `turn_context` 里有 `cwd`、`workspace_roots`、`sandbox_policy`、`model` 等
5. Codex 生成的图直接写到 cwd（项目目录），画布后端通过 `image_generation` 事件拿到路径
6. Skills 分两层：系统级（`~/.codex/skills/.system/`）+ 项目级（`<cwd>/.agents/skills/`）

### 3.2 长连接架构

```
[画布前端 codex-agent-panel.js]
       │
       │  fetch / EventSource (SSE)
       ▼
[main.py CodexAppServerSession]
       │
       │  stdin/stdout (JSON-RPC)
       ▼
[codex app-server 子进程, cwd = <项目目录>]
       │
       │  流式事件：thinking / tool_call / plan_update / image_generated / text_done
       ▼
[OpenAI Codex API]
```

**关键设计点**：
- 每个**项目目录**对应一个 `codex app-server` 子进程
- 切换项目 = 关旧进程 + 启新进程
- **不要自己存会话历史**，用 `~/.codex/sessions/`（启动时扫描、列出时按 cwd 过滤）
- 后端只做"事件转发 + URL 暴露"，不发明任何存档

### 3.3 路由清单（main.py 末尾追加）

```
GET  /api/codex-agent/status              检测 Codex CLI 安装 + 版本 + 登录状态
POST /api/codex-agent/board/open           {project_dir} → threadId
POST /api/codex-agent/board/list           → 扫 ~/.codex/sessions/ 列出所有项目
GET  /api/codex-agent/sessions/list        ?project_dir=...  → 列出该项目下的所有 session
POST /api/codex-agent/sessions/resume      {session_id} → threadId (调 thread/resume)
POST /api/codex-agent/turn                 {text, attachments} → SSE 流 (turn 事件)
GET  /api/codex-agent/skills/list          ?project_dir=... → 列出可用技能
GET  /api/codex-agent/file/view?path=...   静态文件服务（暴露 Codex 生成的图）
```

### 3.4 数据流：画布里"生成一张图"

```
[1] 用户在侧栏聊天框说"画一只猫"
[2] 前端 POST /api/codex-agent/turn { text, project_dir, attachments: [选中节点图 URL] }
[3] 后端：
    - 把图 URL 下载到 <project_dir>/.codex-refs/xxx.png (作为 Codex 可读路径)
    - 启动 codex app-server (cwd = project_dir)
    - 发 thread/start → 拿 threadId
    - 发 user message (text + image refs)
    - 流式接收事件：
      ├─ thinking → SSE → 前端
      ├─ tool_call(image_generation) → SSE → 前端
      ├─ image_generated { path: "<project_dir>/cat.png" } → 后端 → SSE → 前端
      └─ text_done → SSE → 前端
[4] 前端收到 image_generated 事件：
    - 渲染聊天流："🖼️ 已生成 cat.png"
    - 调用 window.addImageNode({ url: "/api/codex-agent/file/view?path=<project_dir>/cat.png" })
    - 画布右边出现 cat.png 节点
[5] 用户点选 cat.png → 弹菜单"放到分镜文件夹"
[6] 后端 mv <project_dir>/cat.png <project_dir>/分镜/cat.png
[7] 节点 url 更新
```

---

## 4. 阶段计划

### 阶段 0：准备工作（半天）
- [x] 调研 cameo 项目（/Users/a000/CodeSpace/cameo）的 ChatPanel 设计
- [x] 调研 Codex CLI 实际存档行为
- [x] 写本规划文档
- [ ] 用户在 GitHub fork `hero8152/Infinite-Canvas` → 自己的账号
- [ ] `git remote rename origin upstream` + `git remote add origin git@github.com:<YOU>/Infinite-Canvas.git`
- [ ] 切到 `feature/codex-agent` 分支

### 阶段 1：最小可运行版（半天）
- [ ] `main.py` 末尾加 `CodexAppServerSession` 类骨架
- [ ] 加 `/api/codex-agent/status` 端点
- [ ] 加 `/api/codex-agent/file/view` 端点（图片暴露）
- [ ] 重启服务验证：`curl http://127.0.0.1:3000/api/codex-agent/status`

### 阶段 2：聊天 + 流式（1 天）
- [ ] `/api/codex-agent/turn` SSE 端点
- [ ] `static/js/codex-agent-panel.js` 基础框架（侧栏 + 输入框 + 消息列表）
- [ ] 前端 EventSource 接收 + 渲染文本

### 阶段 3：项目切换 + 会话列表（1-2 天）
- [ ] `/api/codex-agent/board/open` + `board/list`
- [ ] `/api/codex-agent/sessions/list`（扫 `~/.codex/sessions/` + 按 cwd 分组）
- [ ] `/api/codex-agent/sessions/resume`
- [ ] 前端项目下拉 UI + 历史会话列表

### 阶段 4：附件 + 画布节点引用（1 天）
- [ ] Composer 附件按钮 + 选中节点 badge
- [ ] 后端下载 ref 图到 `<project_dir>/.codex-refs/`
- [ ] 把图路径作为 user message 的 image refs 发给 Codex

### 阶段 5：流式块渲染（1-2 天）
- [ ] 区分 thinking / tool / image / text 块
- [ ] 渲染 thinking（折叠）、tool（状态条）、image（带生成中/完成两态）

### 阶段 6：TodoFloat（半天）
- [ ] Codex 的 `update_plan` 工具事件监听
- [ ] 浮动 todo 列表 UI

### 阶段 7：技能 + 多会话切换 + 整理文件（1 天）
- [ ] `/api/codex-agent/skills/list`
- [ ] 技能选择 UI
- [ ] 会话切换 UI（下拉）
- [ ] "整理到项目目录"功能

### 阶段 8：打磨 + 文档（半天）
- [ ] 写 `docs/codex-agent-user-guide.md`
- [ ] 写 `AGENTS.md` 末尾的 codex-agent 段落
- [ ] commit + 推到自己 fork

---

## 5. 风险清单

| # | 风险 | 缓解 |
|---|---|---|
| R1 | `codex app-server` 协议细节没完全摸清 | 先用 `--help` + 看 cameo 实现 |
| R2 | Codex 启动后内存占用 | 长连接 + 自动 idle 断开 |
| R3 | 切换项目时旧 thread 状态丢失 | 用 `thread/resume` 接上 |
| R4 | 流式 SSE 在某些代理下不工作 | 后端用 `StreamingResponse` |
| R5 | Codex 生成的图路径含特殊字符 | URL 编码 + path 校验 |
| R6 | 用户没登录 Codex | `/status` 端点返回清晰提示 |
| R7 | 沙箱拒绝写项目目录外的路径 | 默认 workspace-write + 给项目目录即可 |

---

## 6. 参考资料

### 6.1 cameo 项目（最相关的参考实现）
- 仓库：`/Users/a000/CodeSpace/cameo`
- 关键文件：
  - `src/components/ChatPanel.tsx` — 聊天 UI 设计
  - `src/components/Composer.tsx` — 输入框 + 附件 + 技能
  - `src/components/Sidebar.tsx` — 项目文件夹侧栏
  - `src/components/StreamingStatus.tsx` — 流式状态
  - `src-tauri/src/codex.rs` — Codex app-server 调用实现
  - `src-tauri/src/skills.rs` — Skills 软链接机制
  - `src-tauri/src/session.rs` — 会话管理（**我们不抄这个**，用 Codex 自己的）

### 6.2 Codex CLI
- 安装：`npm install -g @openai/codex`
- 验证：`codex --version`
- 长连接：`codex app-server`（JSON-RPC over stdio）
- 会话存档：`~/.codex/sessions/<年>/<月>/<日>/rollout-<ISO时间>-<uuid>.jsonl`
- Skills：`~/.codex/skills/.system/` + `<cwd>/.agents/skills/`

### 6.3 本项目 Infinite-Canvas
- 后端：`main.py`（16255 行 FastAPI 单文件）
- 前端：`static/canvas.js` + `static/smart-canvas.js`（vanilla JS）
- 现有 codex 集成：`run_codex_cli()`（4116 行附近，走 `codex exec`）
- 已有静态服务：`/api/view` 可参考

---

## 7. 下一步

**当前正在做：阶段 1（最小可运行版）**

等用户 fork 完 GitHub 仓库 → 配置 git remote → 推送到自己的 fork

---

## 8. 实际进展（2026-07-04 ~ 2026-07-05）

### 8.1 已完成阶段

| 阶段 | commit | 内容 | 验证 |
|---|---|---|---|
| 1 | `60bc1dd` | 3 个状态端点（status / sessions/list / file/view） | ✅ curl 200 |
| 2 | `e9254ef` | CodexAppServerSession 完整实现（spawn / JSON-RPC / SSE） + board/open + turn + close | ✅ GPT-5.5 流式回显 |
| 3.1 | `3f5f7d4` | 侧栏 UI 极简版（误装到 gpt-chat.html） | ❌ |
| 3.2 | `160c51a` | 移回 canvas.html + smart-canvas.html | ✅ |
| 3.3 | `05e2555` | UI 修复（主题色 + 按钮缩小 + 侧栏下移）+ 历史回放 | ✅ |
| 3.4 | `88cffb9` | 附件功能（画布节点读取 + URL 粘入 + base64 data URL 转换） | ✅ GPT-5.5 看到 logo.png 描述 "Minimal white dot" |
| 3.5 | `fffa337` | 选项目自动 replay 最新历史 | ✅ |
| 3.6 | `a717dce` | 深色模式显式样式（不依赖 CSS 变量继承） | ✅ |
| 3.7 | `6794d86` | alert 改 inline 提示 | ✅ |

**8 个 commit，全部已推送到** `git@github.com:xiao7477/Infinite-Canvas.git` **的** `feature/codex-agent` **分支**。

### 8.2 当前可用的能力

- ✅ 选项目（自动接上次对话）
- ✅ 跟 Codex 流式聊天（thinking / agentMessage / tool / image 块渲染）
- ✅ 选画布图节点 / 粘 URL → Codex 看图（base64 data URL）
- ✅ 切历史会话（从 `~/.codex/sessions/` 读 jsonl 回放）
- ✅ 白天/黑夜主题（侧栏正确跟随）
- ✅ 新对话 / 关闭 session

### 8.3 关键技术发现

**Codex app-server 协议**（实测 codex 0.142.5）：

1. **image item 用 `url` 字段，不是 `path`**
   ```json
   {"type": "image", "url": "..."}  // ✅
   {"type": "image", "path": "..."}  // ❌ 报错 missing field url
   ```

2. **url 必须是 inline base64 data URL，**不**接受 `file://` 或 HTTP URL**
   ```
   ❌ file:///path/to/img.png      → "Invalid 'image_url'"
   ❌ http://127.0.0.1:3000/...    → "remote image URLs are not supported; use an inline data URL instead"
   ✅ data:image/png;base64,iVBORw...  → 成功
   ```

3. **后端 helper**：`_to_inline_data_url(url, client)` 自动把 `file://` / 本地路径 / HTTP URL 转 base64 data URL

4. **Codex 桌面客户端把 thinking 也当 `agent_message` 发出**（不是单独的 reasoning 事件）—— 切历史时会看到"我会用一下...技能"这种声明

### 8.4 关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 会话存档 | **不发明，用 Codex 自己的** `~/.codex/sessions/` | 用户偏好"和工具本身一致"；保证 Codex 软件和画布的聊天连续 |
| Codex 进程 | **每个项目一个 `codex app-server` 子进程** | 会话独立；切项目 = 换进程 |
| UI 接入 | **追加新文件 + 改 2 个画布 html 末尾** | 不动 canvas.js / smart-canvas.js；原作者更新冲突接近 0 |
| 深色模式 | **显式 `body.theme-dark` 覆盖** | 之前用 `var(--card, #fff)` fallback，深色下白底漏出 |
| 画布联动 | **从 DOM 读 `.node.selected` 里 `<img>`** | 不动画布代码；用 `getAttribute('data-id')` 反查 |

### 8.5 当前文件清单（实际）

| 文件 | 行数 | 状态 |
|---|---|---|
| `main.py` | 16255 → 16897 (+642) | 末尾追加 codex-agent 模块 |
| `static/canvas.html` | 356 → 359 (+3) | 末尾加 `<link>` + `<script>` |
| `static/smart-canvas.html` | 442 → 445 (+3) | 同上 |
| `static/css/codex-agent-panel.css` | 391（新建） | 完整样式 + dark 显式覆盖 |
| `static/js/codex-agent-panel.js` | 651（新建） | 完整 IIFE |
| `docs/agent-mode-design.md` | 261 → ~400（本文档） | 规划 + 实际进展 |

**未动**：canvas.js (14552) / smart-canvas.js (16797) / gpt-chat.html (1658) / 其他 12 个 .html / data/ / requirements.txt

### 8.6 跟原作者更新的兼容性

| 改动 | 冲突概率 |
|---|---|
| main.py 末尾追加 642 行 | 极低 |
| canvas.html / smart-canvas.html 末尾 +3 行 | 极低 |
| 新建 2 个独立文件 | 0 |
| 改 docs/agent-mode-design.md | 低 |

预计 merge upstream 平均每月 1-2 次，冲突处理 < 5 分钟。

### 8.7 下一步可推

1. **A. 让 Codex 在项目目录生图**
   - 后端 turn 端点已支持 image block 渲染
   - 需要测 `$imagegen` skill 触发 + 解析 imageGeneration item
   - 然后调用 main.py 现有 addImageNode API 把图加到画布

2. **B. 技能列表**
   - 读 `~/.codex/skills/.system/` + `<project>/.agents/skills/`
   - 加 `/api/codex-agent/skills/list` 端点
   - 前端 Composer 加"⚡ 技能"按钮

3. **C. 演示 merge upstream 流程**
   - `git fetch upstream && git merge upstream/main`
   - 验证冲突少 + 容易解决

4. **D. 拖拽调整侧栏宽度**（UX 改进）

### 8.8 已知限制

- Codex image 必须 inline base64（最大文件大小受 OpenAI API 限制，~20MB）
- Codex 桌面客户端把 thinking 当 agentMessage 发出，前端区分困难
- 画布联动只能读选中节点（不能调 addImageNode 添加节点，因为 canvas.js 没暴露全局 API）