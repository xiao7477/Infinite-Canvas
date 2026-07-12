# Infinite Canvas Agent Mode

> Last updated: 2026-07-08
> Branch: `feature/codex-agent`
> Goal: add a Codex Agent panel for smart canvas workflows while keeping upstream updates easy to merge.

## Design Rules

- Project-level execution rules live in the repository root `AGENTS.md`. Future Codex work should read and follow that file first.
- Keep Agent features isolated. Prefer `static/js/codex-agent-panel.js`, `static/css/codex-agent-panel.css`, and the `/api/codex-agent/*` backend routes.
- Do not rewrite original canvas interaction code for Agent work. If smart canvas integration is necessary, add narrow bridge functions on `window.SmartCanvasAgentApi`.
- Agent layout, placement, rename, move, and arrange behavior must remain in `smartAgent*` helpers or `SmartCanvasAgentApi`; do not modify original canvas layout/interaction code for these features.
- Ordinary canvas is out of scope for now. Agent mode should appear only in smart canvas.
- Do not store temporary Agent reference files in the user project directory. Use the system temp cache by default.
- Treat selected chat attachments as ordered refs: `图1/ref_1`, `图2/ref_2`, and so on.
- Canvas Agent requests are canvas-first. When the user asks to generate images/videos in the Agent panel, default to creating smart canvas generation nodes.
- For ambiguous, expensive, or slow tasks, ask for confirmation first, especially video generation and large batch generation.
- Do not expose internal `canvas_agent_action`, `<canvas_agent_context>`, provider lists, or skill dumps in chat history UI.
- Keep markdown rendering readable. Code blocks should be copyable, collapsible, wrap lines, and support expanded viewing.
- When changing shared smart canvas behavior, verify that node select, node drag, canvas pan, wheel zoom, and shortcut `Z` still behave like upstream.

## Current Architecture

### Backend

Main integration lives in `main.py`:

- `POST /api/codex-agent/board/open`
- `POST /api/codex-agent/board/close`
- `POST /api/codex-agent/turn`
- `POST /api/codex-agent/turn/background`
- `GET /api/codex-agent/turn/status`
- `GET /api/codex-agent/turn/active`
- `POST /api/codex-agent/turn/stop`
- `GET /api/codex-agent/panel-state`
- `GET /api/codex-agent/panel-state/latest`
- `POST /api/codex-agent/panel-state`
- `GET /api/codex-agent/history/projects`
- `GET /api/codex-agent/history/conversations`
- `GET /api/codex-agent/history/conversation`
- `GET /api/codex-agent/history/latest`
- `GET /api/codex-agent/threads/replay`
- `GET /api/codex-agent/sessions/list`
- `GET /api/codex-agent/status`
- `GET /api/codex-agent/file/view`
- `POST /api/codex-agent/preferences/remember`
- `POST /api/codex-agent/tools/canvas`

The backend starts `codex app-server` with `cwd = project_dir`, prepares selected canvas refs as local temp files, and injects a hidden `<canvas_agent_context>`. The product path is now a single App Server Agent line: Canvas Agent APIs talk to an Agent service/runtime layer, which wraps `CodexAppServerRuntime` over JSON-RPC stdio. The original `/turn` route still streams directly to the panel for compatibility, while `/turn/background` creates an in-process Agent task with an event log so the browser can refresh or close without killing the turn.

Runtime identity is not just `project_dir`. Active runtime sessions are keyed by the current canvas, Canvas Agent conversation, project directory, and bottom Codex thread pointer when needed. `codex_thread_id` is only an execution pointer; if App Server cannot resume it, the backend creates a fresh Codex thread, keeps the same Canvas Agent conversation visible, and emits a warning block instead of losing history.

