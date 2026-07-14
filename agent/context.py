"""Canvas Agent 的最小 App Server 上下文 envelope。"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional


def _one_line(value: Any) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()


def build_minimal_context_envelope(
    *,
    metadata: Mapping[str, Any],
    refs: List[Mapping[str, Any]],
    preferences: str = "",
    skill: Optional[Mapping[str, str]] = None,
    native_tools_enabled: bool = True,
    no_project_rule: str = "",
) -> str:
    """
    构建只包含本轮引用的 envelope。

    工具列表由 Dynamic Tool schema 提供；专项流程只从当前命中的 Canvas Skill 注入。
    """
    level = max(0, min(3, int(metadata.get("context_level") or 0)))
    active_skill = dict(skill or {})
    skill_id = _one_line(active_skill.get("id"))
    lines = [
        "<canvas_agent_context>",
        "context_protocol: app_server_minimal_envelope_v2",
        f"project_dir: {_one_line(metadata.get('project_dir')) or '无目录'}",
        f"canvas_id: {_one_line(metadata.get('canvas_id'))}",
        f"conversation_id: {_one_line(metadata.get('conversation_id'))}",
        f"context_snapshot_id: {_one_line(metadata.get('context_snapshot_id'))}",
        f"viewport_snapshot_id: {_one_line(metadata.get('viewport_snapshot_id'))}",
        f"canvas_revision: {int(metadata.get('canvas_revision') or 0)}",
        f"captured_at: {_one_line(metadata.get('captured_at'))}",
        f"context_level: {level}",
        f"context_intent: {_one_line(metadata.get('context_intent')) or 'chat'}",
        f"context_command: {_one_line(metadata.get('context_command'))}",
        f"active_canvas_skill: {skill_id or '(none)'}",
        f"agent_approval_policy: {_one_line(metadata.get('approval_policy'))}",
        "你在 Infinite Canvas 画布 Agent 中；画布事实按需通过 infinite_canvas 工具查询，不要猜测。",
        "所有画布写入只通过 infinite_canvas 工具；不展示内部节点 ID、快照 ID 或工具协议。",
        "发送后的左/右/当前视口语义以 viewport_snapshot_id 对应快照为准。",
    ]
    if not native_tools_enabled:
        lines.extend([
            "native_tools: unavailable",
            "兼容模式下查询才输出隐藏 canvas_agent_tool JSON，写操作才输出隐藏 canvas_agent_action JSON。",
        ])
    if no_project_rule:
        lines.append(no_project_rule)

    total_nodes = metadata.get("total_nodes")
    selected_nodes = metadata.get("selected_nodes")
    if total_nodes is not None or selected_nodes:
        lines.extend([
            "canvas_counts:",
            f"  total_nodes: {int(total_nodes or 0)}",
            f"  selected_nodes: {int(selected_nodes or 0)}",
        ])

    lines.append("selected_refs:")
    if not refs:
        lines.append("(none)")
    for index, ref in enumerate(refs, 1):
        ref_id = _one_line(ref.get("refId") or ref.get("ref_id") or f"ref_{index}")
        kind = _one_line(ref.get("kind")) or "image"
        local_path = _one_line(ref.get("local_path"))
        lines.extend([
            f"- id: {ref_id}",
            f"  name: {_one_line(ref.get('name')) or os.path.basename(local_path or 'asset')}",
            f"  kind: {kind}",
            f"  local_path: {local_path}",
            f"  source_path: {_one_line(ref.get('source_path'))}",
            f"  canvas_kind: {_one_line(ref.get('canvasKind') or ref.get('canvas_kind'))}",
            f"  node_id: {_one_line(ref.get('nodeId') or ref.get('node_id'))}",
            f"  image_index: {_one_line(ref.get('imageIndex') if ref.get('imageIndex') is not None else '')}",
        ])
        if kind == "prompt":
            prompt_text = str(ref.get("text") or "")[:4000].strip()
            if prompt_text:
                lines.append(f"  prompt_text: {prompt_text}")

    instructions = str(active_skill.get("instructions") or "").strip()
    if skill_id and instructions:
        lines.extend(["<canvas_skill>", f"id: {skill_id}", instructions, "</canvas_skill>"])

    clean_preferences = str(preferences or "").strip()
    if clean_preferences and (level > 0 or skill_id):
        lines.extend(["user_canvas_preferences:", clean_preferences])
    lines.append("</canvas_agent_context>")
    return "\n".join(line for line in lines if line is not None and line != "")
