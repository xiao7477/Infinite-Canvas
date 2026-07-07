# Infinite Canvas Agent Work Rules

This repository is a fork of Infinite Canvas with a custom Codex Agent mode.
Keep upstream compatibility as the main engineering constraint.

## Scope

- Agent mode is currently for smart canvas only.
- Ordinary canvas should not show or depend on Agent mode.
- Agent-specific frontend work should live in:
  - `static/js/codex-agent-panel.js`
  - `static/css/codex-agent-panel.css`
- Agent-specific backend work should stay under `/api/codex-agent/*` in `main.py`.
- Smart canvas integration should be exposed through narrow `window.SmartCanvasAgentApi` bridge methods.

## Do Not Touch For Agent Work

- Do not rewrite original mouse interaction logic for select, drag, pan, zoom, or shortcut `Z`.
- Do not change original canvas save/load schemas unless the user explicitly approves.
- Do not alter original node IDs, connection IDs, or connection data shape.
- Do not change real asset file paths or rename real files for Agent naming tasks.
- Do not put temporary Agent reference files in user project folders.

## Smart Canvas Bridge Rules

- Prefix Agent bridge helpers with `smartAgent`.
- Agent layout, placement, rename, move, and arrange behavior must stay in `smartAgent*` helpers or `SmartCanvasAgentApi`.
- If Agent needs to move or arrange nodes, only write `node.x` and `node.y`.
- If Agent needs to rename a canvas item, default to changing `node.images[index].name` only.
- Only change `node.title` when the user explicitly asks to change source node/title.
- Internal node IDs such as `smart_xxx`, `prompt_xxx`, and `loop_xxx` are read-only identifiers for hidden actions; do not expose them in user-facing text.

## Chat And Context Rules

- Treat selected attachments as ordered refs: `图1/ref_1`, `图2/ref_2`, and so on.
- Hide `<canvas_agent_context>`, `canvas_agent_action`, provider lists, skill dumps, and internal IDs from normal chat UI.
- For vague or expensive tasks, ask one concise confirmation question first.
- If the user says "directly run", "use defaults", or equivalent, execute with current canvas defaults.

## Verification

After Agent changes, at minimum check:

- `node --check static/js/codex-agent-panel.js` when that file changes.
- `node --check static/js/smart-canvas.js` when that file changes.
- `python -m py_compile main.py` when backend code changes.
- If shared smart canvas code is touched, manually verify select, drag, pan, zoom, and `Z`.

## Documentation

- Keep `docs/agent-mode-design.md` updated with current Agent behavior and known base-canvas issues.
- Base/original author bugs should be documented first and fixed later only with explicit user approval.