Canvas Tools use App Server native experimental `dynamicTools` as their primary path. At `thread/start`/`thread/resume`, the backend registers the `infinite_canvas` namespace and handles App Server's server-initiated `item/tool/call` requests with structured results. Registered queries are `get_selected_nodes`, `get_viewport_nodes`, `get_canvas_summary`, `search_canvas_nodes`, `get_node_detail`, and `get_connected_nodes`; mutations cover media/prompt/text/loop/generation-node creation, rename, move, arrange, group, ungroup, and explicit preference storage. `generate_images` and `generate_videos` submit provider tasks from the backend and persist task ids on the smart-canvas nodes; `create_*_generation_nodes` remains the node-only alternative.

Generation provider names are resolved strictly before a task is queued: `Gemini`/`Antigravity`/`agy` resolve to the configured `gemini-cli` provider, and `GPT`/`OpenAI`/`Codex` resolve to `codex`. An unknown explicit provider is an error, never a silent fallback to the current primary provider (for example RunningHub). Agent image/video requests must use the registered Canvas generation tools, not App Server's own `imageGeneration`/`$imagegen`; if such a side-effect still arrives after a Canvas task was submitted, it is not added as a duplicate canvas result. The Canvas task count is exact: each requested image keeps at most one primary output.

The composer’s reference rail accepts selected image, video, and `smart-prompt` nodes. It is hidden until references exist, then provides a single horizontally scrollable row with add/clear controls. Media references are prepared as temporary local files for App Server image input; prompt-node references remain ordered text context and are never downloaded as media. The rail preserves order and `@图N` numbering and focuses the source node on double click. A send creates a transient in-message status line (`发送中` → `思考中` → `运行中`) which disappears as soon as the turn completes, fails, or stops.

Tool schemas are intentionally narrow: creation accepts bounded item batches and explicit generation fields, target operations accept only node/ref targets and known move/rename fields, and layout options are enumerated. Unknown fields are rejected by the native Tool schema instead of being interpreted as a broad canvas operation.

The current App Server dynamic-tools interface is experimental, so a runtime retries without `dynamicTools` only when registration itself is unsupported. That fallback alone enables the older hidden `canvas_agent_tool` / `canvas_agent_action` JSON protocol; a native runtime never parses those blocks, preventing duplicate execution. Missing or ambiguous query targets return structured errors rather than empty successful results.

Agent-created generation tasks are recorded in `history.sqlite` with the local task id, provider task id when available, canvas/node linkage, request payload, status, result, and error. They survive refreshes and service restarts. On startup, tasks with a durable provider task id resume polling automatically; a request that was interrupted before its provider exposed a reusable id is marked `interrupted` rather than being silently re-submitted and potentially charged twice, and can be retried through a Tool.

Generation queue Tools are `get_generation_queue`, `get_generation_task`, `cancel_generation_task`, and `retry_generation_task`. Cancellation is only allowed while a job is still locally queued. Once it has entered provider execution or has an upstream task id, cancellation is refused and the node remains running; this avoids displaying a false “cancelled” state for work that will still be billed and completed upstream. `delete_nodes` and `undo_last_agent_action` are registered mutation Tools and always require high-risk confirmation, even when the panel is set to automatic execution. Agent mutations store a private pre-action canvas snapshot outside the canvas schema so the most recent Agent action can be restored without changing original node or connection IDs.

For 即梦 CLI, a `get_history_by_ids failed: ret=2008` response during task polling is treated as an upstream-history synchronization delay, not a terminal generation error. The node stays pending and retries with its existing `submit_id`. Terminal CLI errors are normalized for node cards: explicit material/content-review responses become a Chinese review-failure hint; a generic `generation failed` response is shown simply as “生成失败”.

For Agent-created image nodes, an explicit aspect ratio is converted into a concrete image size and also written back to the node's `ratio`/`resolution` display settings. This prevents a requested portrait size such as `9:16` from being submitted as a vertical image while the node still displays or reruns as `1:1`. Provider and model remain the canvas's configured defaults unless the user explicitly selects a different configured model.

Composer references are intentionally not connected by graph edges. For Agent-generated nodes their saved media references (`runInputRefs`) are treated as persistent run configuration and are not removed by detached-input cleanup, so “上游输入” thumbnails remain visible after a canvas refresh.

