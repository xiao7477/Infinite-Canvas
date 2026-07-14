"""Canvas Agent 专用 Skill 与斜杠命令注册层。

这些 Skill 由 Infinite Canvas 按需加载，不注册成用户的全局 Codex Skills。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


SKILLS_ROOT = Path(__file__).resolve().parent / "skills"


_COMMANDS: List[Dict[str, Any]] = [
    {
        "id": "organize",
        "command": "/整理",
        "aliases": [],
        "title": "整理",
        "description": "移动、统一尺寸、横纵宫格或整理完整节点树",
        "skill": "infinite-canvas-organize",
        "intent": "canvas_operation",
        "context_level": 2,
        "default_scope": "viewport",
        "generation_context": False,
        "risk": "write",
    },
    {
        "id": "rename",
        "command": "/重命名",
        "aliases": [],
        "title": "重命名",
        "description": "按规则重命名节点素材显示名",
        "skill": "infinite-canvas-asset-naming",
        "intent": "canvas_operation",
        "context_level": 2,
        "default_scope": "selected",
        "generation_context": False,
        "risk": "write",
    },
    {
        "id": "prompt",
        "command": "/生成提示词",
        "aliases": [],
        "title": "生成提示词",
        "description": "把参考素材或主题写成提示词节点",
        "skill": "infinite-canvas-prompt-workflow",
        "intent": "asset_analysis",
        "context_level": 2,
        "default_scope": "selected",
        "generation_context": False,
        "risk": "write",
    },
    {
        "id": "image",
        "command": "/创建生图节点",
        "aliases": [],
        "title": "创建生图节点",
        "description": "创建智能画布生图节点",
        "skill": "infinite-canvas-generation",
        "intent": "generation",
        "context_level": 2,
        "default_scope": "selected",
        "generation_context": True,
        "risk": "write",
    },
    {
        "id": "video",
        "command": "/创建视频节点",
        "aliases": [],
        "title": "创建视频节点",
        "description": "创建智能画布视频生成节点",
        "skill": "infinite-canvas-generation",
        "intent": "generation",
        "context_level": 2,
        "default_scope": "selected",
        "generation_context": True,
        "risk": "write",
    },
    {
        "id": "summarize",
        "command": "/总结画布",
        "aliases": [],
        "title": "总结画布",
        "description": "总结当前视口或全画布内容",
        "skill": "infinite-canvas-analysis",
        "intent": "global_canvas",
        "context_level": 3,
        "default_scope": "canvas",
        "generation_context": False,
        "risk": "read",
    },
    {
        "id": "locate",
        "command": "/定位",
        "aliases": [],
        "title": "定位",
        "description": "定位、选中或高亮相关节点",
        "skill": "infinite-canvas-analysis",
        "intent": "canvas_operation",
        "context_level": 2,
        "default_scope": "viewport",
        "generation_context": False,
        "risk": "read",
    },
    {
        "id": "batch",
        "command": "/批量任务",
        "aliases": ["/批量处理"],
        "title": "批量任务",
        "description": "使用现有画布工具规划并执行多步任务",
        "skill": "infinite-canvas-batch-planning",
        "intent": "global_canvas",
        "context_level": 3,
        "default_scope": "canvas",
        "generation_context": True,
        "risk": "expensive",
        "task_node_available": False,
    },
]


_SKILL_KEYWORDS = (
    ("infinite-canvas-generation", re.compile(r"生图|生(?:成)?(?:一|两|几|\d+)?张?图|生成图片|画一张|视频|图生视频|模型|provider", re.I)),
    ("infinite-canvas-asset-naming", re.compile(r"重命名|改名|命名规则|素材名", re.I)),
    ("infinite-canvas-organize", re.compile(r"整理|排列|布局|分组|取消分组|移动节点", re.I)),
    ("infinite-canvas-prompt-workflow", re.compile(r"提示词|prompt|反推|拆镜", re.I)),
    ("infinite-canvas-analysis", re.compile(r"总结画布|定位|找到|上下游|连接关系", re.I)),
)


def public_commands() -> List[Dict[str, Any]]:
    """返回前端可见的命令注册表副本。"""
    return [dict(item) for item in _COMMANDS]


def _command_tokens(item: Mapping[str, Any]) -> List[str]:
    return [str(item.get("command") or ""), *[str(alias) for alias in item.get("aliases") or []]]


def resolve_command(value: str) -> Optional[Dict[str, Any]]:
    """从命令名或用户文本中解析首个已注册命令。"""
    text = str(value or "").strip()
    if not text:
        return None
    direct = text.split(None, 1)[0]
    for item in _COMMANDS:
        if direct in _command_tokens(item):
            return dict(item)
    match = re.search(r"(?:^|\s)(/[^\s/]+)", text)
    if not match:
        return None
    token = match.group(1)
    for item in _COMMANDS:
        if token in _command_tokens(item):
            return dict(item)
    return None


def normalize_context_profile(
    text: str,
    requested: Optional[Mapping[str, Any]] = None,
    attachment_count: int = 0,
) -> Dict[str, Any]:
    """在服务端校验前端路由，并以命令注册表为优先真值源。"""
    source = dict(requested or {})
    command = resolve_command(str(source.get("command") or "")) or resolve_command(text)
    try:
        level = max(0, min(3, int(source.get("level", 2 if attachment_count else 0))))
    except (TypeError, ValueError):
        level = 2 if attachment_count else 0
    intent = str(source.get("intent") or "chat")
    scope = str(source.get("scope") or "auto")
    mode = str(source.get("mode") or "auto")
    generation_context = bool(source.get("generationContext") or source.get("generation_context"))

    if command:
        level = int(command.get("context_level", level))
        intent = str(command.get("intent") or intent)
        generation_context = bool(command.get("generation_context"))
        source["command"] = command.get("command")
        source["command_id"] = command.get("id")
    else:
        raw = str(text or "")
        if re.search(r"全画布|整个画布|所有节点|全部节点|总览|版图|整理全部", raw, re.I):
            level, intent = 3, "global_canvas"
        elif re.search(r"生图|生(?:成)?(?:一|两|几|\d+)?张?图|生成图片|画一张|视频|图生视频|模型|provider", raw, re.I):
            level, intent, generation_context = max(level, 2), "generation", True
        elif re.search(r"当前|视口|选中|左边|右边|附近|移动|整理|重命名|分组|节点|画布", raw, re.I):
            level, intent = max(level, 2), "canvas_operation"
        elif attachment_count or re.search(r"这张图|图片|素材|分析|描述|读取", raw, re.I):
            level, intent = max(level, 2), "asset_analysis"

    if scope == "canvas":
        level = 3
    elif scope in {"selected", "node"}:
        level = max(level, 2)
    if mode == "chat" and not attachment_count and not command:
        level, intent = 0, "chat"

    source.update({
        "level": level,
        "label": {0: "纯聊天", 1: "轻量画布", 2: "局部节点", 3: "全画布"}.get(level, "局部节点"),
        "intent": intent,
        "mode": mode,
        "scope": scope,
        "attachmentCount": max(0, int(attachment_count or 0)),
        "generationContext": generation_context,
    })
    return source


def resolve_skill(text: str, profile: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    """只为当前任务选择一个主 Canvas Skill。"""
    source = dict(profile or {})
    command = resolve_command(str(source.get("command") or "")) or resolve_command(text)
    if command:
        return str(command.get("skill") or "") or None
    raw = str(text or "")
    for skill_id, pattern in _SKILL_KEYWORDS:
        if pattern.search(raw):
            return skill_id
    intent = str(source.get("intent") or "")
    if intent in {"asset_analysis", "global_canvas"}:
        return "infinite-canvas-analysis"
    return None


@lru_cache(maxsize=16)
def load_skill_instructions(skill_id: str, limit: int = 3200) -> str:
    """读取 Canvas 专用 Skill；路径只能来自内部注册表。"""
    registered = {str(item.get("skill") or "") for item in _COMMANDS}
    registered.update(skill for skill, _ in _SKILL_KEYWORDS)
    if skill_id not in registered:
        return ""
    path = SKILLS_ROOT / skill_id / "SKILL.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            text = parts[2].strip()
    return text[: max(0, int(limit))].rstrip()


def active_skill_context(text: str, profile: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
    skill_id = resolve_skill(text, profile)
    if not skill_id:
        return {"id": "", "instructions": ""}
    return {"id": skill_id, "instructions": load_skill_instructions(skill_id)}
