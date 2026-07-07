# Infinite Canvas Agent Mode

> Last updated: 2026-07-06
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
- `GET /api/codex-agent/threads/replay`
- `GET /api/codex-agent/sessions/list`
- `GET /api/codex-agent/status`
- `GET /api/codex-agent/file/view`
- `POST /api/codex-agent/preferences/remember`

The backend starts `codex app-server` with `cwd = project_dir`, streams events to the panel, prepares selected canvas refs as local temp files, and injects a hidden `<canvas_agent_context>`.

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
- Codex app-server streaming chat.
- Session list and replay from Codex's own `~/.codex/sessions/`.
- Ordered selected attachments with thumbnails and `@图N` mentions.
- Image paste into Agent input uploads to canvas and adds a numbered attachment.
- Markdown message rendering, copyable/collapsible code blocks, and expanded code view.
- Stop button for the active Agent turn.
- Hidden `canvas_agent_action` execution.
- Add image/video/media/prompt/text/loop nodes to smart canvas.
- Group and ungroup selected/reference smart canvas nodes through the existing smart group system.
- Generate image nodes through the smart canvas API generation flow.
- Generate video nodes through the smart canvas API video generation flow.
- Reference wiring from selected source nodes to generated nodes.
- Background generation: after a task node is created and submitted, the chat turn can finish while the node keeps running.
- Canvas generation logs for Agent-triggered image/video tasks.
- Global short preferences via `remember_preference`.

## Important Behavior Notes

- `canvas_agent_action` is an implementation detail. The model may emit it, but the panel hides it from normal chat.
- Generated smart canvas nodes should preserve input refs with `runInputRefs`; the input thumbnail strip should show actual inputs, not the node's own output.
- Agent-generated media nodes should be placed near selected/reference nodes when possible. Avoid piling new nodes at the same coordinate.
- Agent grouping should reuse the existing smart group behavior: images are absorbed into the group thumbnail grid; prompt and loop nodes remain group members.
- If the user explicitly says "directly run", "use defaults", or "no need to ask", the Agent may execute with current defaults.
- If the user asks for a vague video or a large batch without key settings, the Agent should ask one concise confirmation question.

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