Deleting a smart node creates a local deletion tombstone until the updated canvas is acknowledged by the server. This prevents an older canvas snapshot or a simultaneous generation-status broadcast from merging a deleted failed node back into the visible canvas before refresh.

Agent panel chat state is persisted outside user project folders. The Canvas Agent now owns its history list instead of using Codex native sessions as the UI source:

```text
~/.codex/infinite-canvas-agent/
  history.sqlite
  panel-history/
```

`history.sqlite` stores projects, canvases, conversations, message blocks, and task metadata. `conversation_id` is the stable Canvas Agent identity; `codex_thread_id` is only an execution pointer and can be replaced when Codex cannot resume. The browser may show a `localStorage` snapshot first for instant paint, but backend history is the source of truth after recovery.

Message history accepts typed UI blocks including `tool_call`, `tool_result`, `canvas_action_result`, `progress`, `node_locator`, `choice`, `parameter_form`, and `media_preview`. Old `tool`, `todo`, `image`, `thinking`, and `error` blocks remain readable.

`/api/codex-agent/history/projects` and `/api/codex-agent/history/conversations` drive the project and conversation lists. Codex rollout files are not used as the primary Canvas Agent history list. `/api/codex-agent/sessions/list` and `/api/codex-agent/threads/replay` are legacy/debug fallbacks only and should not be called by the normal Agent panel restore flow.

Current history UI is scoped to the open canvas:

```text
current canvas -> work directory -> conversation
```

The floating panel top bar keeps the compact layout:

```text
work directory dropdown / new conversation / history dropdown / close
```

The work directory dropdown lists only directories used by the current canvas and always includes `无目录`. Its compact left-aligned icon controls expose add (`+`), directory-management (`−`), and sorting (`↕`) on hover. The sort menu supports `按添加时间排序` and `按最新使用时间排序`. The add control opens an add-workdir dialog with manual absolute-path entry plus server-side preset roots and their direct child folders. Directory-management only hides a directory from the current canvas directory list through `project_visibility`; it never deletes conversations or messages.

The history dropdown has compact hover-labelled Lucide icon controls for `当前` (`folder-open`), `全部` (`folders`), and sorting (`arrow-up-down`). They use the same 24px borderless visual language as the top panel actions. `当前` shows sessions under the selected work directory, while `全部` groups every current-canvas session by work directory; both views support `按添加时间排序` and `按最新使用时间排序`. Selecting a session from another group switches the work directory first, then restores that conversation.

Within an Agent reply, adjacent tool and command events are rendered as a single collapsible summary at their original position in the reply stream (for example, after one paragraph and before the next). Thinking, temporary send status, and cleaned-empty protocol blocks never split an otherwise adjacent tool group. Its one-line count places commands before tools; expanded entries are border cards with a Lucide type icon, status, command/tool name, and output. The summary contains no time or repeated `工具与命令` title. A separate reply-level `已处理 · 耗时` line stays at the top of the reply and reveals the whole-turn completion timestamp only on hover.

Canvas-action result cards are compact, default-expanded `<details>` blocks: the title uses an icon plus a created-node summary such as `已生成 2个生图节点` or `已创建 1个提示词节点，2个生图节点`; the expanded content contains only node locator chips. Backend-hosted generation writes `runStartedAt`, `runFinishedAt`, and `runElapsedMs` to the node so its completed duration remains correct after refresh.

`无目录` is a first-class Canvas Agent mode for chat and canvas-only operations. Its UI/history project key is the empty string, and the backend runs Codex in:

```text
~/.codex/infinite-canvas-agent/no-project-workspace/
```

If the user asks for project file reads/writes while in `无目录`, the Agent should ask them to choose a work directory first.

Workdir presets are stored outside user projects:

```text
~/.codex/infinite-canvas-agent/workdir-presets.json
```

Global canvas Agent preferences live outside project folders:

```text
~/.codex/infinite-canvas-agent/preferences.md
```

Default cache root for selected refs:

```text
<system temp>/infinite-canvas-codex-agent/refs/<project_hash>/
```

Relevant environment variables:

- `CODEX_AGENT_REF_CACHE_DIR`
- `CODEX_AGENT_REF_TTL_HOURS`
- `CODEX_AGENT_PREFS_FILE`

### Frontend

Agent panel:

- `static/js/codex-agent-panel.js`
- `static/css/codex-agent-panel.css`

Smart canvas bridge:

- `window.SmartCanvasAgentApi.getContext()`
- `refreshFromServer()`
- `getSelectedAssets()`
- `getAllAssets()`
- `addMediaNodes()`
- `addPromptNodes()`
- `addLoopNodes()`
- `groupNodes()`
- `ungroupNodes()`
- `generateImageNodes()`
- `generateVideoNodes()`
- `getImageGenerationDefaults()`
- `getVideoGenerationDefaults()`

HTML entry:

- `static/smart-canvas.html` loads the Agent panel.
- `static/canvas.html` should not load the Agent panel.

## Current Capabilities

- Floating smart canvas Agent window with resizable panel.
- Codex app-server streaming chat through the Agent runtime layer.
- Backend-managed Agent turns with `task_id`, pollable event logs, active-task recovery, and stop support.
- Per-canvas panel snapshots in `localStorage` for first paint, backed by server-side panel history so tool/action result blocks survive refresh.
- Session list and replay from Codex's own `~/.codex/sessions/`.
- Ordered selected attachments with thumbnails and `@图N` mentions.
- Image paste into Agent input uploads to canvas and adds a numbered attachment.
- Markdown message rendering, copyable/collapsible code blocks, and expanded code view.
- Stop button for the active Agent turn.
- Hidden `canvas_agent_tool` execution for canvas queries and low-risk operations.
- Hidden `canvas_agent_action` execution for generation and compatibility fallback; background Agent turns execute supported actions on the backend to avoid page-refresh interruption.
- Add image/video/media/prompt/text/loop nodes to smart canvas.
- Rename, move, arrange, group, and ungroup selected/reference smart canvas nodes from the backend with missing-node skips.
- Generate image nodes through the smart canvas API generation flow.
- Generate video nodes through the smart canvas API video generation flow.
- Reference wiring from selected source nodes to generated nodes.
- Background generation: after a task node is created and submitted, the chat turn can finish while the node keeps running.
- Canvas generation logs for Agent-triggered image/video tasks.
- Global short preferences via `remember_preference`.

Backend action support currently covers deterministic canvas edits. Full backend handoff for smart canvas image/video generation is still a follow-up because it must recreate pending-node submission, provider polling, and result writeback outside the browser.

## Important Behavior Notes

- `canvas_agent_action` is an implementation detail. The model may emit it, but the panel hides it from normal chat.
- Generated smart canvas nodes should preserve input refs with `runInputRefs`; the input thumbnail strip should show actual inputs, not the node's own output. When the composer has explicit image/video/prompt references, those values are copied into the new generation node without graph connections; prompt-node text is merged into the node’s editable prompt. When no composer references are present but smart-canvas nodes are selected, new generation nodes are placed in an open area to the right of that selection and receive input connections from it.
- Agent-generated media nodes should be placed near selected/reference nodes when possible. Avoid piling new nodes at the same coordinate.
- Agent grouping should reuse the existing smart group behavior: images are absorbed into the group thumbnail grid; prompt and loop nodes remain group members.
- Backend Agent tasks should treat conflicts as partial success: if a target node, thread, or image index no longer exists, skip that item, report it, and continue applying valid edits.
- Agent placement uses three scopes: `viewport` means the visible smart canvas viewport captured at send time, `global` means the bounds of all canvas content, and `node` means relative to a selected/reference node. `viewport` placement first anchors to the node cluster visible in or near that viewport, then expands outward on the requested side; it should not stick to the viewport corner or fly away from nearby nodes. Backend collision checks should use the real node sizes sent in the front-end context snapshot when available. Later user pan/zoom during Agent thinking must not change that turn's `viewport` placement.
- Restoring a saved panel thread must resume that exact Codex thread. If a project app-server process is already running for a different thread, restart it and call `thread/resume` for the requested thread instead of silently reusing the current process.
- The Agent panel has explicit status states: `booting`, `loading_projects`, `opening_project`, `loading_history`, `reconnecting_task`, `ready`, `busy`, and `error`. During boot/loading/reconnect states, message input, send, project switch, history switch, new session, and attachment controls must stay locked and show a loading hint instead of appearing ready.
- Refresh recovery order is: show local snapshot as a temporary UI, fetch backend latest panel state, open/resume the exact thread, fetch exact backend panel state, reconnect active background task, and only use Codex session replay when no panel state exists.
- Project and history dropdowns must include Agent panel-history entries even when no matching Codex rollout file is available on the current computer.
- If the user explicitly says "directly run", "use defaults", or "no need to ask", the Agent may execute with current defaults.
- If the user asks for a vague video or a large batch without key settings, the Agent should ask one concise confirmation question.

## Agent UX Redesign Plan

The current floating panel is a working Agent control surface, not the final product UI. The target is a canvas-native Agent workspace that feels close to Codex chat while supporting canvas-specific objects, tasks, and decisions.

### Product Direction

- Codex remains the execution engine.
- Canvas Agent owns product history, UI state, canvas task records, and interaction blocks.
- The floating panel is only the first shell. The same history, message blocks, input toolbar, and task UI should be portable into a future integrated canvas UI.
- Visual language should be close to Codex: clean message flow, compact tool blocks, collapsible thinking, clear send/stop states, and a dense input area.
- Canvas Agent should differ where the domain differs: node references, viewport scope, generation settings, batch task progress, node locating, and canvas action confirmations.

### Independent History Model

Move toward a dedicated Canvas Agent history store instead of using Codex sessions as the primary list source:

```text
~/.codex/infinite-canvas-agent/
  history.sqlite
  attachments/
  refs/
  exports/
```

Target entities:

```text
projects
canvases
conversations
messages
tasks
context_snapshots
preferences
```

Important identity rules:

- `conversation_id` is the permanent Canvas Agent conversation id.
- `codex_thread_id` is an execution-thread pointer and may be replaced if Codex cannot resume it.
- Canvas Agent messages must remain visible even if the Codex thread is archived, deleted, or unavailable.
- If `codex_thread_id` resume fails, open the Canvas Agent history normally, show a concise warning, create a new Codex thread, and continue appending to the same Canvas Agent conversation.
- Codex rollout files are fallback transcript data, not the source of truth for Canvas Agent UI.

### Window Information Architecture

Target panel structure:

```text
Top bar
- current project / canvas / conversation
- history
- new conversation
- archive
- settings
- background task status

Message area
- user messages
- assistant replies
- collapsible thinking
- tool calls/results
- canvas action results
- choices/forms
- progress blocks
- node locator blocks

Input area
- text input
- attachments / selected canvas assets
- @ references
- / commands
- model selector
- mode selector
- scope selector
- approval policy selector
- send / stop

Side or popover surfaces
- history list
- task list
- context preview
- settings
```

### Typed Message Blocks

Messages should be stored and rendered as typed blocks instead of plain Markdown-only records. Initial block taxonomy:

```text
text
thinking
tool_call
tool_result
canvas_action_result
choice
parameter_form
progress
node_locator
media_preview
attachment
error
```

Typed blocks enable interaction without parsing prose. Examples:

- `choice`: ask the user to pick model, size, provider, action mode, or confirmation path.
- `parameter_form`: structured image/video generation settings.
- `progress`: batch generation or multi-step canvas operation progress.
- `node_locator`: focus, highlight, or select affected nodes.
- `canvas_action_result`: report changed/skipped nodes and provide follow-up actions.

Current implementation status:

- Backend panel-state compaction preserves this typed block taxonomy.
- Background task events produce `progress` blocks for task start/finish.
- Canvas action execution produces `canvas_action_result` blocks with affected/skipped counts.
- Canvas action results may add `node_locator` blocks; the smart canvas bridge exposes `focusNodes(ids)` for locating affected nodes.
- `choice` and `parameter_form` have passive renderers; wiring them to submit structured answers is a follow-up.

### Input Toolbar

The input area should evolve toward Codex-style density plus canvas-specific controls:

```text
+ / attachment
@ reference
/ command
model
mode
scope
approval policy
send / stop
```

`@` references should resolve canvas objects and scopes:

```text
@图1
@选中节点
@当前视口
@全部画布
@某个节点标题
@某个工作流
```

`/` commands should expose common canvas actions:

```text
/整理
/重命名
/生成提示词
/创建生图节点
/创建视频节点
/总结画布
/定位
/批量处理
```

Mode selector:

```text
聊天
操作画布
生成
整理
分析
```

Scope selector:

```text
选中节点
当前视口
全画布
指定节点附近
```

Approval policy selector:

```text
自动执行
执行前确认
高风险操作确认
```

Current implementation status:

- The floating panel composer has Codex-style toolbar controls for attachment, `@`, `/`, mode, scope, approval policy, and send/stop.
- `@` opens the attachment/reference picker and inserts `@图N` tokens.
- `/` opens a command picker for common canvas tasks such as organize, rename, prompt generation, image/video node creation, summarize, locate, and batch processing.
- Mode/scope/approval buttons currently cycle lightweight state and are included in the turn canvas context as `agentInput`.
- Approval policy applies to canvas write Tools; read-only context queries always run automatically:
  - `自动执行`: run every write Tool immediately, including backend-hosted image/video generation.
  - `高风险确认`: pause video generation, ungrouping, preference storage, and broad (3+ targets or full-canvas) rename/move/arrange/group/image-generation actions. The confirmation includes node count, planned image/video count, and selected provider/model. It does not invent a credit quote; only larger workloads show a “may consume substantial credits” warning. It offers direct task submission, node-only creation, or cancellation.
  - `执行前确认`: pause every write Tool.
- Confirmation blocks persist as typed `choice` blocks and call `/api/codex-agent/action/resolve`. For native Tool calls the pending record also stores the App Server request id; approval or rejection sends the structured Tool result back to the paused turn.
- The selected mode/scope/approval state is kept in the local panel snapshot and sent to backend panel-state.

### Context Engineering

Do not send full canvas context for every user message. Use intent routing and context levels:

```text
Level 0: user message + conversation summary
Level 1: lightweight canvas summary, selection, viewport, counts
Level 2: selected/reference/nearby node details
Level 3: full canvas index/structure for global organization or analysis
```

Routing defaults:

- Plain chat: no detailed canvas context.
- Image or selected asset analysis: send only selected/attached refs.
- Node operation: send selected/reference nodes, viewport snapshot, and nearby collision/layout info.
- Global organization: send a compact full-canvas index and ask for confirmation if expensive.
- Ambiguous complex work: return a `choice` or `parameter_form` block before execution.

Current implementation status:

- The Agent panel builds a `contextProfile` for each turn from input mode, scope, attachments, slash command, and message text.
- The visible input no longer asks users to manually choose "operate canvas" or "current viewport"; context is routed automatically from user intent, attachments, slash command, and selection state.
- The frontend still sends the send-time canvas snapshot to the backend so deterministic actions and collision checks can run safely. The backend also captures the real smart-canvas nodes and connections into `~/.codex/infinite-canvas-agent/context-snapshots/`, while sending only a minimal App Server envelope to Codex.
- Context levels are active:
  - Level 0: pure chat, no node list, no provider list, no canvas coordinates.
  - Level 1: lightweight canvas summary and node counts.
  - Level 2: selected refs plus snapshot IDs; node detail stays in the server snapshot/tool layer.
  - Level 3: global intent marker plus snapshot IDs; full indexes are queried through Canvas Tools instead of prompt text.
- Image/video provider defaults are only sent for generation/model-related intents.
- The backend hidden context includes `context_snapshot_id`, `viewport_snapshot_id`, `context_level`, `context_intent`, selected refs, node counts, and approval policy. It does not include the full node list in prompt text.
- The bottom hint shows automatic context routing instead of manual mode/scope buttons.
- Query tools read that immutable send-time snapshot by default; `search_canvas_nodes` is paginated and bounded. Write tools re-load the live canvas and re-resolve targets before mutation, so changed/deleted targets are skipped safely.
- Native Tool results return directly through the paused App Server request and render as chronological tool blocks; no follow-up turn is required.

### History And Archive UX

Canvas Agent history should be a first-class UI, separate from Codex history:

```text
最近对话
按项目
按画布
归档
无目录对话
搜索
任务未完成
```

Each history item should show:

```text
title
project
canvas
last message preview
task status
updated time
archive state
```

Required operations:

```text
new conversation
rename
archive
unarchive
delete
search
restore
```

Directory management should not depend on browser filesystem access. The browser should show server-side registered/recent directories and allow manual server-path entry. Support a `no project` conversation mode for pure chat and canvas-only tasks that do not need file operations.

### Task UX

Long-running or multi-step work needs UI outside plain chat text:

- Top-level lightweight task status in the panel.
- Task list for running/completed/failed/retryable work.
- `progress` blocks inside chat for the relevant turn.
- Stop/retry controls for task blocks where safe.
- Persist task state in Canvas Agent history so refresh does not erase progress.

### Visual Direction

Match Codex where it helps familiarity:

- dark, restrained panel
- clean message flow
- compact markdown
- collapsible thinking
- tool blocks with running/done/error state
- input toolbar with dense controls

Differentiate canvas-specific blocks:

- canvas action results: blue action cards
- risky operations: orange/red confirmation cards
- node locator: compact card with focus/highlight/select controls
- batch work: progress bar and counts
- generation setup: parameter form card

### Implementation Phases

1. Build independent `history.sqlite` and stop using Codex sessions as the primary Canvas Agent history list.
2. Convert panel messages to typed blocks and persist them through the new history store.
3. Redesign message rendering toward Codex-style blocks plus canvas-specific block renderers.
4. Rebuild input area with `@`, `/`, mode, scope, approval, attachment, send/stop controls.
5. Add intent routing and context levels so canvas data is sent on demand.
6. Add history sidebar/popover with project/canvas/archive/search support.
7. Add task list/progress UI and refresh-safe task recovery.
8. Keep the floating panel as the current shell, but ensure the inner UX modules can move into a future integrated canvas UI.

### UX Acceptance Criteria

- Refresh restores the visible conversation, tool/action results, scroll state, and task status.
- Codex thread archive/delete does not remove Canvas Agent history from the list.
- If Codex resume fails, old messages remain visible and a new thread continues the same Canvas Agent conversation.
- Project and conversation lists load quickly without recursive scanning of Codex rollout files.
- `@` can reference selected nodes, current viewport, and attached refs.
- `/` exposes common canvas tasks.
- Agent can answer with `choice` and `parameter_form` blocks when user intent is underspecified.
- Canvas-created nodes can be focused/highlighted from a `node_locator` block.
- Complex tasks show progress and can be stopped or retried where supported.
- The UI feels close to Codex chat while preserving canvas-specific controls.

## Upstream Compatibility

This fork should continue to accept upstream updates from `hero8152/Infinite-Canvas`.

Preferred change order:

1. Add behavior in Agent panel files.
2. Add backend behavior in `/api/codex-agent/*`.
3. Add small smart canvas bridge functions only when the Agent must read/write canvas state.
4. Avoid broad refactors of `static/js/smart-canvas.js`.
5. Never change base canvas interactions for Agent convenience.

Files most likely to conflict with upstream:

- `main.py`
- `static/js/smart-canvas.js`
- `static/smart-canvas.html`
- `static/canvas.html`

Files designed to avoid upstream conflict:

- `static/js/codex-agent-panel.js`
- `static/css/codex-agent-panel.css`
- `docs/agent-mode-design.md`

## Known Issue: Canvas View Scale

Observed on 2026-07-06:

- Smart canvas can reach an extreme zoom value through repeated zooming.
- Pressing `Z` enters overview/zoom preview, but it does not reset the underlying saved viewport scale.
- After clicking or dragging, the canvas can return to the previous extreme scale, making nodes appear missing.
- In one case, adding a node while the viewport scale was extremely tiny produced huge world coordinates.

This appears to be a base smart canvas behavior, not Agent-specific.

Recommended improvement, not yet implemented:

- Add two small buttons to the minimap / lower-right view controls:
  - Overview button: same behavior as shortcut `Z`.
  - Reset view button: reset viewport to a sane scale and center all nodes.
- Add a safe clamp only at intentional view reset/save boundaries, not inside low-level mouse interaction paths.
- Preserve original drag, select, pan, and wheel behavior.

## Known Issue: Generation Task Recovery

Observed on 2026-07-06:

- Image generation uses a backend task ID path and can recover after refresh/reopen.
- Video generation, including manual smart canvas runs and Agent-created runs, historically used a long `/api/canvas-video` request.
- If the page is refreshed or closed before that long request returns a final media result or a provider pending ID, the canvas may only save an empty pending node shell.
- The node timer can later reappear because `runStartedAt` was saved, but result recovery is unreliable without a persisted task ID.

This is a smart canvas video task lifecycle issue, broader than the Agent panel.

Recommended improvement:

- Move video generation to a backend task ID flow, matching image generation.
- Persist `pendingTasks` for video nodes immediately after node creation.
- Let refresh/reopen resume video polling through the saved backend task ID.
- Agent-created video nodes can use this path first; applying it to manual video runs changes broader smart canvas behavior and should be handled deliberately.

## Upstream Watchlist

These look like base/original smart canvas issues. Keep them recorded and check upstream updates before fixing them locally.

- Video task recovery: manual smart canvas video generation can lose result fill-back after refresh/close because it does not persist a backend task ID before the long `/api/canvas-video` request completes.
- View scale reset: shortcut `Z` shows an overview but does not reset the underlying viewport scale, so canvas interaction can jump back to an extreme zoom level.
- If upstream does not fix these, consider local fixes in separate, clearly scoped commits that avoid changing unrelated Agent code.

## Backlog

### Agent Workflow

- Improve placement: generated nodes should appear to the right of referenced nodes and search for nearby empty space.
- Add optional wait mode for complex tasks where the user asks the Agent to inspect outputs after generation.
- Add task/progress panel for multi-step or batch canvas workflows.
- Support batch workflows: create N generation nodes, prompt nodes, and video nodes with references wired correctly.
- Support richer video defaults and clarification prompts based on available providers.

### Canvas Bridge

- Keep reference edges as first-class context for Agent-created nodes.
- Ensure pasted/attached media never duplicates canvas assets.
- Support robust thumbnail generation for video refs and historical replay.
- Add safe, narrow APIs for node positioning and connection creation instead of editing internals directly.
- Improve Agent-only node arrangement modes, but keep them isolated from upstream/original layout logic.

### UX

- Keep Agent panel history visually consistent with live chat.
- Continue hiding internal contexts, provider hints, and raw action JSON.
- Keep refresh recovery visually stable: restore the panel snapshot first, then reconnect background tasks instead of replacing the panel with lossy Codex replay.
- Keep code blocks compact by default with copy and expand controls.
- Keep the busy state clear with elapsed time and stop control.

### Documentation

- Add a short user guide for Agent canvas workflows.
- Document how to update from upstream without overwriting Agent files.
- Document local cache locations and cleanup behavior.

## Git Notes

Current fork remote:

```text
origin   git@github.com:xiao7477/Infinite-Canvas.git
upstream https://github.com/hero8152/Infinite-Canvas.git
```

Main Agent branch:

```text
feature/codex-agent
```

Recent relevant commit:

```text
630360d feat(agent): improve canvas generation workflow
```
