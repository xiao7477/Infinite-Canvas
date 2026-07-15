"""Canvas Agent backend and API router.

This module owns the fork-specific Agent implementation. ``main.py`` only
configures the explicit upstream dependencies and includes ``router``.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path as _Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from agent.canvas_skills import active_skill_context as _canvas_agent_active_skill_context
from agent.canvas_skills import normalize_context_profile as _canvas_agent_normalize_context_profile
from agent.canvas_skills import public_commands as _canvas_agent_public_commands
from agent.context import build_minimal_context_envelope as _canvas_agent_build_minimal_context_envelope
from agent.complex_tasks import ComplexTaskEngine, ComplexTaskError, normalize_batch_task_spec, normalize_complex_task_spec
from agent.revision import CanvasRevisionConflict as _CanvasRevisionConflict
from agent.revision import CanvasRevisionStore as _CanvasRevisionStore


router = APIRouter()

_HOST_DEPENDENCY_NAMES = (
    "AIReference",
    "API_ENV_FILE",
    "ASSETS_DIR",
    "BASE_DIR",
    "CANVAS_AGENT_IMAGE_SIZE_OPTIONS",
    "CANVAS_TASKS",
    "CANVAS_TASK_LOCK",
    "CANVAS_TASK_WORKERS",
    "CHAT_RATIO_SIZE_OPTIONS",
    "CODEX_DEFAULT_IMAGE_MODELS",
    "CanvasVideoRequest",
    "GEMINI_CLI_DEFAULT_IMAGE_MODELS",
    "JimengPendingError",
    "OUTPUT_DIR",
    "OUTPUT_INPUT_DIR",
    "OUTPUT_OUTPUT_DIR",
    "OnlineImageRequest",
    "create_canvas_image_task",
    "create_canvas_video_task",
    "jimeng_query_result",
    "jimeng_store_outputs",
    "load_api_providers",
    "load_canvas",
    "manager",
    "normalize_canvas_kind",
    "now_ms",
    "parse_size_pair",
    "provider_protocol",
    "save_canvas",
)

_host_dependencies: Dict[str, Any] = {}
COMPLEX_TASK_ENGINE: Optional[ComplexTaskEngine] = None


def configure_agent_backend(*, codex_cli_resolver, **dependencies: Any) -> None:
    """Bind the narrow set of upstream services used by Canvas Agent."""
    missing = [name for name in _HOST_DEPENDENCY_NAMES if name not in dependencies]
    extra = [name for name in dependencies if name not in _HOST_DEPENDENCY_NAMES]
    if missing or extra:
        raise RuntimeError(f"Canvas Agent dependency mismatch: missing={missing}, extra={extra}")
    _host_dependencies.clear()
    _host_dependencies.update(dependencies)
    _host_dependencies["codex_cli_resolver"] = codex_cli_resolver
    globals().update({name: dependencies[name] for name in _HOST_DEPENDENCY_NAMES})
    _configure_complex_task_engine()


def codex_cli_executable():
    resolver = _host_dependencies.get("codex_cli_resolver")
    return resolver() if callable(resolver) else None

# Canvas Agent 后端主体。
# 会话存档 → 读 ~/.codex/sessions/（Codex 自己管）
# Skills    → Canvas Skills 按需注入，通用 Skills 由 Codex 自己加载
# 生成任务 → 复用 main.py 显式注入的上游 Provider/画布任务入口
# 文档：docs/agent-mode-design.md


# Codex home 目录（用环境变量 CODEX_HOME 兜底，默认 ~/.codex）
CODEX_AGENT_HOME = _Path(os.environ.get("CODEX_HOME") or (_Path.home() / ".codex"))
CODEX_AGENT_PREFS_FILE = _Path(os.environ.get("CODEX_AGENT_PREFS_FILE") or (CODEX_AGENT_HOME / "infinite-canvas-agent" / "preferences.md"))
CODEX_AGENT_DEFAULT_PREFS = """# Infinite Canvas Agent Preferences
- 模糊且高成本/耗时任务先确认，尤其生视频、大量生图。
- 画布聊天默认优先操作当前智能画布。
- 优先使用已选素材，按图1、图2顺序引用。
"""
CODEX_AGENT_PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)
CODEX_AGENT_LOCAL_PROXY_PORTS = (7890, 7897, 7899, 1080, 1087, 6152)


def _codex_agent_env_file_values(path: _Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        if not path.exists() or not path.is_file():
            return values
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            values[key] = value.strip().strip('"').strip("'")
    except Exception as exc:
        print(f"[codex-agent] failed to read env file {path}: {exc}")
    return values


def _codex_agent_extra_env_values(project_dir: str = "") -> Dict[str, str]:
    env_files = [
        CODEX_AGENT_HOME / ".env",
        _Path(BASE_DIR) / ".env",
        _Path(API_ENV_FILE),
    ]
    if project_dir:
        try:
            env_files.insert(1, _Path(project_dir).expanduser().resolve() / ".env")
        except Exception:
            pass
    merged: Dict[str, str] = {}
    for path in env_files:
        for key, value in _codex_agent_env_file_values(path).items():
            if key in CODEX_AGENT_PROXY_ENV_KEYS or key.startswith("CODEX_"):
                merged.setdefault(key, value)
    for key in CODEX_AGENT_PROXY_ENV_KEYS:
        if os.environ.get(key):
            merged[key] = os.environ.get(key, "")
    proxy = (
        merged.get("CODEX_AGENT_PROXY")
        or merged.get("CODEX_PROXY")
        or merged.get("ALL_PROXY")
        or merged.get("all_proxy")
    )
    if proxy:
        for key in ("ALL_PROXY", "all_proxy"):
            merged.setdefault(key, proxy)
    for upper, lower in (("HTTP_PROXY", "http_proxy"), ("HTTPS_PROXY", "https_proxy"), ("ALL_PROXY", "all_proxy"), ("NO_PROXY", "no_proxy")):
        if merged.get(upper) and not merged.get(lower):
            merged[lower] = merged[upper]
        if merged.get(lower) and not merged.get(upper):
            merged[upper] = merged[lower]
    return {key: value for key, value in merged.items() if value}


def _codex_agent_proxy_port_status(proxy_url: str) -> Dict[str, Any]:
    if not proxy_url:
        return {"configured": False}
    try:
        parsed = urllib.parse.urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
        host = parsed.hostname or ""
        port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
    except Exception:
        return {"configured": True, "url": proxy_url, "reachable": None, "error": "proxy url parse failed"}
    reachable = None
    error = ""
    try:
        with socket.create_connection((host, port), timeout=0.35):
            reachable = True
    except Exception as exc:
        reachable = False
        error = str(exc)
    return {"configured": True, "url": proxy_url, "host": host, "port": port, "reachable": reachable, "error": error}


def _codex_agent_detect_local_proxy() -> str:
    if str(os.environ.get("CODEX_AGENT_AUTO_PROXY") or "1").strip().lower() in {"0", "false", "off", "no"}:
        return ""
    for port in CODEX_AGENT_LOCAL_PROXY_PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.18):
                return f"http://127.0.0.1:{port}"
        except Exception:
            continue
    return ""


def _codex_agent_env_summary(project_dir: str = "") -> Dict[str, Any]:
    values = _codex_agent_extra_env_values(project_dir)
    proxy = (
        values.get("ALL_PROXY")
        or values.get("all_proxy")
        or values.get("HTTPS_PROXY")
        or values.get("https_proxy")
        or values.get("HTTP_PROXY")
        or values.get("http_proxy")
        or _codex_agent_detect_local_proxy()
    )
    return {
        "codex_cli": codex_cli_executable(),
        "proxy_env_keys": sorted([key for key in values if key.lower().endswith("_proxy")]),
        "proxy": _codex_agent_proxy_port_status(proxy),
    }


def _codex_agent_app_server_env(project_dir: str) -> Dict[str, str]:
    env = {**os.environ, "PWD": project_dir}
    env.update(_codex_agent_extra_env_values(project_dir))
    if not any(env.get(key) for key in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")):
        detected = _codex_agent_detect_local_proxy()
        if detected:
            env["ALL_PROXY"] = detected
            env["all_proxy"] = detected
    return env


# Native App Server dynamic tools. Keep this registry as the single source for
# registered schemas, policy classification, UI wording, and action conversion.
CODEX_AGENT_DYNAMIC_TOOL_NAMESPACE = "infinite_canvas"
CODEX_AGENT_DYNAMIC_TOOL_REGISTRY_VERSION = 5


def _codex_agent_canvas_tool_registry() -> Dict[str, Dict[str, Any]]:
    options_schema = {
        "type": "object",
        "properties": {
            "scope": {"type": "string", "enum": ["selected", "viewport", "canvas", "all", "node"]},
            "selected": {"type": "boolean"}, "all": {"type": "boolean"}, "cols": {"type": "integer", "minimum": 1, "maximum": 12},
            "mode": {"type": "string", "enum": ["grid", "horizontal", "vertical", "column", "layers", "columns"]},
            "layout": {"type": "string", "enum": ["grid", "horizontal", "vertical", "column", "layers", "columns"]},
            "x": {"type": "number"}, "y": {"type": "number"}, "cellX": {"type": "number", "minimum": 80, "maximum": 4000}, "cellY": {"type": "number", "minimum": 80, "maximum": 4000},
            "gapX": {"type": "number", "minimum": 0, "maximum": 1000}, "gapY": {"type": "number", "minimum": 0, "maximum": 1000},
            "side": {"type": "string", "enum": ["left", "right", "top", "bottom", "center"]},
            "placement_scope": {"type": "string", "enum": ["viewport", "global", "node"]},
            "anchor_node_id": {"type": "string"}, "anchor_ref": {"type": "string"},
            "size_mode": {"type": "string", "enum": ["keep", "standard", "reset"]},
            "media_size": {"type": "string", "enum": ["keep", "standard", "reset"]},
            "non_media_size": {"type": "string", "enum": ["keep", "reset"]},
            "preserve_aspect": {"type": "boolean"},
            "tree_scope": {"type": "string", "enum": ["both", "upstream", "downstream"]},
            "title": {"type": "string", "maxLength": 160}, "name": {"type": "string", "maxLength": 160},
            "expected_revision": {"type": "integer", "minimum": 0},
        }, "additionalProperties": False,
    }
    target_schema = {
        "type": "object",
        "properties": {
            "node_id": {"type": "string"},
            "ref": {"type": "string"},
            "scope": {"type": "string", "enum": ["selected", "viewport", "canvas"]},
            "name": {"type": "string", "maxLength": 180},
            "image_index": {"type": "integer", "minimum": 0},
            "items": {"type": "array", "maxItems": 100, "items": {"type": "object", "properties": {"node_id": {"type": "string"}, "ref": {"type": "string"}, "selected": {"type": "boolean"}, "x": {"type": "number"}, "y": {"type": "number"}, "dx": {"type": "number"}, "dy": {"type": "number"}, "name": {"type": "string", "maxLength": 180}, "image_index": {"type": "integer", "minimum": 0}}, "additionalProperties": False}},
            "nodes": {"type": "array", "maxItems": 100, "items": {"type": "object", "properties": {"node_id": {"type": "string"}, "ref": {"type": "string"}, "selected": {"type": "boolean"}}, "additionalProperties": False}},
            "targets": {"type": "array", "maxItems": 100, "items": {"type": "object", "properties": {"node_id": {"type": "string"}, "ref": {"type": "string"}, "selected": {"type": "boolean"}}, "additionalProperties": False}},
            "options": options_schema,
        },
        "additionalProperties": False,
    }
    create_schema = {
        "type": "object",
        "properties": {
            "items": {"type": "array", "minItems": 1, "maxItems": 24, "items": {"type": "object", "properties": {"prompt": {"type": "string", "maxLength": 12000}, "text": {"type": "string", "maxLength": 12000}, "title": {"type": "string", "maxLength": 160}, "url": {"type": "string", "maxLength": 4000}, "path": {"type": "string", "maxLength": 4000}, "name": {"type": "string", "maxLength": 240}, "kind": {"type": "string", "enum": ["image", "video", "audio"]}, "provider_id": {"type": "string", "maxLength": 120}, "model": {"type": "string", "maxLength": 180}, "count": {"type": "integer", "minimum": 1, "maximum": 8}, "size": {"type": "string", "maxLength": 40}, "quality": {"type": "string", "maxLength": 40}, "duration": {"type": "integer", "minimum": 1, "maximum": 60}, "aspect_ratio": {"type": "string", "maxLength": 20}, "resolution": {"type": "string", "maxLength": 40}, "camerafixed": {"type": "boolean"}, "generate_audio": {"type": "boolean"}, "reference_images": {"type": "array", "maxItems": 8, "items": {"type": "object", "properties": {"url": {"type": "string", "maxLength": 4000}, "name": {"type": "string", "maxLength": 240}, "kind": {"type": "string", "enum": ["image", "video"]}}, "required": ["url"], "additionalProperties": False}}}, "additionalProperties": False}},
            "options": options_schema,
        },
        "required": ["items"], "additionalProperties": False,
    }
    batch_reference_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "minLength": 1, "maxLength": 4000},
            "name": {"type": "string", "maxLength": 240},
            "kind": {"type": "string", "enum": ["image", "video"]},
        },
        "required": ["url"],
        "additionalProperties": False,
    }
    batch_item_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 120},
            "title": {"type": "string", "maxLength": 160},
            "kind": {"type": "string", "enum": ["image", "video"]},
            "provider_id": {"type": "string", "minLength": 1, "maxLength": 120},
            "model": {"type": "string", "maxLength": 180},
            "prompt": {"type": "string", "minLength": 1, "maxLength": 12000},
            "reference_images": {"type": "array", "maxItems": 8, "items": batch_reference_schema},
            "count": {"type": "integer", "minimum": 1, "maximum": 8},
            "size": {"type": "string", "maxLength": 40},
            "aspect_ratio": {"type": "string", "maxLength": 20},
            "quality": {"type": "string", "maxLength": 40},
            "duration": {"type": "integer", "minimum": 1, "maximum": 60},
            "resolution": {"type": "string", "maxLength": 40},
            "camerafixed": {"type": "boolean"},
            "generate_audio": {"type": "boolean"},
        },
        "required": ["kind", "provider_id", "prompt"],
        "additionalProperties": False,
    }
    batch_task_schema = {
        "type": "object",
        "properties": {
            "spec": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "maxLength": 160},
                    "base_revision": {"type": "integer", "minimum": 0},
                    "link_visibility": {"type": "string", "enum": ["visible", "hidden"]},
                    "max_retries": {"type": "integer", "minimum": 0, "maximum": 5},
                    "concurrency": {
                        "type": "object",
                        "properties": {
                            "global": {"type": "integer", "minimum": 1, "maximum": 20},
                            "providers": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 1, "maximum": 20}},
                        },
                        "additionalProperties": False,
                    },
                    "options": {"type": "object", "additionalProperties": True},
                    "items": {"type": "array", "minItems": 1, "maxItems": 200, "items": batch_item_schema},
                },
                "required": ["title", "items"],
                "additionalProperties": False,
            },
        },
        "required": ["spec"],
        "additionalProperties": False,
    }
    batch_task_id_schema = {
        "type": "object",
        "properties": {"task_id": {"type": "string", "minLength": 1}},
        "required": ["task_id"],
        "additionalProperties": False,
    }
    return {
        "get_selected_nodes": {"query": True, "label": "查询选中节点", "description": "读取发送本消息时画布中选中的节点。", "input_schema": {"type": "object", "additionalProperties": False}},
        "get_viewport_nodes": {"query": True, "label": "查询可见节点", "description": "读取发送本消息时视口范围内的节点。", "input_schema": {"type": "object", "properties": {"pad": {"type": "number"}, "limit": {"type": "integer", "minimum": 1, "maximum": 120}}, "additionalProperties": False}},
        "get_canvas_summary": {"query": True, "label": "查询画布摘要", "description": "读取画布节点数量、类型统计和连接数量。", "input_schema": {"type": "object", "additionalProperties": False}},
        "get_generation_settings": {"query": True, "label": "查询生成设置", "description": "按需读取发送本消息时的图片/视频默认设置及可用 Provider 和模型；仅在用户指定平台/模型或询问可用选项时调用。", "input_schema": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["all", "image", "video"]}}, "additionalProperties": False}},
        "search_canvas_nodes": {"query": True, "label": "搜索画布节点", "description": "在发送本消息时的画布快照中按名称、标题、文本或类型搜索节点；结果分页且限量返回。", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "types": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer", "minimum": 1, "maximum": 80}, "cursor": {"type": "integer", "minimum": 0}}, "additionalProperties": False}},
        "get_node_detail": {"query": True, "label": "查询节点详情", "description": "读取指定节点，或唯一选中节点的详细信息。", "input_schema": target_schema},
        "get_connected_nodes": {"query": True, "label": "查询关联节点", "description": "读取指定节点，或唯一选中节点的连接关系和相邻节点。", "input_schema": target_schema},
        "get_layout_context": {"query": True, "label": "分析整理空间", "description": "读取选中/引用节点的真实外框、尺寸差异、周边占用和四向可用空间，用于提出整理建议。", "input_schema": target_schema},
        "get_node_tree": {"query": True, "label": "查询节点树", "description": "递归读取一个节点的全部上游、下游和内部连线。", "input_schema": target_schema},
        "get_generation_queue": {"query": True, "label": "查看生成队列", "description": "查看当前画布由 Agent 提交的图片和视频生成任务及状态。", "input_schema": {"type": "object", "properties": {"statuses": {"type": "array", "items": {"type": "string", "enum": ["queued", "running", "jimeng_pending", "interrupted", "failed", "succeeded", "cancelled"]}}}, "additionalProperties": False}},
        "get_generation_task": {"query": True, "label": "获取生成任务详情", "description": "读取当前画布中指定生成任务的状态和错误信息。", "input_schema": {"type": "object", "properties": {"task_id": {"type": "string", "minLength": 1}}, "required": ["task_id"], "additionalProperties": False}},
        "create_media_nodes": {"action": "add_media", "label": "添加媒体节点", "description": "在画布上添加已有图片或视频媒体节点；不移动或重命名真实文件。", "input_schema": create_schema},
        "create_prompt_nodes": {"action": "add_prompt", "label": "添加提示词节点", "description": "在画布上添加提示词节点。", "input_schema": create_schema},
        "create_text_nodes": {"action": "add_text", "label": "添加文本节点", "description": "在画布上添加文本节点。", "input_schema": create_schema},
        "create_loop_nodes": {"action": "add_loop", "label": "添加循环节点", "description": "在画布上添加循环节点。", "input_schema": create_schema},
        "generate_images": {"action": "generate_image", "label": "生成图片", "description": "按当前画布的图片模型设置直接提交图片生成；高风险确认时可改为仅创建配置节点。", "input_schema": create_schema},
        "generate_videos": {"action": "generate_video", "label": "生成视频", "description": "按当前画布的视频模型设置直接提交视频生成；高风险确认时可改为仅创建配置节点。", "input_schema": create_schema},
        "create_image_generation_nodes": {"action": "create_image_generation_node", "label": "创建图片生成节点", "description": "仅创建并配置图片生成节点，不提交图片生成任务。", "input_schema": create_schema},
        "create_video_generation_nodes": {"action": "create_video_generation_node", "label": "创建视频生成节点", "description": "仅创建并配置视频生成节点，不提交视频生成任务。", "input_schema": create_schema},
        "cancel_generation_task": {"action": "cancel_generation_task", "label": "取消生成任务", "description": "仅取消尚未提交上游的排队任务；任务一旦已提交上游即不可取消，节点继续运行。", "input_schema": {"type": "object", "properties": {"task_id": {"type": "string", "minLength": 1}}, "required": ["task_id"], "additionalProperties": False}},
        "retry_generation_task": {"action": "retry_generation_task", "label": "重试生成任务", "description": "重试当前画布中一个失败、中断或已取消的 Agent 生成任务。", "input_schema": {"type": "object", "properties": {"task_id": {"type": "string", "minLength": 1}}, "required": ["task_id"], "additionalProperties": False}},
        "create_batch_task": {"action": "create_batch_task", "label": "规划批量任务", "description": "提交严格的独立生成节点清单；先展示批量执行确认卡，用户确认后由服务端按平台并发排队创建并执行节点。", "input_schema": batch_task_schema},
        "get_batch_task": {"query": True, "label": "查询批量任务", "description": "查询批量任务的项目、进度和最近事件。", "input_schema": batch_task_id_schema},
        "control_batch_task": {"action": "control_batch_task", "label": "控制批量任务", "description": "暂停、继续、取消或重试批量任务。", "input_schema": {"type": "object", "properties": {"task_id": {"type": "string", "minLength": 1}, "action": {"type": "string", "enum": ["pause", "resume", "cancel", "retry", "retry_failed"]}}, "required": ["task_id", "action"], "additionalProperties": False}},
        "delete_nodes": {"action": "delete_nodes", "label": "删除节点", "description": "删除指定或选中的智能画布节点，并移除其连接；始终需要高风险确认。", "input_schema": target_schema},
        "undo_last_agent_action": {"action": "undo_last_agent_action", "label": "撤销最近 Agent 动作", "description": "恢复最近一次 Agent 修改前的节点和连线状态；始终需要高风险确认。", "input_schema": {"type": "object", "additionalProperties": False}},
        "rename_assets": {"action": "rename_nodes", "label": "重命名素材", "description": "修改图片上方的素材显示名；不修改节点内部标题或真实文件。", "input_schema": target_schema},
        "move_nodes": {"action": "move_nodes", "label": "移动节点", "description": "移动指定节点的位置。", "input_schema": target_schema},
        "arrange_nodes": {"action": "arrange_nodes", "label": "排列节点", "description": "排列指定、选中或全画布节点。", "input_schema": target_schema},
        "resize_nodes": {"action": "resize_nodes", "label": "调整节点大小", "description": "将单图/视频节点标准化，或让多图、提示词、循环和分组恢复默认尺寸。", "input_schema": target_schema},
        "arrange_node_tree": {"action": "arrange_node_tree", "label": "整理节点树", "description": "以一个节点为中心递归整理其全部上下游，并在根节点附近的可用空位紧凑收拢。", "input_schema": target_schema},
        "group_nodes": {"action": "group_nodes", "label": "分组节点", "description": "为指定或选中节点创建智能分组。", "input_schema": target_schema},
        "ungroup_nodes": {"action": "ungroup_nodes", "label": "取消分组", "description": "删除指定智能分组外框，不删除其内部节点。", "input_schema": target_schema},
        "remember_preference": {"action": "remember_preference", "label": "保存偏好", "description": "保存用户明确要求记住的画布偏好。", "input_schema": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"], "additionalProperties": False}},
    }


def _codex_agent_dynamic_tool_specs() -> List[Dict[str, Any]]:
    registry = _codex_agent_canvas_tool_registry()
    return [{
        "type": "namespace",
        "name": CODEX_AGENT_DYNAMIC_TOOL_NAMESPACE,
        "description": f"Infinite Canvas smart-canvas tools v{CODEX_AGENT_DYNAMIC_TOOL_REGISTRY_VERSION}. Query context first when needed; mutate only through these tools.",
        "tools": [
            {
                "type": "function",
                "name": name,
                "description": str(spec["description"]),
                "inputSchema": spec["input_schema"],
            }
            for name, spec in registry.items()
        ],
    }]


def _codex_agent_is_dynamic_tools_registration_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    markers = ("dynamictools", "dynamic tools", "unknown field", "unknown parameter", "experimentalapi", "experimental api")
    return any(marker in text for marker in markers)


def _codex_agent_dynamic_tool_response(result: Dict[str, Any], success: Optional[bool] = None) -> Dict[str, Any]:
    ok = bool(result.get("ok", True)) if success is None else bool(success)
    return {
        "success": ok,
        "contentItems": [{"type": "inputText", "text": json.dumps(result, ensure_ascii=False)}],
    }


from agent.runtime import CodexAppServerRuntime, CodexRuntimeDependencies


_CODEX_AGENT_RUNTIME_DEPENDENCIES = CodexRuntimeDependencies(
    cli_executable=codex_cli_executable,
    app_server_env=_codex_agent_app_server_env,
    dynamic_tool_specs=_codex_agent_dynamic_tool_specs,
    is_dynamic_tools_registration_error=_codex_agent_is_dynamic_tools_registration_error,
    dynamic_tool_response=_codex_agent_dynamic_tool_response,
)


_codex_agent_sessions: Dict[str, CodexAppServerRuntime] = {}
_codex_agent_lock = Lock()
_codex_agent_refs_last_cleanup = 0.0


def _codex_agent_runtime_key(project_dir: str = "", canvas_id: str = "", conversation_id: str = "", thread_id: str = "") -> str:
    project_key = str(project_dir or "").strip()
    canvas_key = str(canvas_id or "").strip() or "no-canvas"
    conv_key = str(conversation_id or "").strip()
    thread_key = str(thread_id or "").strip()
    identity = conv_key or (f"thread:{thread_key}" if thread_key else f"draft:{uuid.uuid4().hex}")
    raw = "\n".join([project_key, canvas_key, identity])
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def _codex_agent_runtime_key_candidates(project_dir: str = "", canvas_id: str = "", conversation_id: str = "", thread_id: str = "") -> List[str]:
    project_key = str(project_dir or "").strip()
    canvas_key = str(canvas_id or "").strip() or "no-canvas"
    candidates: List[str] = []
    if conversation_id:
        candidates.append(hashlib.sha256("\n".join([project_key, canvas_key, str(conversation_id)]).encode("utf-8", errors="replace")).hexdigest())
    if thread_id:
        candidates.append(hashlib.sha256("\n".join([project_key, canvas_key, f"thread:{thread_id}"]).encode("utf-8", errors="replace")).hexdigest())
    return candidates


async def _codex_agent_open_runtime(project_dir: str = "", thread_id: str = "", canvas_id: str = "", conversation_id: str = "") -> Dict[str, Any]:
    effective_project_dir = _codex_agent_effective_project_dir(project_dir)
    if project_dir and not os.path.isabs(project_dir):
        raise HTTPException(status_code=400, detail=f"请输入绝对路径: {project_dir}")
    if not os.path.isdir(effective_project_dir):
        raise HTTPException(status_code=400, detail=f"项目目录不存在: {project_dir or '无目录'}")

    runtime_key = ""
    with _codex_agent_lock:
        for candidate in _codex_agent_runtime_key_candidates(project_dir, canvas_id, conversation_id, thread_id):
            if candidate in _codex_agent_sessions:
                runtime_key = candidate
                break
        if not runtime_key:
            runtime_key = _codex_agent_runtime_key(project_dir, canvas_id, conversation_id, thread_id)
        runtime = _codex_agent_sessions.get(runtime_key)
        if runtime is None:
            runtime = CodexAppServerRuntime(
                runtime_key,
                effective_project_dir,
                _CODEX_AGENT_RUNTIME_DEPENDENCIES,
            )
            _codex_agent_sessions[runtime_key] = runtime

    try:
        started = await runtime.start(thread_id=thread_id)
    except HTTPException:
        with _codex_agent_lock:
            _codex_agent_sessions.pop(runtime_key, None)
        raise
    except Exception as e:
        with _codex_agent_lock:
            _codex_agent_sessions.pop(runtime_key, None)
        raise HTTPException(status_code=500, detail=f"启动 Codex App Server Runtime 失败: {e}")

    return {
        "project_dir": project_dir,
        "thread_id": started.get("thread_id") or "",
        "runtime_key": runtime_key,
        "resume_warning": started.get("resume_warning") or "",
        "is_new": not thread_id or bool(started.get("resume_warning")),
    }


async def _codex_agent_close_runtime(project_dir: str = "", canvas_id: str = "", conversation_id: str = "", thread_id: str = "") -> int:
    with _codex_agent_lock:
        if conversation_id or thread_id:
            keys = [key for key in _codex_agent_runtime_key_candidates(project_dir, canvas_id, conversation_id, thread_id) if key in _codex_agent_sessions]
        else:
            keys = [key for key, runtime in _codex_agent_sessions.items() if runtime and str(runtime.project_dir) == _codex_agent_effective_project_dir(project_dir)]
        runtimes = [_codex_agent_sessions.pop(key, None) for key in keys]
    count = 0
    for runtime in runtimes:
        if not runtime:
            continue
        count += 1
        await runtime.stop()
    return count


async def _codex_agent_runtime_for_payload(payload: "CodexAgentTurnRequest") -> CodexAppServerRuntime:
    project_dir = str(payload.project_dir or "").strip()
    canvas_id = _codex_agent_canvas_id_from_payload(payload)
    thread_id = str(payload.thread_id or "").strip()
    conversation_id = str(payload.conversation_id or "").strip()
    with _codex_agent_lock:
        for candidate in _codex_agent_runtime_key_candidates(project_dir, canvas_id, conversation_id, thread_id):
            runtime = _codex_agent_sessions.get(candidate)
            if runtime:
                return runtime
    opened = await _codex_agent_open_runtime(project_dir, thread_id, canvas_id, conversation_id)
    with _codex_agent_lock:
        runtime = _codex_agent_sessions.get(opened.get("runtime_key", ""))
    if not runtime:
        raise HTTPException(status_code=500, detail="Agent Runtime 启动后不可用")
    return runtime


def _codex_agent_ref_ttl_seconds() -> Optional[float]:
    raw = str(os.environ.get("CODEX_AGENT_REF_TTL_HOURS") or "168").strip()
    try:
        hours = float(raw)
    except ValueError:
        hours = 168.0
    if hours <= 0:
        return None
    return hours * 3600


def _codex_agent_cleanup_refs(cache_root: _Path) -> None:
    global _codex_agent_refs_last_cleanup
    ttl = _codex_agent_ref_ttl_seconds()
    if ttl is None:
        return
    now = time.time()
    if now - _codex_agent_refs_last_cleanup < 3600:
        return
    _codex_agent_refs_last_cleanup = now
    if not cache_root.exists():
        return
    cutoff = now - ttl
    try:
        for path in cache_root.rglob("*"):
            try:
                if path.is_file() or path.is_symlink():
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
            except Exception as exc:
                print(f"Codex Agent refs 缓存清理文件失败: {path}: {exc}")
        for path in sorted((p for p in cache_root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                path.rmdir()
            except OSError:
                pass
            except Exception as exc:
                print(f"Codex Agent refs 缓存清理目录失败: {path}: {exc}")
    except Exception as exc:
        print(f"Codex Agent refs 缓存清理失败: {exc}")


def _codex_agent_refs_dir(project_dir: str) -> _Path:
    project_root = _codex_agent_effective_project_dir(project_dir)
    project_key = hashlib.sha1(str(_Path(project_root).resolve()).encode("utf-8")).hexdigest()[:16]
    cache_root = _Path(os.environ.get("CODEX_AGENT_REF_CACHE_DIR") or os.path.join(tempfile.gettempdir(), "infinite-canvas-codex-agent", "refs"))
    _codex_agent_cleanup_refs(cache_root)
    refs_dir = cache_root / project_key
    refs_dir.mkdir(parents=True, exist_ok=True)
    return refs_dir


def _codex_agent_safe_suffix(source: str, content_type: str = "") -> str:
    clean = str(source or "").split("?", 1)[0].split("#", 1)[0]
    suffix = os.path.splitext(clean)[1].lower()
    if suffix and re.match(r"^\.[a-z0-9]{1,8}$", suffix):
        return suffix
    guessed = mimetypes.guess_extension(content_type or "") or ""
    return guessed if guessed and re.match(r"^\.[a-z0-9]{1,8}$", guessed.lower()) else ".png"


def _codex_agent_local_path_from_url(raw: str) -> Optional[str]:
    """把本应用自己的资源 URL 映射成本地文件，避免服务端再经 LAN IP 下载自己。"""
    try:
        parsed = urllib.parse.urlparse(raw)
    except Exception:
        return None

    path = urllib.parse.unquote(parsed.path or "")
    query = urllib.parse.parse_qs(parsed.query or "")
    candidates: List[str] = []

    if path.startswith("/assets/"):
        rel = path[len("/assets/"):].lstrip("/")
        candidates.append(os.path.join(ASSETS_DIR, rel))
    elif path.startswith("/output/"):
        rel = path[len("/output/"):].lstrip("/")
        candidates.append(os.path.join(OUTPUT_DIR, rel))
    elif path == "/api/view":
        filename = (query.get("filename") or [""])[0]
        kind = (query.get("type") or ["input"])[0]
        subfolder = (query.get("subfolder") or [""])[0]
        if filename:
            safe_name = os.path.basename(filename)
            if kind == "output":
                candidates.append(os.path.join(OUTPUT_OUTPUT_DIR, subfolder, safe_name))
            else:
                candidates.append(os.path.join(OUTPUT_INPUT_DIR, subfolder, safe_name))
    elif path == "/api/codex-agent/file/view":
        file_path = (query.get("path") or [""])[0]
        if file_path:
            candidates.append(file_path)

    base_dirs = [_Path(ASSETS_DIR).resolve(), _Path(OUTPUT_DIR).resolve(), CODEX_AGENT_HOME.resolve()]
    for candidate in candidates:
        try:
            p = _Path(candidate).resolve()
        except Exception:
            continue
        if path == "/api/codex-agent/file/view" and p.is_file():
            return str(p)
        if p.is_file() and any(p == base or base in p.parents for base in base_dirs):
            return str(p)
    return None


def _codex_agent_attachment_value(item: Any) -> Dict[str, Any]:
    if isinstance(item, dict):
        data = dict(item)
        data["url"] = str(data.get("url") or data.get("src") or data.get("path") or "").strip()
        return data
    return {"url": str(item or "").strip()}


def _codex_agent_media_kind_from_ref(ref: Dict[str, Any], path: str = "") -> str:
    kind = str(ref.get("kind") or ref.get("mediaKind") or "").strip().lower()
    if kind in {"image", "video", "audio"}:
        return kind
    target = (path or str(ref.get("url") or "")).split("?", 1)[0].split("#", 1)[0].lower()
    if re.search(r"\.(mp4|mov|webm|m4v|avi|mkv)$", target):
        return "video"
    if re.search(r"\.(mp3|wav|m4a|aac|ogg|flac)$", target):
        return "audio"
    return "image"


async def _codex_agent_prepare_local_image(ref: Any, project_dir: str, client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
    """
    把画布里的图片引用准备成 Codex app-server 可读的本地绝对路径。
    这比直接传 remote URL 更稳，也和 Codex `localImage` 输入类型对齐。
    """
    ref_data = _codex_agent_attachment_value(ref)
    ref_url = str(ref_data.get("url") or "").strip()
    if not ref_url:
        raise ValueError("empty ref")

    refs_dir = _codex_agent_refs_dir(project_dir)
    raw = ref_url

    if raw.startswith("data:"):
        if ";base64," not in raw:
            raise ValueError("data URL 缺少 base64 数据")
        header, encoded = raw.split(";base64,", 1)
        mime = header[5:].split(";", 1)[0] if header.startswith("data:") else "image/png"
        suffix = _codex_agent_safe_suffix("", mime)
        target = refs_dir / f"ref-{uuid.uuid4().hex}{suffix}"
        with open(target, "wb") as f:
            f.write(base64.b64decode(encoded))
        local_path = str(target)
        return {**ref_data, "url": ref_url, "local_path": local_path, "kind": _codex_agent_media_kind_from_ref(ref_data, local_path)}

    if raw.startswith("file://"):
        local = urllib.parse.unquote(raw[len("file://"):])
    elif raw.startswith("http://") or raw.startswith("https://"):
        local_hit = _codex_agent_local_path_from_url(raw)
        if local_hit:
            local = local_hit
        else:
            c = client or httpx.AsyncClient()
            try:
                r = await c.get(raw, timeout=30, follow_redirects=True)
                r.raise_for_status()
                suffix = _codex_agent_safe_suffix(raw, r.headers.get("content-type", ""))
                target = refs_dir / f"ref-{uuid.uuid4().hex}{suffix}"
                with open(target, "wb") as f:
                    f.write(r.content)
                local_path = str(target)
                return {**ref_data, "url": ref_url, "local_path": local_path, "kind": _codex_agent_media_kind_from_ref(ref_data, local_path)}
            finally:
                if client is None:
                    await c.aclose()
    elif raw.startswith("/"):
        local = _codex_agent_local_path_from_url(raw) or raw
    else:
        raise ValueError(f"unsupported ref: {raw}")

    local_path = _Path(local).expanduser().resolve()
    if not local_path.is_file():
        raise FileNotFoundError(str(local_path))
    suffix = _codex_agent_safe_suffix(str(local_path), mimetypes.guess_type(str(local_path))[0] or "")
    target = refs_dir / f"ref-{uuid.uuid4().hex}{suffix}"
    shutil.copyfile(str(local_path), str(target))
    local_out = str(target)
    return {**ref_data, "url": ref_url, "source_path": str(local_path), "local_path": local_out, "kind": _codex_agent_media_kind_from_ref(ref_data, local_out)}


async def _codex_agent_prepare_attachment(ref: Any, project_dir: str, client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
    """Keep prompt-node references as ordered text; media follows the local-image path."""
    ref_data = _codex_agent_attachment_value(ref)
    if str(ref_data.get("kind") or "").strip().lower() == "prompt":
        text = str(ref_data.get("text") or ref_data.get("prompt") or "").strip()
        if not text:
            raise ValueError("empty prompt ref")
        return {**ref_data, "kind": "prompt", "text": text[:12000], "local_path": ""}
    return await _codex_agent_prepare_local_image(ref_data, project_dir, client)


def _codex_agent_ensure_preferences_file() -> None:
    try:
        CODEX_AGENT_PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not CODEX_AGENT_PREFS_FILE.exists():
            CODEX_AGENT_PREFS_FILE.write_text(CODEX_AGENT_DEFAULT_PREFS, encoding="utf-8")
    except Exception as exc:
        print(f"[codex-agent] preferences file unavailable: {exc}")


def _codex_agent_read_preferences(limit: int = 1200) -> str:
    try:
        _codex_agent_ensure_preferences_file()
        text = CODEX_AGENT_PREFS_FILE.read_text(encoding="utf-8").strip()
    except Exception as exc:
        print(f"[codex-agent] failed to read preferences: {exc}")
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n- ...偏好过长，已截断"


def _codex_agent_append_preference(note: str) -> str:
    clean = re.sub(r"\s+", " ", str(note or "")).strip(" -\t\r\n")
    if not clean:
        raise ValueError("偏好内容为空")
    if len(clean) > 80:
        clean = clean[:80].rstrip()
    _codex_agent_ensure_preferences_file()
    current = CODEX_AGENT_PREFS_FILE.read_text(encoding="utf-8") if CODEX_AGENT_PREFS_FILE.exists() else ""
    line = f"- {clean}"
    if line in current.splitlines():
        return str(CODEX_AGENT_PREFS_FILE)
    suffix = "" if current.endswith("\n") or not current else "\n"
    CODEX_AGENT_PREFS_FILE.write_text(current + suffix + line + "\n", encoding="utf-8")
    return str(CODEX_AGENT_PREFS_FILE)


def _codex_agent_save_context_snapshot(payload: "CodexAgentTurnRequest", canvas_context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    ctx = canvas_context if isinstance(canvas_context, dict) else {}
    canvas_id = _codex_agent_canvas_id_from_payload(payload) or "no-canvas"
    conversation_id = str(payload.conversation_id or "").strip() or str(payload.thread_id or "").strip() or "draft"
    native = ctx.get("native") if isinstance(ctx.get("native"), dict) else {}
    snapshot_id = f"snap_{uuid.uuid4().hex[:16]}"
    viewport_snapshot_id = f"vp_{uuid.uuid4().hex[:16]}"
    canvas_snapshot: Dict[str, Any] = {}
    canvas_revision = 0
    try:
        live_canvas = load_canvas(canvas_id)
        if isinstance(live_canvas, dict):
            canvas_revision = int(CODEX_AGENT_REVISION_STORE.observe(canvas_id, live_canvas).get("revision") or 0)
            canvas_snapshot = {
                "title": live_canvas.get("name") or live_canvas.get("title") or "",
                "nodes": live_canvas.get("nodes") if isinstance(live_canvas.get("nodes"), list) else [],
                "connections": live_canvas.get("connections") if isinstance(live_canvas.get("connections"), list) else [],
            }
    except Exception:
        # The frontend context remains a usable reduced snapshot for canvases that
        # disappear between request receipt and snapshot persistence.
        canvas_snapshot = {}
    data = {
        "schema": 3,
        "snapshot_id": snapshot_id,
        "viewport_snapshot_id": viewport_snapshot_id,
        "project_dir": str(payload.project_dir or ""),
        "canvas_id": canvas_id,
        "conversation_id": conversation_id,
        "thread_id": str(payload.thread_id or ""),
        "created_at": _codex_agent_now(),
        "canvas_revision": canvas_revision,
        "context": ctx,
        "canvas_snapshot": canvas_snapshot,
    }
    try:
        canvas_dir = CODEX_AGENT_CONTEXT_SNAPSHOT_DIR / hashlib.sha256(str(canvas_id).encode("utf-8", errors="replace")).hexdigest()[:16]
        canvas_dir.mkdir(parents=True, exist_ok=True)
        path = canvas_dir / f"{snapshot_id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        print(f"[codex-agent] failed to save context snapshot: {exc}")
        path = _Path("")
    return {
        "snapshot_id": snapshot_id,
        "viewport_snapshot_id": viewport_snapshot_id,
        "snapshot_path": str(path) if path else "",
        "canvas_id": canvas_id,
        "conversation_id": conversation_id,
        "captured_at": native.get("capturedAt") or native.get("captured_at") or _codex_agent_now(),
        "canvas_revision": canvas_revision,
    }


def _codex_agent_advance_context_snapshot(
    canvas_context: Optional[Dict[str, Any]],
    canvas_id: str,
    canvas: Dict[str, Any],
    canvas_revision: Optional[int] = None,
) -> None:
    """Advance this turn's working snapshot after a successful Agent-owned write.

    The send-time snapshot is still the stable starting point. Once this Agent
    turn changes the canvas, later Tools in the same turn must see that new
    state/revision instead of treating the Agent's own previous write as an
    external conflict.
    """
    ctx = canvas_context if isinstance(canvas_context, dict) else {}
    meta = ctx.get("_contextSnapshot") if isinstance(ctx.get("_contextSnapshot"), dict) else {}
    if not meta or not isinstance(canvas, dict):
        return
    revision = int(
        canvas_revision
        if canvas_revision is not None
        else CODEX_AGENT_REVISION_STORE.observe(canvas_id, canvas).get("revision") or 0
    )
    meta["canvas_revision"] = revision
    meta["working_revision_advanced"] = True
    # Later native Tool calls in the same turn reuse this in-memory context.
    # Refresh its geometry as well as the file-backed snapshot; otherwise an
    # earlier resize can leave `native.allNodes` describing the send-time DOM
    # size and a following arrange call will place the newly enlarged nodes on
    # top of each other.
    native = ctx.get("native") if isinstance(ctx.get("native"), dict) else None
    if native is not None:
        current_nodes = [node for node in (canvas.get("nodes") or []) if isinstance(node, dict)]
        selected_ids = {
            str(item.get("id") or "")
            for item in (native.get("selectedNodes") or [])
            if isinstance(item, dict) and item.get("id")
        }
        selected_ids.update(str(node_id) for node_id in (native.get("selectedNodeIds") or []) if node_id)
        native["allNodes"] = [_codex_agent_node_summary(node) for node in current_nodes]
        native["selectedNodes"] = [
            _codex_agent_node_summary(node)
            for node in current_nodes
            if str(node.get("id") or "") in selected_ids
        ]
        native["connections"] = [
            {
                "from": conn.get("from") or "",
                "to": conn.get("to") or "",
                "kind": conn.get("kind") or "flow",
            }
            for conn in (canvas.get("connections") or [])
            if isinstance(conn, dict)
        ]
    path_text = str(meta.get("snapshot_path") or "").strip()
    if not path_text:
        return
    try:
        root = CODEX_AGENT_CONTEXT_SNAPSHOT_DIR.resolve()
        path = _Path(path_text).resolve()
        if root not in path.parents or path.suffix != ".json" or not path.is_file():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        stored_canvas_id = str(data.get("canvas_id") or "")
        if stored_canvas_id and stored_canvas_id != str(canvas_id or ""):
            return
        data["canvas_revision"] = revision
        data["canvas_snapshot"] = {
            "title": canvas.get("name") or canvas.get("title") or "",
            "nodes": canvas.get("nodes") if isinstance(canvas.get("nodes"), list) else [],
            "connections": canvas.get("connections") if isinstance(canvas.get("connections"), list) else [],
        }
        tmp = path.with_name(path.name + f".{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        # A missing working snapshot should not turn a successful canvas write
        # into a user-visible failure; revision checks still protect later calls.
        print(f"[codex-agent] failed to advance context snapshot: {exc}")


def _codex_agent_build_turn_context_text(project_dir: str, refs: List[Dict[str, Any]], canvas_context: Optional[Dict[str, Any]], payload: "CodexAgentTurnRequest") -> str:
    # Keep the private working snapshot on the task context. It starts at the
    # authoritative send-time state and advances only after this turn's own
    # successful writes; mutable browser UI state is never queried directly.
    ctx = canvas_context if isinstance(canvas_context, dict) else {}
    requested_profile = ctx.get("contextProfile") if isinstance(ctx.get("contextProfile"), dict) else {}
    ctx["contextProfile"] = _canvas_agent_normalize_context_profile(payload.text, requested_profile, len(refs))
    snapshot = _codex_agent_save_context_snapshot(payload, ctx)
    ctx["_contextSnapshot"] = snapshot
    return _codex_agent_context_envelope_text(project_dir, refs, ctx, payload, snapshot)


def _codex_agent_context_envelope_text(
    project_dir: str,
    refs: List[Dict[str, Any]],
    canvas_context: Optional[Dict[str, Any]],
    payload: "CodexAgentTurnRequest",
    snapshot: Dict[str, Any],
) -> str:
    """v2 最小 envelope：工具说明交给 schema，专项流程交给按需 Canvas Skill。"""
    ctx = canvas_context if isinstance(canvas_context, dict) else {}
    native = ctx.get("native") if isinstance(ctx.get("native"), dict) else {}
    profile = ctx.get("contextProfile") if isinstance(ctx.get("contextProfile"), dict) else {}
    agent_input = ctx.get("agentInput") if isinstance(ctx.get("agentInput"), dict) else {}
    node_counts = native.get("nodeCounts") if isinstance(native.get("nodeCounts"), dict) else {}
    selected_ids = native.get("selectedNodeIds") if isinstance(native.get("selectedNodeIds"), list) else []
    skill = _canvas_agent_active_skill_context(str(payload.text or ""), profile)
    try:
        context_level = max(0, min(3, int(profile.get("level", 2 if refs else 0))))
    except (TypeError, ValueError):
        context_level = 2 if refs else 0
    metadata = {
        "project_dir": project_dir or "无目录",
        "canvas_id": snapshot.get("canvas_id") or _codex_agent_canvas_id_from_payload(payload),
        "conversation_id": str(payload.conversation_id or ""),
        "context_snapshot_id": snapshot.get("snapshot_id") or "",
        "viewport_snapshot_id": snapshot.get("viewport_snapshot_id") or "",
        "canvas_revision": snapshot.get("canvas_revision") or 0,
        "captured_at": snapshot.get("captured_at") or "",
        "context_level": context_level,
        "context_intent": profile.get("intent") or "chat",
        "context_command": profile.get("command") or "",
        "approval_policy": agent_input.get("approvalPolicy") or agent_input.get("approval_policy") or profile.get("approvalPolicy") or "",
        "total_nodes": node_counts.get("total") if isinstance(node_counts, dict) else None,
        "selected_nodes": (node_counts.get("selected") if isinstance(node_counts, dict) else None) or len(selected_ids),
    }
    no_project_rule = "当前未选择工作目录；可以聊天和操作画布，需要读写项目文件时请用户先选择工作目录。" if not project_dir else ""
    return _canvas_agent_build_minimal_context_envelope(
        metadata=metadata,
        refs=refs,
        preferences=_codex_agent_read_preferences(limit=800),
        skill=skill,
        native_tools_enabled=bool(ctx.get("_native_tools_enabled", True)),
        no_project_rule=no_project_rule,
    )


def _codex_agent_file_view_url(path: str) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://", "data:")):
        return text
    return "/api/codex-agent/file/view?path=" + urllib.parse.quote(text)


def _codex_agent_parse_ref_context(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    raw = str(text or "")
    refs: List[Dict[str, Any]] = []
    if "<canvas_agent_context>" not in raw or "</canvas_agent_context>" not in raw:
        return _codex_agent_strip_internal_context(raw), refs

    before, rest = raw.split("<canvas_agent_context>", 1)
    ctx, after = rest.split("</canvas_agent_context>", 1)
    selected_ctx = ctx.split("selected_refs:", 1)[1] if "selected_refs:" in ctx else ""
    for stop in (
        "\n<canvas_skill>",
        "\nuser_canvas_preferences:",
        "\n</canvas_agent_context>",
        "\ncurrent_canvas_image_generation_defaults:",
        "\nsend_time_visible_canvas_viewport:",
        "\navailable_image_providers:",
        "\ncurrent_canvas_video_generation_defaults:",
        "\navailable_video_providers:",
        "\ncurrent_canvas_nodes:",
        "\ncurrent_canvas_connections_count:",
    ):
        if stop in selected_ctx:
            selected_ctx = selected_ctx.split(stop, 1)[0]

    current: Optional[Dict[str, Any]] = None
    for line in selected_ctx.splitlines():
        stripped = line.strip()
        if stripped.startswith("- id:"):
            if current:
                refs.append(current)
            current = {"refId": stripped.split(":", 1)[1].strip()}
            continue
        if current is not None and ":" in stripped:
            key, value = stripped.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key == "name":
                current["name"] = value
            elif key == "kind":
                current["kind"] = value
            elif key == "local_path":
                current["local_path"] = value
            elif key == "source_path":
                current["source_path"] = value
            elif key == "canvas_kind":
                current["canvasKind"] = value
            elif key == "node_id":
                current["nodeId"] = value
            elif key == "image_index":
                current["imageIndex"] = value
            elif key == "node_title":
                current["nodeTitle"] = value
    if current:
        refs.append(current)

    for index, ref in enumerate(refs, 1):
        path = str(ref.get("source_path") or ref.get("local_path") or "").strip()
        ref["url"] = _codex_agent_file_view_url(path)
        ref["refId"] = str(ref.get("refId") or f"ref_{index}")
        ref["name"] = str(ref.get("name") or os.path.basename(path) or f"图{index}")
        ref["kind"] = str(ref.get("kind") or _codex_agent_media_kind_from_ref(ref, path) or "image")

    clean = before + after
    marker = "用户请求："
    if marker in clean:
        clean = clean.split(marker, 1)[1]
    return _codex_agent_strip_agent_markup(clean).strip(), refs


def _codex_agent_strip_internal_context(text: str) -> str:
    clean = str(text or "")
    for tag in (
        "skill",
        "skills_instructions",
        "environment_context",
        "app-context",
        "permissions instructions",
        "collaboration_mode",
        "plugins_instructions",
    ):
        escaped = re.escape(tag)
        clean = re.sub(rf"<{escaped}>[\s\S]*?</{escaped}>", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"<skill\b[\s\S]*?</skill>", "", clean, flags=re.IGNORECASE)
    return clean.strip()


def _codex_agent_strip_agent_markup(text: str) -> str:
    clean = _codex_agent_strip_internal_context(text)
    clean = re.sub(r"```(?:canvas_agent_action|canvas-agent-action)\s*[\s\S]*?```", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"<canvas_agent_action>[\s\S]*?</canvas_agent_action>", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"```(?:canvas_agent_tool|canvas-agent-tool|canvas_tool|canvas-tool)\s*[\s\S]*?```", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"<(?:canvas_agent_tool|canvas_tool)>[\s\S]*?</(?:canvas_agent_tool|canvas_tool)>", "", clean, flags=re.IGNORECASE)
    return clean.strip()


def _codex_agent_is_internal_context_text(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return True
    lowered = raw.lower()
    internal_markers = (
        "<environment_context>",
        "<app-context>",
        "<permissions instructions>",
        "<collaboration_mode>",
        "<skills_instructions>",
        "<plugins_instructions>",
    )
    if any(marker in lowered for marker in internal_markers):
        return True
    return lowered.startswith("<skill>") or "<skill>" in lowered[:200]


async def _to_inline_data_url(url: str, client: Optional[httpx.AsyncClient] = None) -> str:
    """
    把任意形式的"图片源"转成 Codex app-server 期望的 inline data URL：
    - 已经是 data: URL → 直接返回
    - file://path 或 本地绝对路径 → 读文件 + base64 + 加 mime 前缀
    - http(s)://URL → 用 httpx 下载 + base64

    Codex app-server 明确不支持 remote image URL（只接受 data URL）。
    """
    if not url:
        raise ValueError("empty url")
    if url.startswith("data:"):
        return url

    if url.startswith("file://"):
        local = url[len("file://"):]
    elif url.startswith("/"):
        local = url
    elif url.startswith("http://") or url.startswith("https://"):
        # 下载
        c = client or httpx.AsyncClient()
        try:
            r = await c.get(url, timeout=30, follow_redirects=True)
            r.raise_for_status()
            content = r.content
        finally:
            if client is None:
                await c.aclose()
        mime, _ = mimetypes.guess_type(url)
        if not mime:
            mime = "image/png"
        b64 = base64.b64encode(content).decode("ascii")
        return f"data:{mime};base64,{b64}"
    else:
        raise ValueError(f"unsupported url: {url}")

    # 本地文件
    if not os.path.isfile(local):
        raise FileNotFoundError(local)
    with open(local, "rb") as f:
        content = f.read()
    mime, _ = mimetypes.guess_type(local)
    if not mime:
        mime = "image/png"
    b64 = base64.b64encode(content).decode("ascii")
    return f"data:{mime};base64,{b64}"


# === 阶段 2：Pydantic models + 路由 ===

class CodexAgentBoardOpenRequest(BaseModel):
    project_dir: str
    thread_id: Optional[str] = None
    canvas_id: str = ""
    conversation_id: str = ""


class CodexAgentBoardCloseRequest(BaseModel):
    project_dir: str
    thread_id: Optional[str] = None
    canvas_id: str = ""
    conversation_id: str = ""


class CodexAgentTurnRequest(BaseModel):
    project_dir: str
    text: str
    attachments: Optional[List[Any]] = None  # URL、本地路径，或 {url,name,nodeId,...}
    canvas_context: Optional[Dict[str, Any]] = None
    canvas_id: str = ""
    thread_id: str = ""
    conversation_id: str = ""


class CodexAgentTaskStatusRequest(BaseModel):
    task_id: str
    after: int = 0


class CodexAgentActionResolveRequest(BaseModel):
    task_id: str
    approval_id: str
    decision: str = "approve"


class CodexAgentPanelStateRequest(BaseModel):
    project_dir: str = ""
    canvas_id: str = ""
    thread_id: str = ""
    conversation_id: str = ""
    canvas_title: str = ""
    status: str = "ready"
    input_mode: str = ""
    input_scope: str = ""
    approval_policy: str = ""
    open: bool = False
    messages: Optional[List[Any]] = None
    attachments: Optional[List[Any]] = None
    task_id: str = ""
    task_offset: int = 0
    scroll_top: int = 0


class CodexAgentPreferenceRequest(BaseModel):
    note: str


class CodexAgentProjectVisibilityRequest(BaseModel):
    canvas_id: str
    project_dir: str = ""
    hidden: bool = False


class CodexAgentWorkdirPresetRequest(BaseModel):
    path: str


class CodexAgentCanvasToolRequest(BaseModel):
    canvas_id: str = ""
    tool: str
    args: Optional[Dict[str, Any]] = None
    refs: Optional[List[Any]] = None
    canvas_context: Optional[Dict[str, Any]] = None


class ComplexTaskCreateRequest(BaseModel):
    canvas_id: str = ""
    spec: Dict[str, Any]


class ComplexTaskControlRequest(BaseModel):
    action: str


class ComplexTaskReplyRequest(BaseModel):
    text: str


class ComplexTaskReviseRequest(BaseModel):
    changes: List[Dict[str, Any]]


_codex_agent_tasks: Dict[str, Dict[str, Any]] = {}
_codex_agent_task_lock = Lock()
CODEX_AGENT_PANEL_STATE_DIR = CODEX_AGENT_HOME / "infinite-canvas-agent" / "panel-history"
CODEX_AGENT_HISTORY_DB = CODEX_AGENT_HOME / "infinite-canvas-agent" / "history.sqlite"
CODEX_AGENT_WORKDIR_PRESETS_FILE = CODEX_AGENT_HOME / "infinite-canvas-agent" / "workdir-presets.json"
CODEX_AGENT_NO_PROJECT_DIR = CODEX_AGENT_HOME / "infinite-canvas-agent" / "no-project-workspace"
CODEX_AGENT_CONTEXT_SNAPSHOT_DIR = CODEX_AGENT_HOME / "infinite-canvas-agent" / "context-snapshots"
CODEX_AGENT_REVISION_STORE = _CanvasRevisionStore(CODEX_AGENT_HOME / "infinite-canvas-agent" / "canvas-revisions")
_codex_agent_history_lock = Lock()


def _codex_agent_now() -> int:
    try:
        return now_ms()
    except Exception:
        return int(time.time() * 1000)


def _codex_agent_history_id(*parts: str) -> str:
    raw = "\n".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def _codex_agent_history_connect() -> sqlite3.Connection:
    CODEX_AGENT_HISTORY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CODEX_AGENT_HISTORY_DB))
    conn.row_factory = sqlite3.Row
    return conn


def _codex_agent_history_init() -> None:
    with _codex_agent_history_lock:
        conn = _codex_agent_history_connect()
        try:
            conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                project_dir TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_opened_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS canvases (
                id TEXT PRIMARY KEY,
                canvas_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_opened_at INTEGER NOT NULL,
                UNIQUE(project_id, canvas_id)
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                canvas_row_id TEXT NOT NULL,
                canvas_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                codex_thread_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'ready',
                archived INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_message_preview TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                message_index INTEGER NOT NULL,
                role TEXT NOT NULL,
                blocks_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                canvas_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                progress_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS canvas_generation_tasks (
                task_id TEXT PRIMARY KEY,
                canvas_id TEXT NOT NULL DEFAULT '',
                node_id TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                provider_id TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                upstream_task_id TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_canvas_generation_tasks_status ON canvas_generation_tasks(status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_canvas_generation_tasks_canvas ON canvas_generation_tasks(canvas_id, updated_at);
            CREATE TABLE IF NOT EXISTS canvas_agent_undo (
                id TEXT PRIMARY KEY,
                canvas_id TEXT NOT NULL,
                action_json TEXT NOT NULL DEFAULT '[]',
                snapshot_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                undone_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_canvas_agent_undo_latest ON canvas_agent_undo(canvas_id, undone_at, created_at);
            CREATE TABLE IF NOT EXISTS project_visibility (
                canvas_id TEXT NOT NULL,
                project_dir TEXT NOT NULL DEFAULT '',
                hidden INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(canvas_id, project_dir)
            );
            CREATE INDEX IF NOT EXISTS idx_conversations_project_updated ON conversations(project_id, archived, updated_at);
            CREATE INDEX IF NOT EXISTS idx_conversations_canvas_updated ON conversations(canvas_id, archived, updated_at);
            CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, message_index);
            """)
            conn.commit()
        finally:
            conn.close()


def _canvas_generation_task_persist(task: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> None:
    """Store agent-capable generation task state outside process memory."""
    if not isinstance(task, dict) or not task.get("id"):
        return
    _codex_agent_history_init()
    now = _codex_agent_now()
    task_id = str(task.get("id") or "")
    stored_payload = payload if isinstance(payload, dict) else task.get("payload")
    stored_payload = stored_payload if isinstance(stored_payload, dict) else {}
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    with _codex_agent_history_lock:
        conn = _codex_agent_history_connect()
        try:
            existing = conn.execute("SELECT created_at FROM canvas_generation_tasks WHERE task_id=?", (task_id,)).fetchone()
            conn.execute("""
                INSERT INTO canvas_generation_tasks(
                    task_id, canvas_id, node_id, kind, status, provider_id, model,
                    upstream_task_id, payload_json, result_json, error, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    canvas_id=excluded.canvas_id, node_id=excluded.node_id, kind=excluded.kind,
                    status=excluded.status, provider_id=excluded.provider_id, model=excluded.model,
                    upstream_task_id=excluded.upstream_task_id, payload_json=excluded.payload_json,
                    result_json=excluded.result_json, error=excluded.error, updated_at=excluded.updated_at
            """, (
                task_id, str(task.get("canvas_id") or ""), str(task.get("node_id") or ""),
                str(task.get("kind") or task.get("type") or ""), str(task.get("status") or "queued"),
                str(task.get("provider_id") or ""), str(task.get("model") or ""),
                str(task.get("submit_id") or task.get("upstream_task_id") or ""),
                json.dumps(stored_payload, ensure_ascii=False), json.dumps(result, ensure_ascii=False),
                str(task.get("error") or ""), int(existing["created_at"]) if existing else now, now,
            ))
            conn.commit()
        finally:
            conn.close()


def _canvas_generation_task_rows(statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    _codex_agent_history_init()
    with _codex_agent_history_lock:
        conn = _codex_agent_history_connect()
        try:
            query = "SELECT * FROM canvas_generation_tasks"
            values: List[Any] = []
            if statuses:
                query += " WHERE status IN (" + ",".join("?" for _ in statuses) + ")"
                values.extend(statuses)
            query += " ORDER BY updated_at DESC"
            rows = conn.execute(query, values).fetchall()
        finally:
            conn.close()
    out = []
    for row in rows:
        data = dict(row)
        for key in ("payload_json", "result_json"):
            try:
                data[key[:-5]] = json.loads(data.get(key) or "{}")
            except Exception:
                data[key[:-5]] = {}
        out.append(data)
    return out


def _canvas_task_payload_dict(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        return dict(payload)
    try:
        return payload.model_dump()
    except Exception:
        try:
            return payload.dict()
        except Exception:
            return {}


# Public bridge used by the host canvas generation endpoints.  These helpers
# used to live in main.py; keeping named exports prevents the Agent extraction
# from leaving the ordinary image/video task routes with dangling globals.
def persist_canvas_generation_task(task: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> None:
    _canvas_generation_task_persist(task, payload)


def canvas_task_payload_dict(payload: Any) -> Dict[str, Any]:
    return _canvas_task_payload_dict(payload)


def _codex_agent_record_undo(canvas_id: str, actions: List[Dict[str, Any]], canvas: Dict[str, Any]) -> None:
    _codex_agent_history_init()
    snapshot = {"nodes": canvas.get("nodes") or [], "connections": canvas.get("connections") or []}
    with _codex_agent_history_lock:
        conn = _codex_agent_history_connect()
        try:
            conn.execute("INSERT INTO canvas_agent_undo(id, canvas_id, action_json, snapshot_json, created_at) VALUES(?, ?, ?, ?, ?)", (
                _codex_agent_uid("undo"), canvas_id, json.dumps(actions, ensure_ascii=False), json.dumps(snapshot, ensure_ascii=False), _codex_agent_now(),
            ))
            conn.commit()
        finally:
            conn.close()


async def _codex_agent_undo_last_action(canvas_id: str, expected_revision: Optional[int] = None) -> Dict[str, Any]:
    _codex_agent_history_init()
    canvas = load_canvas(canvas_id)
    try:
        revision_state = CODEX_AGENT_REVISION_STORE.assert_expected(canvas_id, canvas, expected_revision)
    except _CanvasRevisionConflict as exc:
        return {
            "ok": False,
            "conflict": True,
            "message": "画布已在确认撤销后发生变化，请重新查询后再撤销。",
            "expected_revision": exc.expected,
            "canvas_revision": exc.current,
            "results": [],
            "changed": 0,
            "skipped": 1,
        }
    with _codex_agent_history_lock:
        conn = _codex_agent_history_connect()
        try:
            row = conn.execute("SELECT * FROM canvas_agent_undo WHERE canvas_id=? AND undone_at IS NULL ORDER BY created_at DESC LIMIT 1", (canvas_id,)).fetchone()
            if not row:
                return {"ok": False, "message": "没有可撤销的 Agent 画布动作", "results": [], "changed": 0, "skipped": 0}
            snapshot = json.loads(row["snapshot_json"] or "{}")
            conn.execute("UPDATE canvas_agent_undo SET undone_at=? WHERE id=?", (_codex_agent_now(), row["id"]))
            conn.commit()
        finally:
            conn.close()
    canvas["nodes"] = snapshot.get("nodes") if isinstance(snapshot.get("nodes"), list) else []
    canvas["connections"] = snapshot.get("connections") if isinstance(snapshot.get("connections"), list) else []
    save_canvas(canvas)
    revision_state = CODEX_AGENT_REVISION_STORE.observe(canvas_id, canvas)
    await manager.broadcast_canvas_updated(canvas_id, int(canvas.get("updated_at") or _codex_agent_now()), "codex-agent")
    return {"ok": True, "changed": 1, "skipped": 0, "canvas_revision": int(revision_state.get("revision") or 0), "results": [{"type": "undo_last_agent_action", "status": "done"}], "message": "已撤销最近一次 Agent 画布动作"}


def _codex_agent_message_preview(messages: Any) -> str:
    rows = messages if isinstance(messages, list) else []
    meaningful_types = {"text", "image", "media_preview", "attach", "attachment"}
    for preferred_role in ("user", "bot"):
        for msg in reversed(rows):
            if not isinstance(msg, dict) or str(msg.get("role") or "") != preferred_role:
                continue
            for block in msg.get("blocks") if isinstance(msg.get("blocks"), list) else []:
                if not isinstance(block, dict):
                    continue
                btype = str(block.get("type") or "").strip()
                if btype not in meaningful_types:
                    continue
                text = str(block.get("text") or block.get("prompt") or "").strip()
                if text:
                    return re.sub(r"\s+", " ", text)[:160]
                if btype in {"attach", "attachment", "media_preview"}:
                    items = block.get("items") if isinstance(block.get("items"), list) else []
                    names = [str(item.get("name") or "").strip() for item in items[:2] if isinstance(item, dict) and item.get("name")]
                    return "、".join(names) if names else "含图片/附件的对话"
    return "画布 Agent 对话"


def _codex_agent_has_meaningful_messages(messages: Any) -> bool:
    for msg in messages if isinstance(messages, list) else []:
        if not isinstance(msg, dict):
            continue
        for block in msg.get("blocks") if isinstance(msg.get("blocks"), list) else []:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "").strip()
            if btype in {"text", "image", "media_preview", "attach", "attachment"}:
                text = str(block.get("text") or block.get("prompt") or "").strip()
                items = block.get("items") if isinstance(block.get("items"), list) else []
                if text or items:
                    return True
    return False


def _codex_agent_history_upsert_panel_state(data: Dict[str, Any]) -> Dict[str, Any]:
    project_dir = str(data.get("project_dir") or "").strip()
    canvas_id = str(data.get("canvas_id") or "").strip()
    thread_id = str(data.get("thread_id") or "").strip()
    if not canvas_id or (not project_dir and not thread_id):
        return {}
    _codex_agent_history_init()
    now = int(data.get("updated_at") or _codex_agent_now())
    project_key = project_dir or "__no_project__"
    project_id = _codex_agent_history_id("project", project_key)
    canvas_row_id = _codex_agent_history_id("canvas", project_key, canvas_id)
    conversation_id = str(data.get("conversation_id") or "").strip()
    if not conversation_id:
        conversation_id = _codex_agent_history_id("conversation", project_key, canvas_id, thread_id or str(now))
    messages = data.get("messages") if isinstance(data.get("messages"), list) else []
    if not _codex_agent_has_meaningful_messages(messages):
        return {}
    preview = _codex_agent_message_preview(messages)
    title = preview[:60] or "画布 Agent 对话"
    with _codex_agent_history_lock:
        conn = _codex_agent_history_connect()
        try:
            conn.execute("""
                INSERT INTO projects(id, project_dir, title, created_at, updated_at, last_opened_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_dir) DO UPDATE SET
                    title=excluded.title,
                    updated_at=excluded.updated_at,
                    last_opened_at=excluded.last_opened_at
            """, (project_id, project_dir, os.path.basename(project_dir) if project_dir else "无目录对话", now, now, now))
            conn.execute("""
                INSERT INTO canvases(id, canvas_id, project_id, title, created_at, updated_at, last_opened_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, canvas_id) DO UPDATE SET
                    title=excluded.title,
                    updated_at=excluded.updated_at,
                    last_opened_at=excluded.last_opened_at
            """, (canvas_row_id, canvas_id, project_id, str(data.get("canvas_title") or canvas_id), now, now, now))
            existing = conn.execute("SELECT created_at FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            created_at = int(existing["created_at"]) if existing else now
            conn.execute("""
                INSERT INTO conversations(
                    id, project_id, canvas_row_id, canvas_id, title, codex_thread_id,
                    status, archived, created_at, updated_at, last_message_preview
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    codex_thread_id=excluded.codex_thread_id,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    last_message_preview=excluded.last_message_preview
            """, (conversation_id, project_id, canvas_row_id, canvas_id, title, thread_id, str(data.get("status") or "ready"), created_at, now, preview))
            conn.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
            for index, msg in enumerate(messages[-200:]):
                if not isinstance(msg, dict):
                    continue
                role = "user" if msg.get("role") == "user" else "bot"
                blocks = msg.get("blocks") if isinstance(msg.get("blocks"), list) else []
                conn.execute(
                    "INSERT INTO messages(conversation_id, message_index, role, blocks_json, created_at) VALUES(?, ?, ?, ?, ?)",
                    (conversation_id, index, role, json.dumps(blocks, ensure_ascii=False), now),
                )
            conn.commit()
        finally:
            conn.close()
    return {"conversation_id": conversation_id, "project_id": project_id, "canvas_row_id": canvas_row_id}


def _codex_agent_history_projects(canvas_id: str = "", include_hidden: bool = False) -> List[Dict[str, Any]]:
    _codex_agent_history_init()
    canvas_id = str(canvas_id or "").strip()
    with _codex_agent_history_lock:
        conn = _codex_agent_history_connect()
        try:
            values: List[Any] = []
            canvas_clause = ""
            if canvas_id:
                canvas_clause = "AND c.canvas_id = ?"
                values.append(canvas_id)
            hidden_join = ""
            hidden_where = ""
            hidden_select = "0 AS hidden"
            if canvas_id:
                hidden_join = "LEFT JOIN project_visibility v ON v.canvas_id = ? AND v.project_dir = p.project_dir"
                hidden_select = "COALESCE(v.hidden, 0) AS hidden"
                values.append(canvas_id)
                if not include_hidden:
                    hidden_where = "AND COALESCE(v.hidden, 0) = 0"
            rows = conn.execute(f"""
                WITH project_stats AS (
                    SELECT
                        p.id AS project_id,
                        COUNT(c.id) AS conversation_count,
                        MAX(c.updated_at) AS last_conversation_at
                    FROM projects p
                    JOIN conversations c ON c.project_id = p.id
                    WHERE c.archived = 0 {canvas_clause}
                    GROUP BY p.id
                )
                SELECT p.*, s.conversation_count, s.last_conversation_at, {hidden_select}
                FROM project_stats s
                JOIN projects p ON p.id = s.project_id
                {hidden_join}
                WHERE 1=1 {hidden_where}
                ORDER BY COALESCE(s.last_conversation_at, p.updated_at) DESC
            """, values).fetchall()
            out = []
            for row in rows:
                item = dict(row)
                latest_values: List[Any] = [item["id"]]
                latest_canvas = ""
                if canvas_id:
                    latest_canvas = "AND canvas_id = ?"
                    latest_values.append(canvas_id)
                latest = conn.execute("""
                    SELECT id, codex_thread_id, canvas_id
                    FROM conversations
                    WHERE project_id = ? AND archived = 0 """ + latest_canvas + """
                    ORDER BY updated_at DESC
                    LIMIT 1
                """, latest_values).fetchone()
                if latest:
                    item["latest_conversation_id"] = latest["id"]
                    item["latest_thread_id"] = latest["codex_thread_id"]
                    item["latest_canvas_id"] = latest["canvas_id"]
                else:
                    item["latest_conversation_id"] = ""
                    item["latest_thread_id"] = ""
                    item["latest_canvas_id"] = ""
                out.append(item)
            return out
        finally:
            conn.close()


def _codex_agent_history_conversations(project_dir: Optional[str] = None, canvas_id: str = "", include_archived: bool = False) -> List[Dict[str, Any]]:
    _codex_agent_history_init()
    clauses = []
    values: List[Any] = []
    if project_dir is not None:
        clauses.append("p.project_dir = ?")
        values.append(str(project_dir or ""))
    if canvas_id:
        clauses.append("c.canvas_id = ?")
        values.append(canvas_id)
    if not include_archived:
        clauses.append("c.archived = 0")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _codex_agent_history_lock:
        conn = _codex_agent_history_connect()
        try:
            rows = conn.execute(f"""
                SELECT c.*, p.project_dir, p.title AS project_title
                FROM conversations c
                JOIN projects p ON p.id = c.project_id
                {where}
                ORDER BY c.updated_at DESC
            """, values).fetchall()
            out = []
            for row in rows:
                item = dict(row)
                item["session_id"] = item.get("codex_thread_id", "")
                item["thread_id"] = item.get("codex_thread_id", "")
                item["cwd"] = item.get("project_dir", "")
                item["preview"] = item.get("last_message_preview", "")
                item["started_at"] = datetime.datetime.fromtimestamp(int(item.get("updated_at") or 0) / 1000, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ") if item.get("updated_at") else ""
                item["model"] = "Canvas Agent"
                item["source"] = "canvas-agent"
                out.append(item)
            return out
        finally:
            conn.close()


def _codex_agent_history_conversation_state(conversation_id: str) -> Optional[Dict[str, Any]]:
    _codex_agent_history_init()
    with _codex_agent_history_lock:
        conn = _codex_agent_history_connect()
        try:
            conv = conn.execute("""
                SELECT c.*, p.project_dir
                FROM conversations c
                JOIN projects p ON p.id = c.project_id
                WHERE c.id = ?
            """, (conversation_id,)).fetchone()
            if not conv:
                return None
            rows = conn.execute("SELECT role, blocks_json FROM messages WHERE conversation_id=? ORDER BY message_index ASC", (conversation_id,)).fetchall()
            messages = []
            for row in rows:
                try:
                    blocks = json.loads(row["blocks_json"])
                except Exception:
                    blocks = []
                messages.append({"role": row["role"], "blocks": blocks if isinstance(blocks, list) else []})
            return {
                "project_dir": conv["project_dir"],
                "canvas_id": conv["canvas_id"],
                "thread_id": conv["codex_thread_id"],
                "conversation_id": conv["id"],
                "messages": messages,
                "attachments": [],
                "task_id": "",
                "task_offset": 0,
                "scroll_top": 0,
                "updated_at": conv["updated_at"],
                "open": True,
            }
        finally:
            conn.close()


def _codex_agent_history_latest_state(canvas_id: str = "", project_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    _codex_agent_history_init()
    clauses = ["c.archived = 0"]
    values: List[Any] = []
    if canvas_id:
        clauses.append("c.canvas_id = ?")
        values.append(canvas_id)
    if project_dir is not None:
        clauses.append("p.project_dir = ?")
        values.append(str(project_dir or ""))
    with _codex_agent_history_lock:
        conn = _codex_agent_history_connect()
        try:
            row = conn.execute(f"""
                SELECT c.id
                FROM conversations c
                JOIN projects p ON p.id = c.project_id
                WHERE {" AND ".join(clauses)}
                ORDER BY c.updated_at DESC
                LIMIT 1
            """, values).fetchone()
        finally:
            conn.close()
    return _codex_agent_history_conversation_state(row["id"]) if row else None


def _codex_agent_set_project_visibility(canvas_id: str, project_dir: str = "", hidden: bool = False) -> None:
    canvas_id = str(canvas_id or "").strip()
    if not canvas_id:
        raise ValueError("缺少 canvas_id")
    _codex_agent_history_init()
    with _codex_agent_history_lock:
        conn = _codex_agent_history_connect()
        try:
            conn.execute("""
                INSERT INTO project_visibility(canvas_id, project_dir, hidden, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(canvas_id, project_dir) DO UPDATE SET
                    hidden=excluded.hidden,
                    updated_at=excluded.updated_at
            """, (canvas_id, str(project_dir or ""), 1 if hidden else 0, _codex_agent_now()))
            conn.commit()
        finally:
            conn.close()


def _codex_agent_effective_project_dir(project_dir: str = "") -> str:
    raw = str(project_dir or "").strip()
    if raw:
        return raw
    CODEX_AGENT_NO_PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    return str(CODEX_AGENT_NO_PROJECT_DIR)


def _codex_agent_read_workdir_presets() -> List[str]:
    try:
        if not CODEX_AGENT_WORKDIR_PRESETS_FILE.exists():
            return []
        data = json.loads(CODEX_AGENT_WORKDIR_PRESETS_FILE.read_text(encoding="utf-8"))
        values = data.get("presets") if isinstance(data, dict) else data
        if not isinstance(values, list):
            return []
        out = []
        for value in values:
            text = str(value or "").strip()
            if text and text not in out:
                out.append(text)
        return out
    except Exception:
        return []


def _codex_agent_write_workdir_presets(values: List[str]) -> None:
    CODEX_AGENT_WORKDIR_PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    clean = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in clean:
            clean.append(text)
    CODEX_AGENT_WORKDIR_PRESETS_FILE.write_text(json.dumps({"presets": clean}, ensure_ascii=False, indent=2), encoding="utf-8")


def _codex_agent_panel_state_key(canvas_id: str = "", project_dir: str = "", thread_id: str = "") -> str:
    raw = "\n".join([str(canvas_id or ""), str(project_dir or ""), str(thread_id or "")])
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def _codex_agent_panel_state_path(canvas_id: str = "", project_dir: str = "", thread_id: str = "") -> _Path:
    return CODEX_AGENT_PANEL_STATE_DIR / f"{_codex_agent_panel_state_key(canvas_id, project_dir, thread_id)}.json"


def _codex_agent_compact_panel_messages(messages: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    allowed_types = {
        "text", "thinking", "tool", "error", "image", "todo", "attach",
        "tool_call", "tool_result", "canvas_action_result", "choice",
        "parameter_form", "progress", "node_locator", "media_preview", "attachment", "process",
    }
    scalar_keys = {
        "text", "status", "id", "path", "prompt", "title", "label", "name",
        "kind", "summary", "detail", "command", "tool", "action", "message",
        "task_id", "approval_id", "target", "url", "risk", "reason", "decision",
        "startedAt", "endedAt", "output",
    }
    list_keys = {"items", "options", "fields", "nodes", "results", "actions", "steps"}
    dict_keys = {"meta", "data", "params", "progress", "result"}
    for msg in (messages if isinstance(messages, list) else [])[-120:]:
        if not isinstance(msg, dict):
            continue
        role = "user" if msg.get("role") == "user" else "bot"
        blocks: List[Dict[str, Any]] = []
        for block in (msg.get("blocks") if isinstance(msg.get("blocks"), list) else []):
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if btype not in allowed_types:
                continue
            copy: Dict[str, Any] = {"type": btype}
            for key in scalar_keys:
                if key in block:
                    value = block.get(key)
                    if isinstance(value, str) and len(value) > 24000:
                        value = value[:24000] + "\n..."
                    copy[key] = value
            for key in list_keys:
                if isinstance(block.get(key), list):
                    limit = 40 if key in {"items", "nodes", "results"} else 80
                    copy[key] = block.get(key)[:limit]
            for key in dict_keys:
                if isinstance(block.get(key), dict):
                    copy[key] = block.get(key)
            if btype in {"attach", "attachment"} and isinstance(copy.get("items"), list):
                copy["items"] = copy.get("items")[:40]
                copy["count"] = block.get("count", len(copy["items"]))
            elif "count" in block:
                copy["count"] = block.get("count")
            for key in ("changed", "skipped", "total", "current", "percent"):
                if key in block:
                    copy[key] = block.get(key)
            if block.get("streaming"):
                copy["streaming"] = True
            if block.get("stale"):
                copy["stale"] = True
            blocks.append(copy)
        if blocks:
            out.append({"role": role, "blocks": blocks})
    return out


def _codex_agent_panel_state_public(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {"found": False, "state": None}
    return {
        "found": True,
        "state": {
            "projectDir": data.get("project_dir", ""),
            "project_dir": data.get("project_dir", ""),
            "canvasId": data.get("canvas_id", ""),
            "canvas_id": data.get("canvas_id", ""),
            "threadId": data.get("thread_id", ""),
            "thread_id": data.get("thread_id", ""),
            "conversationId": data.get("conversation_id", ""),
            "conversation_id": data.get("conversation_id", ""),
            "open": bool(data.get("open", False)),
            "messages": data.get("messages") if isinstance(data.get("messages"), list) else [],
            "attachments": data.get("attachments") if isinstance(data.get("attachments"), list) else [],
            "taskId": data.get("task_id", ""),
            "task_id": data.get("task_id", ""),
            "taskOffset": int(data.get("task_offset") or 0),
            "task_offset": int(data.get("task_offset") or 0),
            "scrollTop": int(data.get("scroll_top") or 0),
            "scroll_top": int(data.get("scroll_top") or 0),
            "inputMode": data.get("input_mode", ""),
            "input_mode": data.get("input_mode", ""),
            "inputScope": data.get("input_scope", ""),
            "input_scope": data.get("input_scope", ""),
            "approvalPolicy": data.get("approval_policy", ""),
            "approval_policy": data.get("approval_policy", ""),
            "updatedAt": int(data.get("updated_at") or 0),
            "updated_at": int(data.get("updated_at") or 0),
        },
    }


def _codex_agent_read_panel_state(canvas_id: str = "", project_dir: str = "", thread_id: str = "") -> Optional[Dict[str, Any]]:
    path = _codex_agent_panel_state_path(canvas_id, project_dir, thread_id)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _codex_agent_write_panel_state(payload: CodexAgentPanelStateRequest) -> Dict[str, Any]:
    project_dir = str(payload.project_dir or "").strip()
    canvas_id = str(payload.canvas_id or "").strip()
    thread_id = str(payload.thread_id or "").strip()
    if not canvas_id:
        raise HTTPException(status_code=400, detail="缺少 canvas_id")
    if not project_dir and not thread_id:
        raise HTTPException(status_code=400, detail="缺少 project_dir/thread_id")
    CODEX_AGENT_PANEL_STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = _codex_agent_now()
    data = {
        "schema": 1,
        "project_dir": project_dir,
        "canvas_id": canvas_id,
        "thread_id": thread_id,
        "conversation_id": str(payload.conversation_id or "").strip(),
        "canvas_title": str(payload.canvas_title or "").strip(),
        "status": str(payload.status or "ready").strip() or "ready",
        "input_mode": str(payload.input_mode or "").strip(),
        "input_scope": str(payload.input_scope or "").strip(),
        "approval_policy": str(payload.approval_policy or "").strip(),
        "open": bool(payload.open),
        "messages": _codex_agent_compact_panel_messages(payload.messages),
        "attachments": payload.attachments[:40] if isinstance(payload.attachments, list) else [],
        "task_id": str(payload.task_id or ""),
        "task_offset": max(0, int(payload.task_offset or 0)),
        "scroll_top": max(0, int(payload.scroll_top or 0)),
        "created_at": now,
        "updated_at": now,
    }
    old = _codex_agent_read_panel_state(canvas_id, project_dir, thread_id)
    if old and old.get("created_at"):
        data["created_at"] = old.get("created_at")
    path = _codex_agent_panel_state_path(canvas_id, project_dir, thread_id)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)
    data.update(_codex_agent_history_upsert_panel_state(data))
    return data


def _codex_agent_latest_panel_state(canvas_id: str = "", project_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not CODEX_AGENT_PANEL_STATE_DIR.exists():
        return None
    best: Optional[Dict[str, Any]] = None
    try:
        for path in CODEX_AGENT_PANEL_STATE_DIR.glob("*.json"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if canvas_id and str(data.get("canvas_id") or "") != str(canvas_id):
                continue
            if project_dir is not None and str(data.get("project_dir") or "") != str(project_dir or ""):
                continue
            if best is None or int(data.get("updated_at") or 0) > int(best.get("updated_at") or 0):
                best = data
    except Exception:
        return best
    return best


def _codex_agent_panel_state_preview(messages: Any) -> str:
    for msg in (messages if isinstance(messages, list) else []):
        for block in (msg.get("blocks") if isinstance(msg, dict) and isinstance(msg.get("blocks"), list) else []):
            if not isinstance(block, dict):
                continue
            text = str(block.get("text") or "").strip()
            if text:
                return re.sub(r"\s+", " ", text)[:120]
    return "画布 Agent 对话"


def _codex_agent_panel_state_meta_quick(data: Dict[str, Any], path: Optional[_Path] = None) -> Dict[str, Any]:
    updated = int(data.get("updated_at") or data.get("created_at") or 0)
    if updated:
        try:
            dt = datetime.datetime.fromtimestamp(updated / 1000, tz=datetime.timezone.utc)
            started_at = dt.strftime("%Y-%m-%d %H:%M:%SZ")
        except Exception:
            started_at = str(updated)
    else:
        started_at = ""
    return {
        "session_id": str(data.get("thread_id") or ""),
        "thread_id": str(data.get("thread_id") or ""),
        "panel_state_key": _codex_agent_panel_state_key(str(data.get("canvas_id") or ""), str(data.get("project_dir") or ""), str(data.get("thread_id") or "")),
        "canvas_id": str(data.get("canvas_id") or ""),
        "started_at": started_at,
        "updated_at": updated,
        "cwd": str(data.get("project_dir") or ""),
        "model": "Codex Agent",
        "preview": _codex_agent_panel_state_preview(data.get("messages")),
        "preview_media": "",
        "rollout_path": "",
        "panel_state_path": str(path or ""),
        "source": "panel",
    }


def _codex_agent_panel_state_metas(project_dir: str = "") -> List[Dict[str, Any]]:
    if not CODEX_AGENT_PANEL_STATE_DIR.exists():
        return []
    metas: List[Dict[str, Any]] = []
    try:
        for path in CODEX_AGENT_PANEL_STATE_DIR.glob("*.json"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            cwd = str(data.get("project_dir") or "")
            if project_dir and cwd != project_dir:
                continue
            if not cwd:
                continue
            metas.append(_codex_agent_panel_state_meta_quick(data, path))
    except Exception:
        return metas
    metas.sort(key=lambda item: int(item.get("updated_at") or 0), reverse=True)
    return metas


def _codex_agent_task_public(task: Dict[str, Any], after: int = 0) -> Dict[str, Any]:
    events = task.get("events") or []
    start = max(0, int(after or 0))
    return {
        "task_id": task.get("task_id", ""),
        "project_dir": task.get("project_dir", ""),
        "canvas_id": task.get("canvas_id", ""),
        "thread_id": task.get("thread_id", ""),
        "conversation_id": task.get("conversation_id", ""),
        "runtime_key": task.get("runtime_key", ""),
        "native_tools_enabled": bool(task.get("native_tools_enabled", False)),
        "status": task.get("status", "unknown"),
        "created_at": task.get("created_at", 0),
        "updated_at": task.get("updated_at", 0),
        "started_at": task.get("started_at", 0),
        "completed_at": task.get("completed_at", 0),
        "error": task.get("error", ""),
        "event_count": len(events),
        "next_event_index": len(events),
        "events": events[start:],
        "summary": task.get("summary", {}),
    }


def _codex_agent_add_task_event(task_id: str, event: Dict[str, Any]) -> None:
    with _codex_agent_task_lock:
        task = _codex_agent_tasks.get(task_id)
        if not task:
            return
        task.setdefault("events", []).append(event)
        task["updated_at"] = _codex_agent_now()


def _codex_agent_set_task_status(task_id: str, status: str, **extra: Any) -> None:
    with _codex_agent_task_lock:
        task = _codex_agent_tasks.get(task_id)
        if not task:
            return
        task["status"] = status
        task["updated_at"] = _codex_agent_now()
        if status == "running" and not task.get("started_at"):
            task["started_at"] = task["updated_at"]
        if status in {"completed", "failed", "stopped"}:
            task["completed_at"] = task["updated_at"]
        task.update(extra)


def _codex_agent_canvas_id_from_payload(payload: CodexAgentTurnRequest) -> str:
    if payload.canvas_id:
        return str(payload.canvas_id).strip()
    ctx = payload.canvas_context or {}
    native = ctx.get("native") if isinstance(ctx, dict) else {}
    if isinstance(native, dict):
        value = native.get("canvasId") or native.get("canvas_id")
        if value:
            return str(value).strip()
    return ""


def _codex_agent_extract_canvas_action_blocks(text: str) -> List[str]:
    raw = str(text or "")
    blocks: List[str] = []
    for m in re.finditer(r"```(?:canvas_agent_action|canvas-agent-action)\s*([\s\S]*?)```", raw, re.I):
        body = (m.group(1) or "").strip()
        if body:
            blocks.append(body)
    for m in re.finditer(r"<canvas_agent_action>([\s\S]*?)</canvas_agent_action>", raw, re.I):
        body = (m.group(1) or "").strip()
        if body:
            blocks.append(body)
    return blocks


def _codex_agent_extract_canvas_tool_blocks(text: str) -> List[str]:
    raw = str(text or "")
    blocks: List[str] = []
    for m in re.finditer(r"```(?:canvas_agent_tool|canvas-agent-tool|canvas_tool|canvas-tool)\s*([\s\S]*?)```", raw, re.I):
        body = (m.group(1) or "").strip()
        if body:
            blocks.append(body)
    for m in re.finditer(r"<(?:canvas_agent_tool|canvas_tool)>([\s\S]*?)</(?:canvas_agent_tool|canvas_tool)>", raw, re.I):
        body = (m.group(1) or "").strip()
        if body:
            blocks.append(body)
    return blocks


def _codex_agent_parse_canvas_tool_calls(raw: str) -> List[Dict[str, Any]]:
    try:
        payload = json.loads(str(raw or "").strip())
    except Exception as exc:
        raise ValueError(f"Canvas Tool JSON 解析失败：{exc}") from exc
    if isinstance(payload, list):
        calls = payload
    elif isinstance(payload, dict):
        for key in ("tools", "tool_calls", "calls"):
            if isinstance(payload.get(key), list):
                calls = payload.get(key)
                break
        else:
            calls = [payload]
    else:
        calls = []
    out: List[Dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        tool = str(call.get("tool") or call.get("name") or call.get("type") or function.get("name") or "").strip()
        args = call.get("args") if isinstance(call.get("args"), dict) else (call.get("arguments") if call.get("arguments") is not None else function.get("arguments"))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {"value": args}
        if not isinstance(args, dict):
            args = {}
        # 兼容模型常见的简写：{"tool":"get_node_detail","node_id":"..."}。
        # 旧逻辑只读取 args/arguments，会静默丢掉顶层参数，继而把查询误判为无目标。
        inline_args = {
            key: value for key, value in call.items()
            if key not in {"tool", "name", "type", "function", "args", "arguments", "call_id", "tool_call_id"}
        }
        args = {**inline_args, **args}
        if tool:
            out.append({"tool": tool, "args": args})
    return out


def _codex_agent_media_kind(url: str, fallback: str = "image") -> str:
    clean = str(url or "").split("?", 1)[0].split("#", 1)[0].lower()
    if re.search(r"\.(mp4|mov|webm|m4v|avi|mkv)$", clean):
        return "video"
    if re.search(r"\.(mp3|wav|m4a|aac|ogg|flac)$", clean):
        return "audio"
    return fallback or "image"


def _codex_agent_name_from_url(url: str, fallback: str = "asset") -> str:
    try:
        path = urllib.parse.urlparse(str(url or "")).path
        name = os.path.basename(urllib.parse.unquote(path))
    except Exception:
        name = ""
    return name or fallback


def _codex_agent_canvas_url_for_path(path_or_url: str) -> str:
    raw = str(path_or_url or "").strip()
    if not raw:
        return ""
    if re.match(r"^(https?:|data:|/)", raw, re.I):
        return raw
    return _codex_agent_file_view_url(raw)


def _codex_agent_uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}{int(time.time() * 1000):x}"[-64:]


def _codex_agent_context_node_rects(canvas_context: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    ctx = canvas_context if isinstance(canvas_context, dict) else {}
    native = ctx.get("native") if isinstance(ctx.get("native"), dict) else {}
    items: List[Any] = []
    snapshot = _codex_agent_context_snapshot_data(ctx)
    stored = snapshot.get("canvas_snapshot") if isinstance(snapshot.get("canvas_snapshot"), dict) else {}
    stored_nodes = stored.get("nodes") if isinstance(stored.get("nodes"), list) else []
    items.extend(stored_nodes)
    for key in ("allNodes", "selectedNodes"):
        values = native.get(key)
        if isinstance(values, list):
            items.extend(values)
    rects: Dict[str, Dict[str, float]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id") or "").strip()
        if not node_id:
            continue
        try:
            width = float(item.get("width") or item.get("w") or 0)
            height = float(item.get("height") or item.get("h") or 0)
            if width <= 0 or height <= 0:
                continue
            rects[node_id] = {
                "x": float(item.get("x") or 0),
                "y": float(item.get("y") or 0),
                "width": width,
                "height": height,
            }
        except Exception:
            continue
    return rects


def _codex_agent_node_rect(node: Dict[str, Any], context_rects: Optional[Dict[str, Dict[str, float]]] = None) -> Dict[str, float]:
    node_id = str(node.get("id") or "")
    captured = (context_rects or {}).get(node_id)
    if captured:
        return {
            "x": float(node.get("x") if node.get("x") is not None else captured.get("x") or 0),
            "y": float(node.get("y") if node.get("y") is not None else captured.get("y") or 0),
            "width": max(1, float(captured.get("width") or 1)),
            "height": max(1, float(captured.get("height") or 1)),
        }
    ntype = str(node.get("type") or "smart-image")
    if ntype == "smart-prompt":
        w = max(float(node.get("w") or 316), 316)
        text_len = len(str(node.get("text") or ""))
        h = max(float(node.get("h") or 240), 240, min(560, 190 + text_len / 7))
    elif ntype == "smart-loop":
        w = float(node.get("w") or 340)
        h = float(node.get("h") or 168)
    elif ntype == "smart-group":
        w = float(node.get("w") or 340)
        h = float(node.get("h") or 286)
    else:
        images = [item for item in (node.get("images") or []) if isinstance(item, dict)]
        count = len(images)
        scale = float(node.get("scale") or (0.8 if count > 1 else 2))
        explicit_w = float(node.get("w") or 0)
        explicit_h = float(node.get("h") or 0)
        if explicit_w > 24 and explicit_h > 24:
            w, h = explicit_w, explicit_h
        elif count <= 1:
            if count == 1:
                image = images[0]
                source = str(image.get("url") or image.get("name") or "").split("?", 1)[0].lower()
                kind = str(image.get("kind") or "").lower()
                if kind == "audio" or source.endswith((".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")):
                    w, h = 288 * scale, 150 * scale
                else:
                    natural_w = float(image.get("natural_w") or image.get("width") or image.get("w") or image.get("layout_w") or image.get("preview_w") or 0)
                    natural_h = float(image.get("natural_h") or image.get("height") or image.get("h") or image.get("layout_h") or image.get("preview_h") or 0)
                    if natural_w > 0 and natural_h > 0:
                        fit = min((260 * scale) / natural_w, (220 * scale) / natural_h)
                        w = max(72, round(natural_w * fit))
                        h = max(72, round(natural_h * fit))
                    else:
                        w, h = 260 * scale, 180 * scale
            else:
                w, h = 316, 194
        else:
            thumb = round(224 * scale)
            cell = thumb + 8
            grid = next((item.get("grid") for item in images if isinstance(item.get("grid"), dict) and item.get("grid", {}).get("type") == "grid-split"), None)
            if grid:
                cols = max(1, int(grid.get("cols") or 1))
                rows = max(1, int(grid.get("rows") or math.ceil(count / cols)))
            else:
                cols = min(4, max(2, math.ceil(math.sqrt(count))))
                rows = math.ceil(count / cols)
            visible_rows = min(3, rows)
            w = max(round(226 * scale), cols * cell + 32)
            h = visible_rows * cell - 8 + 32
    return {
        "x": float(node.get("x") or 0),
        "y": float(node.get("y") or 0),
        "width": max(1, w),
        "height": max(1, h),
    }


def _codex_agent_node_summary(node: Dict[str, Any]) -> Dict[str, Any]:
    rect = _codex_agent_node_rect(node)
    summary = {
        "id": node.get("id", ""),
        "type": node.get("type") or "smart-image",
        "title": node.get("title") or "",
        "x": round(float(node.get("x") or 0)),
        "y": round(float(node.get("y") or 0)),
        "width": round(rect["width"]),
        "height": round(rect["height"]),
        "text": node.get("text") or node.get("variablePrompt") or "",
        "images": [
            {
                "index": index,
                "url": img.get("url") or "",
                "name": img.get("name") or "",
                "kind": img.get("kind") or _codex_agent_media_kind(img.get("url") or ""),
            }
            for index, img in enumerate(node.get("images") or [])
            if isinstance(img, dict)
        ],
    }
    if str(node.get("type") or "") == "smart-agent-task":
        summary["task"] = {
            "status": node.get("taskStatus") or "",
            "mode": node.get("taskMode") or "",
            "progress": node.get("taskProgress") if isinstance(node.get("taskProgress"), dict) else {},
            "question": node.get("taskQuestion") or "",
            "link_visibility": node.get("linkVisibility") or "visible",
        }
    return summary


def _codex_agent_selected_ids(canvas_context: Optional[Dict[str, Any]]) -> List[str]:
    ctx = canvas_context or {}
    native = ctx.get("native") if isinstance(ctx, dict) else {}
    ids = []
    if isinstance(native, dict) and isinstance(native.get("selectedNodeIds"), list):
        ids = native.get("selectedNodeIds") or []
    return [str(x).strip() for x in ids if str(x or "").strip()]


def _codex_agent_ref_attachment(value: Any, refs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    text = str(value or "").strip()
    if not text:
        return None
    m = re.match(r"^@?ref[_-]?(\d+)$", text, re.I) or re.match(r"^@?图\s*(\d+)$", text)
    if not m:
        return None
    idx = max(0, int(m.group(1)) - 1)
    return refs[idx] if 0 <= idx < len(refs) else None


def _codex_agent_resolve_node_ids(item: Any, nodes: List[Dict[str, Any]], refs: List[Dict[str, Any]], selected_ids: List[str]) -> List[str]:
    data = {"ref": item} if isinstance(item, str) else (item if isinstance(item, dict) else {})
    existing = {str(node.get("id")) for node in nodes}
    out: List[str] = []

    def add(value: Any) -> None:
        node_id = str(value or "").strip()
        if node_id and node_id in existing and node_id not in out:
            out.append(node_id)

    add(data.get("node_id") or data.get("nodeId") or data.get("id") or data.get("node") or data.get("target_node_id") or data.get("targetNodeId"))
    for key in ("node_ids", "nodeIds", "ids"):
        values = data.get(key)
        if isinstance(values, list):
            for value in values:
                add(value)
    # target/source 既可以是实际节点 id，也可以是 ref_1/图1；两种写法都支持。
    add(data.get("target") or data.get("source") or data.get("anchor_node_id") or data.get("anchorNodeId"))
    ref_value = data.get("ref") or data.get("ref_id") or data.get("refId") or data.get("target_ref") or data.get("targetRef") or data.get("target")
    ref = _codex_agent_ref_attachment(ref_value, refs)
    if ref:
        add(ref.get("nodeId") or ref.get("node_id"))
    if data.get("selected") or data.get("scope") == "selected":
        for node_id in selected_ids:
            add(node_id)
    return out


def _codex_agent_visible_world(canvas_context: Optional[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    ctx = canvas_context if isinstance(canvas_context, dict) else {}
    native = ctx.get("native") if isinstance(ctx.get("native"), dict) else {}
    raw = native.get("visibleWorld") or native.get("visible_world") or ctx.get("visibleWorld") or ctx.get("visible_world") or {}
    if not isinstance(raw, dict):
        return None
    try:
        width = float(raw.get("width") or 0)
        height = float(raw.get("height") or 0)
        if width <= 0 or height <= 0:
            return None
        x = float(raw.get("x") or 0)
        y = float(raw.get("y") or 0)
        return {
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "centerX": float(raw.get("centerX", raw.get("center_x", x + width / 2)) or (x + width / 2)),
            "centerY": float(raw.get("centerY", raw.get("center_y", y + height / 2)) or (y + height / 2)),
        }
    except Exception:
        return None


def _codex_agent_direction_from(options: Dict[str, Any], canvas_context: Optional[Dict[str, Any]]) -> str:
    data = _codex_agent_placement_options(options)
    values = []
    for key in ("side", "placement", "position", "direction", "anchor", "align"):
        if isinstance(data, dict) and data.get(key) is not None:
            values.append(str(data.get(key) or ""))
    ctx = canvas_context if isinstance(canvas_context, dict) else {}
    values.append(str(ctx.get("_agent_user_text") or ""))
    text = " ".join(values).lower()
    if re.search(r"\b(left|west)\b|左侧|左边|左方|左面|左上|左下|往左|放左|画布左|当前画面左", text):
        return "left"
    if re.search(r"\b(right|east)\b|右侧|右边|右方|右面|右上|右下|往右|放右|画布右|当前画面右", text):
        return "right"
    if re.search(r"\b(top|up|above|north)\b|上方|上面|顶部|往上|放上|画布上|当前画面上", text):
        return "top"
    if re.search(r"\b(bottom|down|below|south)\b|下方|下面|底部|往下|放下|画布下|当前画面下", text):
        return "bottom"
    if re.search(r"\b(center|middle)\b|中间|中央|居中", text):
        return "center"
    return "right"


def _codex_agent_placement_options(options: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(options or {}) if isinstance(options, dict) else {}
    placement = data.get("placement")
    if isinstance(placement, dict):
        merged = dict(placement)
        merged.update({k: v for k, v in data.items() if k != "placement"})
        return merged
    return data


def _codex_agent_placement_scope_from(options: Dict[str, Any], canvas_context: Optional[Dict[str, Any]]) -> str:
    data = _codex_agent_placement_options(options)
    direct = str(
        data.get("scope")
        or data.get("placement_scope")
        or data.get("coordinate_space")
        or data.get("relative_to")
        or ""
    ).strip().lower()
    if direct in {"viewport", "view", "current", "visible", "screen"}:
        return "viewport"
    if direct in {"global", "canvas", "board", "overview", "all"}:
        return "global"
    if direct in {"node", "anchor", "selection", "selected", "ref", "reference"}:
        return "node"

    ctx = canvas_context if isinstance(canvas_context, dict) else {}
    text = " ".join([
        str(data.get("side") or ""),
        str(data.get("placement") or ""),
        str(data.get("position") or ""),
        str(data.get("anchor") or ""),
        str(ctx.get("_agent_user_text") or ""),
    ]).lower()
    if re.search(r"当前|现在看到|可见|视口|眼前|这块|这片|当前画面|当前视野|屏幕里|visible|viewport|current view", text):
        return "viewport"
    if re.search(r"这张图|这幅图|这个图|这张|这个节点|选中|所选|节点|素材|ref[_-]?\d+|图\s*\d+|旁边|附近|挨着|相邻|node|selected|anchor", text):
        return "node"
    if re.search(r"全局|整个画布|全画布|所有内容|所有节点|整体|版图|总览|z\s*键|快捷键\s*z|缩放后的画布|global|overview|whole canvas", text):
        return "global"
    return "viewport"


def _codex_agent_bounds_from_rects(rects: List[Dict[str, float]]) -> Optional[Dict[str, float]]:
    if not rects:
        return None
    min_x = min(r["x"] for r in rects)
    min_y = min(r["y"] for r in rects)
    max_x = max(r["x"] + r["width"] for r in rects)
    max_y = max(r["y"] + r["height"] for r in rects)
    return {"x": min_x, "y": min_y, "width": max_x - min_x, "height": max_y - min_y}


def _codex_agent_viewport_local_bounds(visible: Dict[str, float], rects: List[Dict[str, float]]) -> Optional[Dict[str, float]]:
    if not visible or not rects:
        return None
    expanded = {
        "x": visible["x"] - visible["width"] * 0.15,
        "y": visible["y"] - visible["height"] * 0.15,
        "width": visible["width"] * 1.3,
        "height": visible["height"] * 1.3,
    }
    local = [rect for rect in rects if _codex_agent_rect_intersects(rect, expanded, 0)]
    return _codex_agent_bounds_from_rects(local)


def _codex_agent_anchor_rect_from(
    options: Dict[str, Any],
    nodes: List[Dict[str, Any]],
    refs: List[Dict[str, Any]],
    selected_ids: List[str],
    canvas_context: Optional[Dict[str, Any]],
    context_rects: Optional[Dict[str, Dict[str, float]]] = None,
) -> Optional[Dict[str, float]]:
    data = _codex_agent_placement_options(options)
    ctx = canvas_context if isinstance(canvas_context, dict) else {}
    text = str(ctx.get("_agent_user_text") or "")
    probe = dict(data)
    for key in ("anchor_node_id", "anchorNodeId", "target_node_id", "targetNodeId"):
        if data.get(key):
            probe["node_id"] = data.get(key)
            break
    if not any(probe.get(key) for key in ("node_id", "nodeId", "id", "ref", "ref_id", "refId")):
        m = re.search(r"ref[_-]?(\d+)|图\s*(\d+)", text, re.I)
        if m:
            probe["ref"] = f"ref_{m.group(1) or m.group(2)}"
        elif re.search(r"选中|所选|这张图|这个节点|这张|这个图|素材", text):
            probe["selected"] = True
    ids = _codex_agent_resolve_node_ids(probe, nodes, refs, selected_ids)
    rects = [_codex_agent_node_rect(node, context_rects) for node in nodes if str(node.get("id")) in set(ids)]
    return _codex_agent_bounds_from_rects(rects)


def _codex_agent_rect_intersects(a: Dict[str, float], b: Dict[str, float], pad: float = 36) -> bool:
    return not (
        a["x"] + a["width"] + pad <= b["x"]
        or b["x"] + b["width"] + pad <= a["x"]
        or a["y"] + a["height"] + pad <= b["y"]
        or b["y"] + b["height"] + pad <= a["y"]
    )


def _codex_agent_viewport_overlap_score(rect: Dict[str, float], visible: Optional[Dict[str, float]]) -> float:
    if not visible:
        return 0.0
    x1 = max(rect["x"], visible["x"])
    y1 = max(rect["y"], visible["y"])
    x2 = min(rect["x"] + rect["width"], visible["x"] + visible["width"])
    y2 = min(rect["y"] + rect["height"], visible["y"] + visible["height"])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    return (x2 - x1) * (y2 - y1)


def _codex_agent_new_node_point(
    canvas: Dict[str, Any],
    index: int,
    total: int,
    options: Dict[str, Any],
    width: float = 220,
    height: float = 220,
    canvas_context: Optional[Dict[str, Any]] = None,
    refs: Optional[List[Dict[str, Any]]] = None,
    selected_ids: Optional[List[str]] = None,
    context_rects: Optional[Dict[str, Dict[str, float]]] = None,
) -> Dict[str, int]:
    nodes = canvas.get("nodes") or []
    placement = _codex_agent_placement_options(options)
    batch_vertical = str(placement.get("batch_layout") or placement.get("batchLayout") or "").strip().lower() in {"vertical", "column", "stack"}
    if placement.get("x") is not None or placement.get("y") is not None:
        base_x = float(placement.get("x") or 0)
        base_y = float(placement.get("y") or 0)
        cols = max(1, int(placement.get("cols") or (1 if batch_vertical else min(max(total, 1), 3))))
        cell_x = float(placement.get("cellX") or 360)
        cell_y = float(placement.get("cellY") or 280)
        return {"x": round(base_x + (index % cols) * cell_x), "y": round(base_y + (index // cols) * cell_y)}

    visible = _codex_agent_visible_world(canvas_context)
    scope = _codex_agent_placement_scope_from(placement, canvas_context)
    direction = _codex_agent_direction_from(placement, canvas_context)
    pad = float(placement.get("pad") or 56)
    cell_x = float(placement.get("cellX") or max(360, width + 110))
    # Collision checks keep 72px clear around a node. The old 240px node +
    # 70px cell was two pixels too tight, so every adjacent batch row looked
    # occupied and the resolver skipped to every other row.
    batch_height = max(260, height)
    cell_y = float(placement.get("cellY") or max(260, batch_height + (84 if batch_vertical else 70)))
    existing = [_codex_agent_node_rect(node, context_rects) for node in nodes]

    def choose(candidates: List[Dict[str, float]], local_anchor: Optional[Dict[str, float]] = None) -> Optional[Dict[str, int]]:
        open_candidates = []
        for point in candidates:
            rect = {"x": point["x"], "y": point["y"], "width": width, "height": height}
            if not any(_codex_agent_rect_intersects(rect, other, 72) for other in existing):
                open_candidates.append(point)
        if not open_candidates:
            return None
        if local_anchor:
            anchor_cx = local_anchor["x"] + local_anchor["width"] / 2
            anchor_cy = local_anchor["y"] + local_anchor["height"] / 2
            open_candidates.sort(key=lambda p: (abs((p["x"] + width / 2) - anchor_cx) + abs((p["y"] + height / 2) - anchor_cy), p["y"], p["x"]))
        # Newly created siblings have already been inserted into `existing`.
        # For a vertical batch, the first open candidate is therefore the next
        # adjacent slot; applying `index` again skips rows and scatters a batch.
        chosen = open_candidates[0] if batch_vertical else open_candidates[min(index, max(0, len(open_candidates) - 1))]
        return {"x": round(chosen["x"]), "y": round(chosen["y"])}

    if scope == "node":
        anchor = _codex_agent_anchor_rect_from(placement, nodes, refs or [], selected_ids or [], canvas_context, context_rects)
        if anchor:
            rows = max(1, min(8, total + 3))
            cols = max(1, min(8, total + 3))
            candidates: List[Dict[str, float]] = []
            if direction == "left":
                base_x = anchor["x"] - width - pad
                base_y = anchor["y"]
                candidates = [{"x": base_x - step * cell_x, "y": base_y + row * cell_y} for step in range(0, 3) for row in range(rows)]
            elif direction == "top":
                base_x = anchor["x"]
                base_y = anchor["y"] - height - pad
                candidates = [{"x": base_x + col * cell_x, "y": base_y - step * cell_y} for step in range(0, 3) for col in range(cols)]
            elif direction == "bottom":
                base_x = anchor["x"]
                base_y = anchor["y"] + anchor["height"] + pad
                candidates = [{"x": base_x + col * cell_x, "y": base_y + step * cell_y} for step in range(0, 3) for col in range(cols)]
            elif direction == "center":
                candidates = [{"x": anchor["x"] + anchor["width"] / 2 - width / 2, "y": anchor["y"] + anchor["height"] / 2 - height / 2}]
            else:
                base_x = anchor["x"] + anchor["width"] + pad
                base_y = anchor["y"]
                candidates = [{"x": base_x + step * cell_x, "y": base_y + row * cell_y} for step in range(0, 3) for row in range(rows)]
            picked = choose(candidates, anchor)
            if picked:
                return picked

    global_bounds = _codex_agent_bounds_from_rects(existing)
    if scope == "global" and global_bounds:
        rows = max(1, min(10, total + 4))
        cols = max(1, min(10, total + 4))
        candidates: List[Dict[str, float]] = []
        if direction == "left":
            candidates = [{"x": global_bounds["x"] - width - pad - step * cell_x, "y": global_bounds["y"] + row * cell_y} for step in range(0, 3) for row in range(rows)]
        elif direction == "top":
            candidates = [{"x": global_bounds["x"] + col * cell_x, "y": global_bounds["y"] - height - pad - step * cell_y} for step in range(0, 3) for col in range(cols)]
        elif direction == "bottom":
            candidates = [{"x": global_bounds["x"] + col * cell_x, "y": global_bounds["y"] + global_bounds["height"] + pad + step * cell_y} for step in range(0, 3) for col in range(cols)]
        elif direction == "center":
            candidates = [{"x": global_bounds["x"] + global_bounds["width"] / 2 - width / 2, "y": global_bounds["y"] + global_bounds["height"] / 2 - height / 2}]
        else:
            candidates = [{"x": global_bounds["x"] + global_bounds["width"] + pad + step * cell_x, "y": global_bounds["y"] + row * cell_y} for step in range(0, 3) for row in range(rows)]
        picked = choose(candidates, global_bounds)
        if picked:
            return picked

    if visible:
        local_bounds = _codex_agent_viewport_local_bounds(visible, existing)
        if local_bounds:
            rows = max(1, min(12, int(local_bounds["height"] // max(1, cell_y)) + total + 2))
            cols = max(1, min(12, int(local_bounds["width"] // max(1, cell_x)) + total + 2))
            candidates: List[Dict[str, float]] = []
            if direction == "left":
                candidates = [
                    {"x": local_bounds["x"] - width - pad - step * cell_x, "y": local_bounds["y"] + row * cell_y}
                    for step in range(0, 6)
                    for row in range(rows)
                ]
            elif direction == "top":
                candidates = [
                    {"x": local_bounds["x"] + col * cell_x, "y": local_bounds["y"] - height - pad - step * cell_y}
                    for step in range(0, 6)
                    for col in range(cols)
                ]
            elif direction == "bottom":
                candidates = [
                    {"x": local_bounds["x"] + col * cell_x, "y": local_bounds["y"] + local_bounds["height"] + pad + step * cell_y}
                    for step in range(0, 6)
                    for col in range(cols)
                ]
            elif direction == "center":
                candidates = [{"x": local_bounds["x"] + local_bounds["width"] / 2 - width / 2, "y": local_bounds["y"] + local_bounds["height"] / 2 - height / 2}]
            else:
                candidates = [
                    {"x": local_bounds["x"] + local_bounds["width"] + pad + step * cell_x, "y": local_bounds["y"] + row * cell_y}
                    for step in range(0, 6)
                    for row in range(rows)
                ]
            picked = choose(candidates, local_bounds)
            if picked:
                return picked

        view_x = visible["x"]
        view_y = visible["y"]
        view_w = visible["width"]
        view_h = visible["height"]
        band_w = max(width + pad * 2, min(view_w, view_w * 0.38))
        band_h = max(height + pad * 2, min(view_h, view_h * 0.38))

        if direction == "left":
            x0, x1 = view_x + pad, min(view_x + band_w, view_x + view_w - width - pad)
            y0, y1 = view_y + pad, view_y + view_h - height - pad
        elif direction == "top":
            x0, x1 = view_x + pad, view_x + view_w - width - pad
            y0, y1 = view_y + pad, min(view_y + band_h, view_y + view_h - height - pad)
        elif direction == "bottom":
            x0, x1 = view_x + pad, view_x + view_w - width - pad
            y0, y1 = max(view_y + pad, view_y + view_h - band_h), view_y + view_h - height - pad
        elif direction == "center":
            center_x = visible["centerX"] - width / 2
            center_y = visible["centerY"] - height / 2
            x0, x1 = center_x - cell_x, center_x + cell_x
            y0, y1 = center_y - cell_y, center_y + cell_y
        else:
            x0, x1 = max(view_x + pad, view_x + view_w - band_w), view_x + view_w - width - pad
            y0, y1 = view_y + pad, view_y + view_h - height - pad

        if x1 < x0:
            x0 = x1 = visible["centerX"] - width / 2
        if y1 < y0:
            y0 = y1 = visible["centerY"] - height / 2

        candidates: List[Dict[str, float]] = []
        if batch_vertical and direction in {"left", "right"}:
            # Keep same-batch outputs in one nearby column. If the column is
            # occupied, `choose` moves to the next available row before using
            # any farther column.
            column_x = x0 if direction == "left" else x1
            y = y0
            while y <= y1 + 1 and len(candidates) < 80:
                candidates.append({"x": column_x, "y": y})
                y += cell_y
        else:
            y = y0
            while y <= y1 + 1 and len(candidates) < 80:
                x_values: List[float] = []
                x = x0
                while x <= x1 + 1 and len(x_values) < 20:
                    x_values.append(x)
                    x += cell_x
                if direction in {"right", "bottom"}:
                    x_values = list(reversed(x_values))
                for x in x_values:
                    candidates.append({"x": x, "y": y})
                y += cell_y

        # If the requested side is crowded, step just outside the visible edge,
        # but stay close to the current viewport instead of chasing infinite-canvas bounds.
        if direction == "left":
            candidates.extend({"x": view_x - width - pad - step * cell_x, "y": view_y + pad + row * cell_y} for step in range(1, 3) for row in range(max(1, min(6, total + 2))))
        elif direction == "right":
            candidates.extend({"x": view_x + view_w + pad + (step - 1) * cell_x, "y": view_y + pad + row * cell_y} for step in range(1, 3) for row in range(max(1, min(6, total + 2))))
        elif direction == "top":
            candidates.extend({"x": view_x + pad + col * cell_x, "y": view_y - height - pad - step * cell_y} for step in range(1, 3) for col in range(max(1, min(6, total + 2))))
        elif direction == "bottom":
            candidates.extend({"x": view_x + pad + col * cell_x, "y": view_y + view_h + pad + (step - 1) * cell_y} for step in range(1, 3) for col in range(max(1, min(6, total + 2))))

        picked = choose(candidates, visible)
        if picked:
            return picked
        return {"x": round(visible["centerX"] - width / 2), "y": round(visible["centerY"] - height / 2)}

    elif nodes:
        rects = [_codex_agent_node_rect(node, context_rects) for node in nodes]
        base_x = max(r["x"] + r["width"] for r in rects) + 80
        base_y = min(r["y"] for r in rects)
    else:
        base_x = 120
        base_y = 120
    cols = max(1, int(options.get("cols") or (1 if batch_vertical else min(max(total, 1), 3)))) if isinstance(options, dict) else 3
    cell_x = float(options.get("cellX") or 360) if isinstance(options, dict) else 360
    cell_y = float(options.get("cellY") or (height + 84 if batch_vertical else 280)) if isinstance(options, dict) else 280
    return {"x": round(base_x + (index % cols) * cell_x), "y": round(base_y + (index // cols) * cell_y)}


def _codex_agent_next_vertical_batch_point(
    canvas: Dict[str, Any],
    origin: Dict[str, int],
    width: float,
    height: float,
    context_rects: Optional[Dict[str, Dict[str, float]]] = None,
) -> Dict[str, int]:
    """Find the next free sibling slot on a batch's fixed vertical baseline."""
    x = float(origin.get("x") or 0)
    base_y = float(origin.get("y") or 0)
    cell_y = max(260, height) + 84
    existing = [_codex_agent_node_rect(node, context_rects) for node in (canvas.get("nodes") or [])]
    for row in range(80):
        y = base_y + row * cell_y
        rect = {"x": x, "y": y, "width": width, "height": height}
        if not any(_codex_agent_rect_intersects(rect, other, 72) for other in existing):
            return {"x": round(x), "y": round(y)}
    return {"x": round(x), "y": round(base_y + 80 * cell_y)}


def _codex_agent_name_with_existing_ext(name: str, media: Dict[str, Any]) -> str:
    text = str(name or "").strip()
    if not text:
        return ""
    if re.search(r"\.[a-z0-9]{2,8}$", text, re.I):
        return text
    source = str(media.get("name") or media.get("url") or "").split("?", 1)[0]
    m = re.search(r"(\.[a-z0-9]{2,8})$", source, re.I)
    return f"{text}{m.group(1)}" if m else text


def _codex_agent_action_target_ids(
    action: Dict[str, Any],
    nodes: List[Dict[str, Any]],
    refs: List[Dict[str, Any]],
    selected_ids: List[str],
) -> List[str]:
    source_items = action.get("items") or action.get("nodes") or action.get("targets") or []
    ids: List[str] = []
    for item in (source_items if isinstance(source_items, list) else [source_items]):
        for node_id in _codex_agent_resolve_node_ids(item, nodes, refs, selected_ids):
            if node_id not in ids:
                ids.append(node_id)
    options = action.get("options") if isinstance(action.get("options"), dict) else {}
    scope = str(action.get("scope") or options.get("scope") or "").lower()
    if not ids:
        probe = dict(action)
        probe.update({key: value for key, value in options.items() if key not in probe})
        for node_id in _codex_agent_resolve_node_ids(probe, nodes, refs, selected_ids):
            if node_id not in ids:
                ids.append(node_id)
    if not ids and (options.get("selected") or scope == "selected"):
        ids = [node_id for node_id in selected_ids if any(str(node.get("id")) == node_id for node in nodes)]
    if not ids and (options.get("all") or scope in {"all", "canvas"}):
        ids = [str(node.get("id")) for node in nodes if node.get("id")]
    if not ids and selected_ids and scope != "viewport":
        ids = [node_id for node_id in selected_ids if any(str(node.get("id")) == node_id for node in nodes)]
    return ids


def _codex_agent_viewport_target_ids(
    nodes: List[Dict[str, Any]],
    canvas_context: Optional[Dict[str, Any]],
    context_rects: Dict[str, Dict[str, float]],
) -> List[str]:
    visible = _codex_agent_visible_world(canvas_context)
    if not visible:
        return []
    return [
        str(node.get("id"))
        for node in nodes
        if node.get("id") and _codex_agent_rect_intersects(_codex_agent_node_rect(node, context_rects), visible, 0)
    ]


def _codex_agent_media_ratio(node: Dict[str, Any], rect: Dict[str, float]) -> float:
    images = [item for item in (node.get("images") or []) if isinstance(item, dict)]
    if len(images) == 1:
        image = images[0]
        width = float(image.get("natural_w") or image.get("width") or image.get("w") or image.get("layout_w") or 0)
        height = float(image.get("natural_h") or image.get("height") or image.get("h") or image.get("layout_h") or 0)
        if width > 0 and height > 0:
            return width / height
    width = float(rect.get("width") or 0)
    height = float(rect.get("height") or 0)
    return width / height if width > 0 and height > 0 else 1.0


def _codex_agent_apply_size_policy(
    node: Dict[str, Any],
    rect: Dict[str, float],
    media_mode: str,
    non_media_mode: str,
) -> bool:
    node_type = str(node.get("type") or "smart-image")
    images = [item for item in (node.get("images") or []) if isinstance(item, dict)]
    is_media = node_type == "smart-image"
    before = (node.get("w"), node.get("h"), node.get("scale"))
    if is_media and len(images) == 1 and media_mode == "standard":
        ratio = max(0.01, _codex_agent_media_ratio(node, rect))
        if ratio > 1.001:
            width, height = 520, max(72, round(520 / ratio))
        elif ratio < 0.999:
            height, width = 440, max(72, round(440 * ratio))
        else:
            width = height = 440
        node["w"], node["h"], node["scale"] = width, height, 1
    elif is_media and media_mode in {"standard", "reset"}:
        node.pop("w", None)
        node.pop("h", None)
        if len(images) > 1:
            node["scale"] = 0.8
        elif node.get("scale") is not None:
            node["scale"] = 2
    elif not is_media and non_media_mode == "reset":
        node.pop("w", None)
        node.pop("h", None)
        if node.get("scale") is not None:
            node["scale"] = 1
    return before != (node.get("w"), node.get("h"), node.get("scale"))


def _codex_agent_layout_positions(
    target_nodes: List[Dict[str, Any]],
    context_rects: Dict[str, Dict[str, float]],
    mode: str,
    options: Dict[str, Any],
    connections: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Tuple[Dict[str, Any], float, float]], Dict[str, float]]:
    ordered = sorted(target_nodes, key=lambda node: (float(node.get("y") or 0), float(node.get("x") or 0), str(node.get("id") or "")))
    by_id = {str(node.get("id")): node for node in ordered}
    edge_rows = _codex_agent_graph_rows(connections or [], set(by_id))
    if edge_rows:
        incoming = {node_id: 0 for node_id in by_id}
        outgoing = {node_id: [] for node_id in by_id}
        for source, target in edge_rows:
            incoming[target] += 1
            outgoing[source].append(target)
        position_key = lambda node_id: (float(by_id[node_id].get("y") or 0), float(by_id[node_id].get("x") or 0), node_id)
        queue = sorted((node_id for node_id, count in incoming.items() if count == 0), key=position_key)
        ordered_ids: List[str] = []
        while queue:
            node_id = queue.pop(0)
            if node_id in ordered_ids:
                continue
            ordered_ids.append(node_id)
            for next_id in sorted(outgoing[node_id], key=position_key):
                incoming[next_id] -= 1
                if incoming[next_id] == 0:
                    queue.append(next_id)
            queue.sort(key=position_key)
        for node_id in sorted(by_id, key=position_key):
            if node_id not in ordered_ids:
                ordered_ids.append(node_id)
        ordered = [by_id[node_id] for node_id in ordered_ids]
    rects = {str(node.get("id")): _codex_agent_node_rect(node, context_rects) for node in ordered}
    gap_x = max(0, float(options.get("gapX") if options.get("gapX") is not None else 80))
    gap_y = max(0, float(options.get("gapY") if options.get("gapY") is not None else 54))
    changes: List[Tuple[Dict[str, Any], float, float]] = []
    if mode in {"vertical", "column"}:
        max_width = max(rects[str(node.get("id"))]["width"] for node in ordered)
        cursor_y = 0.0
        for node in ordered:
            rect = rects[str(node.get("id"))]
            changes.append((node, (max_width - rect["width"]) / 2, cursor_y))
            cursor_y += rect["height"] + gap_y
    elif mode == "grid":
        cols = max(1, min(12, int(options.get("cols") or math.ceil(math.sqrt(len(ordered))))))
        rows = math.ceil(len(ordered) / cols)
        col_widths = [0.0] * cols
        row_heights = [0.0] * rows
        for index, node in enumerate(ordered):
            rect = rects[str(node.get("id"))]
            col_widths[index % cols] = max(col_widths[index % cols], rect["width"])
            row_heights[index // cols] = max(row_heights[index // cols], rect["height"])
        col_x, row_y = [], []
        cursor = 0.0
        for width in col_widths:
            col_x.append(cursor)
            cursor += width + gap_x
        cursor = 0.0
        for height in row_heights:
            row_y.append(cursor)
            cursor += height + gap_y
        for index, node in enumerate(ordered):
            col, row = index % cols, index // cols
            rect = rects[str(node.get("id"))]
            changes.append((node, col_x[col] + (col_widths[col] - rect["width"]) / 2, row_y[row] + (row_heights[row] - rect["height"]) / 2))
    else:
        max_height = max(rects[str(node.get("id"))]["height"] for node in ordered)
        cursor_x = 0.0
        for node in ordered:
            rect = rects[str(node.get("id"))]
            changes.append((node, cursor_x, (max_height - rect["height"]) / 2))
            cursor_x += rect["width"] + gap_x
    placed = [
        {"x": x, "y": y, "width": rects[str(node.get("id"))]["width"], "height": rects[str(node.get("id"))]["height"]}
        for node, x, y in changes
    ]
    bounds = _codex_agent_bounds_from_rects(placed) or {"x": 0, "y": 0, "width": 1, "height": 1}
    return changes, bounds


def _codex_agent_find_block_origin(
    nodes: List[Dict[str, Any]],
    moving_ids: List[str],
    block: Dict[str, float],
    options: Dict[str, Any],
    canvas_context: Optional[Dict[str, Any]],
    refs: List[Dict[str, Any]],
    selected_ids: List[str],
    context_rects: Dict[str, Dict[str, float]],
) -> Optional[Dict[str, int]]:
    moving = set(moving_ids)
    obstacles = [_codex_agent_node_rect(node, context_rects) for node in nodes if str(node.get("id")) not in moving]
    width, height = max(1, block["width"]), max(1, block["height"])
    if options.get("x") is not None or options.get("y") is not None:
        return {"x": round(float(options.get("x") if options.get("x") is not None else block.get("x") or 0)), "y": round(float(options.get("y") if options.get("y") is not None else block.get("y") or 0))}
    scope = str(options.get("placement_scope") or "").lower()
    side = str(options.get("side") or "center").lower()
    gap_x = max(0, float(options.get("gapX") if options.get("gapX") is not None else 80))
    gap_y = max(0, float(options.get("gapY") if options.get("gapY") is not None else 54))

    def open_point(x: float, y: float) -> bool:
        rect = {"x": x, "y": y, "width": width, "height": height}
        return not any(_codex_agent_rect_intersects(rect, other, 24) for other in obstacles)

    anchor: Optional[Dict[str, float]] = None
    if scope == "node" or options.get("anchor_node_id") or options.get("anchor_ref"):
        anchor_options = dict(options)
        if options.get("anchor_node_id"):
            anchor_options["node_id"] = options.get("anchor_node_id")
        if options.get("anchor_ref"):
            anchor_options["ref"] = options.get("anchor_ref")
        anchor = _codex_agent_anchor_rect_from(anchor_options, nodes, refs, selected_ids, canvas_context, context_rects)
    if scope == "global":
        anchor = _codex_agent_bounds_from_rects(obstacles) or dict(block)
    visible = _codex_agent_visible_world(canvas_context)
    candidates: List[Tuple[float, float]] = []
    if anchor:
        if side == "left":
            base = (anchor["x"] - width - gap_x, anchor["y"] + (anchor["height"] - height) / 2)
            candidates = [(base[0] - col * (width + gap_x), base[1] + row * (height + gap_y)) for col in range(4) for row in [0, 1, -1, 2, -2]]
        elif side == "top":
            base = (anchor["x"] + (anchor["width"] - width) / 2, anchor["y"] - height - gap_y)
            candidates = [(base[0] + col * (width + gap_x), base[1] - row * (height + gap_y)) for row in range(4) for col in [0, 1, -1, 2, -2]]
        elif side == "bottom":
            base = (anchor["x"] + (anchor["width"] - width) / 2, anchor["y"] + anchor["height"] + gap_y)
            candidates = [(base[0] + col * (width + gap_x), base[1] + row * (height + gap_y)) for row in range(4) for col in [0, 1, -1, 2, -2]]
        elif side == "right":
            base = (anchor["x"] + anchor["width"] + gap_x, anchor["y"] + (anchor["height"] - height) / 2)
            candidates = [(base[0] + col * (width + gap_x), base[1] + row * (height + gap_y)) for col in range(4) for row in [0, 1, -1, 2, -2]]
        else:
            candidates = [(anchor["x"] + (anchor["width"] - width) / 2, anchor["y"] + (anchor["height"] - height) / 2)]
    elif scope == "viewport" and visible:
        step_x, step_y = max(80, min(width + gap_x, visible["width"] / 5)), max(80, min(height + gap_y, visible["height"] / 5))
        y = visible["y"] + 24
        while y + height <= visible["y"] + visible["height"] - 24 + 1:
            x = visible["x"] + 24
            while x + width <= visible["x"] + visible["width"] - 24 + 1:
                candidates.append((x, y))
                x += step_x
            y += step_y
        center_x, center_y = visible["centerX"] - width / 2, visible["centerY"] - height / 2
        if side == "left":
            candidates.sort(key=lambda point: (point[0], abs(point[1] - center_y)))
        elif side == "right":
            candidates.sort(key=lambda point: (-point[0], abs(point[1] - center_y)))
        elif side == "top":
            candidates.sort(key=lambda point: (point[1], abs(point[0] - center_x)))
        elif side == "bottom":
            candidates.sort(key=lambda point: (-point[1], abs(point[0] - center_x)))
        else:
            candidates.sort(key=lambda point: abs(point[0] - center_x) + abs(point[1] - center_y))
    for x, y in candidates:
        if open_point(x, y):
            return {"x": round(x), "y": round(y)}
    return None


def _codex_agent_graph_rows(connections: List[Dict[str, Any]], valid_ids: set[str]) -> List[Tuple[str, str]]:
    rows: List[Tuple[str, str]] = []
    for conn in connections:
        source = _codex_agent_connection_value(conn, ("source", "sourceId", "from", "fromNodeId", "start"))
        target = _codex_agent_connection_value(conn, ("target", "targetId", "to", "toNodeId", "end"))
        if source in valid_ids and target in valid_ids and source != target:
            rows.append((source, target))
    return rows


def _codex_agent_tree_data(root_id: str, nodes: List[Dict[str, Any]], connections: List[Dict[str, Any]], scope: str = "both") -> Dict[str, Any]:
    valid = {str(node.get("id")) for node in nodes if node.get("id")}
    edges = _codex_agent_graph_rows(connections, valid)
    outgoing: Dict[str, List[str]] = {node_id: [] for node_id in valid}
    for source, target in edges:
        outgoing[source].append(target)

    # Collapse directed cycles first. The resulting component graph is a DAG,
    # so longest-path layering is stable for branches, merges and feedback loops.
    index = 0
    stack: List[str] = []
    on_stack: set[str] = set()
    indices: Dict[str, int] = {}
    low: Dict[str, int] = {}
    components: List[List[str]] = []

    def visit(node_id: str) -> None:
        nonlocal index
        indices[node_id] = low[node_id] = index
        index += 1
        stack.append(node_id)
        on_stack.add(node_id)
        for next_id in outgoing.get(node_id, []):
            if next_id not in indices:
                visit(next_id)
                low[node_id] = min(low[node_id], low[next_id])
            elif next_id in on_stack:
                low[node_id] = min(low[node_id], indices[next_id])
        if low[node_id] == indices[node_id]:
            component: List[str] = []
            while stack:
                item = stack.pop()
                on_stack.discard(item)
                component.append(item)
                if item == node_id:
                    break
            components.append(component)

    for node_id in valid:
        if node_id not in indices:
            visit(node_id)
    component_of = {node_id: component_id for component_id, members in enumerate(components) for node_id in members}
    component_out: Dict[int, set[int]] = {component_id: set() for component_id in range(len(components))}
    component_in: Dict[int, set[int]] = {component_id: set() for component_id in range(len(components))}
    for source, target in edges:
        source_component, target_component = component_of[source], component_of[target]
        if source_component != target_component:
            component_out[source_component].add(target_component)
            component_in[target_component].add(source_component)
    root_component = component_of.get(root_id)
    if root_component is None:
        return {"ids": {root_id}, "levels": {root_id: 0}, "edges": []}

    def reachable(adjacency: Dict[int, set[int]]) -> set[int]:
        found = {root_component}
        queue = [root_component]
        while queue:
            current = queue.pop(0)
            for next_id in adjacency.get(current, set()):
                if next_id not in found:
                    found.add(next_id)
                    queue.append(next_id)
        return found

    def longest(adjacency: Dict[int, set[int]], included_components: set[int]) -> Dict[int, int]:
        indegree = {component_id: 0 for component_id in included_components}
        for source_component in included_components:
            for target_component in adjacency.get(source_component, set()):
                if target_component in indegree:
                    indegree[target_component] += 1
        queue = sorted(component_id for component_id, degree in indegree.items() if degree == 0)
        distance = {root_component: 0}
        while queue:
            current = queue.pop(0)
            for next_id in sorted(adjacency.get(current, set())):
                if next_id not in indegree:
                    continue
                if current in distance:
                    distance[next_id] = max(distance.get(next_id, 0), distance[current] + 1)
                indegree[next_id] -= 1
                if indegree[next_id] == 0:
                    queue.append(next_id)
        return distance

    forward_components = reachable(component_out) if scope in {"both", "downstream"} else {root_component}
    backward_components = reachable(component_in) if scope in {"both", "upstream"} else {root_component}
    forward_distance = longest(component_out, forward_components)
    backward_distance = longest(component_in, backward_components)
    included_components = forward_components | backward_components
    included = {node_id for node_id, component_id in component_of.items() if component_id in included_components}
    levels: Dict[str, int] = {}
    for node_id in included:
        component_id = component_of[node_id]
        if component_id == root_component:
            levels[node_id] = 0
        elif component_id in forward_distance:
            levels[node_id] = forward_distance[component_id]
        else:
            levels[node_id] = -backward_distance[component_id]
    return {
        "ids": included,
        "levels": levels,
        "edges": [(source, target) for source, target in edges if source in included and target in included],
    }


def _codex_agent_tree_layout(
    tree_nodes: List[Dict[str, Any]],
    levels: Dict[str, int],
    context_rects: Dict[str, Dict[str, float]],
    options: Dict[str, Any],
) -> Tuple[List[Tuple[Dict[str, Any], float, float]], Dict[str, float]]:
    gap_x = max(0, float(options.get("gapX") if options.get("gapX") is not None else 72))
    gap_y = max(0, float(options.get("gapY") if options.get("gapY") is not None else 42))
    columns: Dict[int, List[Dict[str, Any]]] = {}
    rects: Dict[str, Dict[str, float]] = {}
    for node in tree_nodes:
        node_id = str(node.get("id"))
        rects[node_id] = _codex_agent_node_rect(node, context_rects)
        columns.setdefault(int(levels.get(node_id, 0)), []).append(node)
    for layer_nodes in columns.values():
        layer_nodes.sort(key=lambda node: (float(node.get("y") or 0), float(node.get("x") or 0), str(node.get("id"))))
    layer_width = {level: max(rects[str(node.get("id"))]["width"] for node in layer_nodes) for level, layer_nodes in columns.items()}
    x_by_level: Dict[int, float] = {0: 0.0}
    positive = sorted(level for level in columns if level > 0)
    cursor = layer_width.get(0, 0.0) + gap_x
    for level in positive:
        x_by_level[level] = cursor
        cursor += layer_width[level] + gap_x
    cursor = -gap_x
    for level in sorted((level for level in columns if level < 0), reverse=True):
        cursor -= layer_width[level]
        x_by_level[level] = cursor
        cursor -= gap_x
    changes: List[Tuple[Dict[str, Any], float, float]] = []
    for level, layer_nodes in columns.items():
        total_height = sum(rects[str(node.get("id"))]["height"] for node in layer_nodes) + gap_y * max(0, len(layer_nodes) - 1)
        cursor_y = -total_height / 2
        for node in layer_nodes:
            rect = rects[str(node.get("id"))]
            x = x_by_level[level] + (layer_width[level] - rect["width"]) / 2
            changes.append((node, x, cursor_y))
            cursor_y += rect["height"] + gap_y
    placed = [{"x": x, "y": y, "width": rects[str(node.get("id"))]["width"], "height": rects[str(node.get("id"))]["height"]} for node, x, y in changes]
    bounds = _codex_agent_bounds_from_rects(placed) or {"x": 0, "y": 0, "width": 1, "height": 1}
    return changes, bounds


def _codex_agent_find_nearby_tree_origin(
    nodes: List[Dict[str, Any]],
    tree_ids: List[str],
    relative: List[Tuple[Dict[str, Any], float, float]],
    layout_bounds: Dict[str, float],
    root_id: str,
    options: Dict[str, Any],
    canvas_context: Optional[Dict[str, Any]],
    context_rects: Dict[str, Dict[str, float]],
) -> Optional[Dict[str, int]]:
    """Place a compact tree near its root without treating sparse columns as one solid block."""
    moving = set(tree_ids)
    obstacles = [_codex_agent_node_rect(node, context_rects) for node in nodes if str(node.get("id")) not in moving]
    planned: List[Dict[str, float]] = []
    planned_root: Optional[Dict[str, float]] = None
    original_root: Optional[Dict[str, float]] = None
    for node, x, y in relative:
        rect = _codex_agent_node_rect(node, context_rects)
        item = {"x": x, "y": y, "width": rect["width"], "height": rect["height"]}
        planned.append(item)
        if str(node.get("id")) == root_id:
            planned_root = item
            original_root = rect
    if not planned or not planned_root or not original_root:
        return None

    # Keep the selected root close to where the user invoked the command. The tree may be
    # larger than the viewport; nearby placement therefore deliberately has no viewport-fit
    # requirement.
    preferred = (
        original_root["x"] - planned_root["x"],
        original_root["y"] - planned_root["y"],
    )
    centers = [preferred]
    visible = _codex_agent_visible_world(canvas_context)
    if visible:
        centers.append((
            visible["centerX"] - (layout_bounds["x"] + layout_bounds["width"] / 2),
            visible["centerY"] - (layout_bounds["y"] + layout_bounds["height"] / 2),
        ))

    step_x = max(64.0, min(144.0, float(options.get("gapX") or 72)))
    step_y = max(64.0, min(120.0, float(options.get("gapY") or 42)))

    def open_translation(dx: float, dy: float) -> bool:
        for item in planned:
            probe = {
                "x": item["x"] + dx,
                "y": item["y"] + dy,
                "width": item["width"],
                "height": item["height"],
            }
            if any(_codex_agent_rect_intersects(probe, obstacle, 24) for obstacle in obstacles):
                return False
        return True

    seen: set[Tuple[int, int]] = set()
    for ring in range(37):
        offsets = [(0, 0)] if ring == 0 else [
            (x, y)
            for x in range(-ring, ring + 1)
            for y in range(-ring, ring + 1)
            if abs(x) == ring or abs(y) == ring
        ]
        offsets.sort(key=lambda point: abs(point[0]) + abs(point[1]))
        for center_x, center_y in centers:
            for offset_x, offset_y in offsets:
                dx = center_x + offset_x * step_x
                dy = center_y + offset_y * step_y
                key = (round(dx), round(dy))
                if key in seen:
                    continue
                seen.add(key)
                if open_translation(dx, dy):
                    return {
                        "x": round(layout_bounds["x"] + dx),
                        "y": round(layout_bounds["y"] + dy),
                    }
    return None


async def _codex_agent_apply_canvas_actions(
    canvas_id: str,
    actions: List[Dict[str, Any]],
    refs: List[Dict[str, Any]],
    canvas_context: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if not canvas_id:
        return {"ok": False, "message": "no canvas_id", "results": [], "changed": 0, "skipped": 0}
    raw_types = [_codex_agent_action_type(item) for item in actions if isinstance(item, dict)]
    if raw_types and all(action_type in {"undo_last_agent_action", "undo_agent_action"} for action_type in raw_types):
        snapshot_meta = canvas_context.get("_contextSnapshot") if isinstance(canvas_context, dict) and isinstance(canvas_context.get("_contextSnapshot"), dict) else {}
        expected_revision = snapshot_meta.get("canvas_revision")
        return await _codex_agent_undo_last_action(canvas_id, int(expected_revision) if expected_revision is not None else None)
    canvas = load_canvas(canvas_id)
    if normalize_canvas_kind(canvas.get("kind")) != "smart":
        return {"ok": False, "message": "only smart canvas is supported", "results": [], "changed": 0, "skipped": 0}
    revision_state = CODEX_AGENT_REVISION_STORE.observe(canvas_id, canvas)
    current_revision = int(revision_state.get("revision") or 0)
    snapshot_meta = canvas_context.get("_contextSnapshot") if isinstance(canvas_context, dict) and isinstance(canvas_context.get("_contextSnapshot"), dict) else {}
    snapshot_revision = snapshot_meta.get("canvas_revision")
    explicit_expected: Optional[int] = None
    broad_write = False
    for action in actions:
        if not isinstance(action, dict):
            continue
        options = action.get("options") if isinstance(action.get("options"), dict) else {}
        raw_expected = action.get("expected_revision", options.get("expected_revision"))
        if raw_expected is not None:
            try:
                explicit_expected = int(raw_expected)
            except (TypeError, ValueError):
                return {
                    "ok": False,
                    "conflict": True,
                    "message": "expected_revision 必须是非负整数",
                    "expected_revision": raw_expected,
                    "canvas_revision": current_revision,
                    "results": [],
                    "changed": 0,
                    "skipped": len(actions),
                }
            break
        action_type = _codex_agent_action_type(action)
        scope = str(action.get("scope") or options.get("scope") or "").strip().lower()
        target_items = action.get("items") or action.get("nodes") or action.get("targets") or []
        target_count = len(target_items) if isinstance(target_items, list) else 1
        if action_type in {"delete_nodes", "undo_last_agent_action", "undo_agent_action", "arrange_node_tree", "arrange_tree", "layout_node_tree"} or scope in {"canvas", "all"} or target_count >= 3:
            broad_write = True
    expected_revision = explicit_expected
    if (
        explicit_expected is not None
        and snapshot_revision is not None
        and bool(snapshot_meta.get("working_revision_advanced"))
        and int(explicit_expected) < int(snapshot_revision)
    ):
        # The model may repeat the send-time revision even after an earlier Tool
        # in this same turn succeeded. The advanced private snapshot is the safe
        # baseline here; a later external write still has a higher live revision
        # and therefore continues to conflict below.
        expected_revision = int(snapshot_revision)
    if expected_revision is None and broad_write and snapshot_revision is not None:
        expected_revision = int(snapshot_revision)
    try:
        CODEX_AGENT_REVISION_STORE.assert_expected(canvas_id, canvas, expected_revision)
    except _CanvasRevisionConflict as exc:
        return {
            "ok": False,
            "conflict": True,
            "message": "画布已在 Agent 查询后发生变化，请重新查询后再执行。",
            "expected_revision": exc.expected,
            "canvas_revision": exc.current,
            "results": [],
            "changed": 0,
            "skipped": len(actions),
        }
    nodes = canvas.setdefault("nodes", [])
    connections = canvas.setdefault("connections", [])
    undo_snapshot = {"nodes": json.loads(json.dumps(nodes)), "connections": json.loads(json.dumps(connections))}
    selected_ids = _codex_agent_selected_ids(canvas_context)
    context_rects = _codex_agent_context_node_rects(canvas_context)
    results: List[Dict[str, Any]] = []
    changed = 0
    skipped = 0

    # Legacy add_nodes accepted heterogeneous node descriptors. Normalize it once
    # into the same canonical creation actions used by native Tools.
    normalized_actions: List[Dict[str, Any]] = []
    for raw_action in actions:
        action = raw_action if isinstance(raw_action, dict) else {}
        action_type = _codex_agent_action_type(action)
        if action_type not in {"add_node", "add_nodes"}:
            normalized_actions.append(action)
            continue
        source_items = action.get("items") or action.get("nodes") or []
        source_list = source_items if isinstance(source_items, list) else [source_items]
        for item in source_list:
            data = {"text": item} if isinstance(item, str) else (dict(item) if isinstance(item, dict) else {})
            kind = str(data.get("node_type") or data.get("type") or data.get("kind") or "").lower().replace("-", "_")
            if kind in {"loop", "smart_loop"}:
                mapped = "add_loop"
            elif kind in {"video_generation", "generate_video", "video"} and (data.get("prompt") or data.get("text")):
                mapped = "create_video_generation_node"
            elif kind in {"image_generation", "generate_image", "image"} and (data.get("prompt") or data.get("text")):
                mapped = "create_image_generation_node"
            elif kind in {"media", "asset", "file"} or data.get("url") or data.get("path") or data.get("src"):
                mapped = "add_media"
            elif kind in {"text", "note"}:
                mapped = "add_text"
            else:
                mapped = "add_prompt"
            normalized_actions.append({"type": mapped, "items": [data], "options": action.get("options") if isinstance(action.get("options"), dict) else {}})

    def node_by_id(node_id: str) -> Optional[Dict[str, Any]]:
        return next((node for node in nodes if str(node.get("id")) == str(node_id)), None)

    def result(action_type: str, status: str, message: str = "", items: Optional[List[Any]] = None) -> None:
        nonlocal skipped
        if status == "skipped":
            skipped += 1
        results.append({"type": action_type, "status": status, "message": message, "items": items or []})

    for raw_action in normalized_actions:
        action = raw_action if isinstance(raw_action, dict) else {}
        action_type = str(action.get("type") or action.get("action") or "").lower().replace("-", "_")
        options = action.get("options") if isinstance(action.get("options"), dict) else {}
        try:
            if action_type in {"remember_preference", "remember_preferences", "save_preference"}:
                note = str(action.get("note") or action.get("text") or action.get("content") or action.get("preference") or "").strip()
                if note:
                    _codex_agent_append_preference(note)
                    result(action_type, "done", "preference saved")
                else:
                    result(action_type, "skipped", "empty preference")
                continue

            if action_type in {"delete_node", "delete_nodes", "remove_node", "remove_nodes"}:
                source_items = action.get("items") or action.get("nodes") or action.get("targets") or [action]
                target_ids: List[str] = []
                for item in (source_items if isinstance(source_items, list) else [source_items]):
                    for node_id in _codex_agent_resolve_node_ids(item if isinstance(item, dict) else {"ref": item}, nodes, refs, selected_ids):
                        if node_id not in target_ids:
                            target_ids.append(node_id)
                # Native tool callers commonly express "删除选中节点" without
                # repeating opaque node ids. Treat an omitted target as the
                # captured selection, instead of silently returning no-op.
                if not target_ids:
                    target_ids = [node_id for node_id in selected_ids if node_by_id(node_id)]
                deleted = [node for node in nodes if str(node.get("id") or "") in set(target_ids)]
                if not deleted:
                    result(action_type, "skipped", "no selected nodes to delete")
                    continue
                deleted_ids = {str(node.get("id") or "") for node in deleted}
                nodes[:] = [node for node in nodes if str(node.get("id") or "") not in deleted_ids]
                connections[:] = [conn for conn in connections if _codex_agent_connection_value(conn, ("source", "sourceId", "from", "fromNodeId", "start")) not in deleted_ids and _codex_agent_connection_value(conn, ("target", "targetId", "to", "toNodeId", "end")) not in deleted_ids]
                for node in nodes:
                    if isinstance(node.get("inputNodeIds"), list):
                        node["inputNodeIds"] = [node_id for node_id in node["inputNodeIds"] if str(node_id) not in deleted_ids]
                    if node.get("type") == "smart-group" and isinstance(node.get("items"), list):
                        node["items"] = [node_id for node_id in node["items"] if str(node_id) not in deleted_ids]
                changed += len(deleted)
                result(action_type, "done", f"deleted {len(deleted)} nodes", [_codex_agent_node_summary(node) for node in deleted])
                continue

            if action_type in {"add_media", "add_image", "add_video", "add_media_nodes"}:
                source_items = action.get("items") or action.get("media") or action.get("images") or action.get("videos") or [action]
                items = source_items if isinstance(source_items, list) else [source_items]
                created = []
                for index, item in enumerate(items):
                    data = {"url": item} if isinstance(item, str) else (item if isinstance(item, dict) else {})
                    raw_url = str(data.get("url") or data.get("path") or data.get("src") or "").strip()
                    if not raw_url:
                        continue
                    url = _codex_agent_canvas_url_for_path(raw_url)
                    media = {
                        "url": url,
                        "name": data.get("name") or _codex_agent_name_from_url(raw_url),
                        "kind": data.get("kind") or data.get("mediaKind") or _codex_agent_media_kind(raw_url),
                    }
                    if data.get("prompt"):
                        media["prompt"] = str(data.get("prompt"))
                    point = _codex_agent_new_node_point(canvas, index, len(items), options, 220, 220, canvas_context, refs, selected_ids, context_rects)
                    node = {
                        "id": _codex_agent_uid("smart"),
                        "type": "smart-image",
                        "x": point["x"],
                        "y": point["y"],
                        "title": "Image",
                        "images": [media],
                        "scale": 1,
                        "created_at": _codex_agent_now(),
                    }
                    nodes.append(node)
                    created.append(_codex_agent_node_summary(node))
                changed += len(created)
                result(action_type, "done" if created else "skipped", f"created {len(created)} media nodes", created)
                continue

            if action_type in {"add_prompt", "add_text", "add_prompt_nodes"}:
                source_items = action.get("items") or action.get("prompts") or action.get("texts") or [action]
                items = source_items if isinstance(source_items, list) else [source_items]
                created = []
                for index, item in enumerate(items):
                    data = {"text": item} if isinstance(item, str) else (item if isinstance(item, dict) else {})
                    text = str(data.get("text") or data.get("prompt") or "").strip()
                    default_title = "Text" if action_type == "add_text" else "Prompt"
                    title = str(data.get("title") or default_title).strip() or default_title
                    if not text and not title:
                        continue
                    point = _codex_agent_new_node_point(canvas, index, len(items), options, 316, 240, canvas_context, refs, selected_ids, context_rects)
                    node = {
                        "id": _codex_agent_uid("prompt"),
                        "type": "smart-prompt",
                        "x": point["x"],
                        "y": point["y"],
                        "w": 316,
                        "h": 240,
                        "title": title,
                        "text": text,
                        "promptSeparator": ";",
                        "promptSplitEnabled": False,
                        "llmEnabled": False,
                        "llmSystemEnabled": False,
                        "llmSystemPrompt": "You are a helpful prompt assistant.",
                        "llmInstruction": "",
                        "created_at": _codex_agent_now(),
                    }
                    nodes.append(node)
                    created.append(_codex_agent_node_summary(node))
                changed += len(created)
                result(action_type, "done" if created else "skipped", f"created {len(created)} prompt nodes", created)
                continue

            if action_type in {"add_loop", "add_loop_nodes"}:
                source_items = action.get("items") or action.get("loops") or [action]
                items = source_items if isinstance(source_items, list) else [source_items]
                created = []
                for index, item in enumerate(items):
                    data = {"variablePrompt": item} if isinstance(item, str) else (item if isinstance(item, dict) else {})
                    point = _codex_agent_new_node_point(canvas, index, len(items), options, 340, 168, canvas_context, refs, selected_ids, context_rects)
                    node = {
                        "id": _codex_agent_uid("loop"),
                        "type": "smart-loop",
                        "x": point["x"],
                        "y": point["y"],
                        "w": 340,
                        "h": 168,
                        "title": str(data.get("title") or "Loop"),
                        "count": max(1, int(float(data.get("count") or 1))),
                        "mode": data.get("mode") or "serial",
                        "showPrompt": False,
                        "imageInput": False,
                        "loopStart": 1,
                        "imageBatchSize": 1,
                        "variablePrompt": str(data.get("variablePrompt") or data.get("text") or data.get("prompt") or ""),
                        "created_at": _codex_agent_now(),
                    }
                    nodes.append(node)
                    created.append(_codex_agent_node_summary(node))
                changed += len(created)
                result(action_type, "done" if created else "skipped", f"created {len(created)} loop nodes", created)
                continue

            if action_type in {"rename_node", "rename_nodes", "set_node_title", "set_node_titles"}:
                source_items = action.get("items") or action.get("nodes") or action.get("targets") or [action]
                items = source_items if isinstance(source_items, list) else [source_items]
                touched = []
                missing = 0
                for item in items:
                    data = item if isinstance(item, dict) else {"ref": item}
                    # Native strict schemas historically exposed the global rename
                    # value under options.name. Accept that form as well as the
                    # preferred root/items form so a valid Tool call cannot turn
                    # into a silent no-op.
                    name = str(data.get("name") or data.get("title") or data.get("label") or data.get("text") or options.get("name") or options.get("title") or "").strip()
                    if not name:
                        continue
                    # The filename-like label above an image is images[index].name.
                    # Internal node.title is a type/fallback label and is not an
                    # Agent rename target, even if an older model sends field:title.
                    wants_title = False
                    ids = _codex_agent_resolve_node_ids(data, nodes, refs, selected_ids)
                    if not ids:
                        missing += 1
                    for node_id in ids:
                        node = node_by_id(node_id)
                        if not node:
                            missing += 1
                            continue
                        image_index_raw = data.get("image_index", data.get("imageIndex", options.get("image_index")))
                        image_index = int(float(image_index_raw)) if image_index_raw is not None and str(image_index_raw) != "" else (0 if len(node.get("images") or []) == 1 else -1)
                        if wants_title:
                            node["title"] = name
                            touched.append(_codex_agent_node_summary(node))
                        elif 0 <= image_index < len(node.get("images") or []):
                            node["images"][image_index]["name"] = _codex_agent_name_with_existing_ext(name, node["images"][image_index])
                            touched.append(_codex_agent_node_summary(node))
                        else:
                            missing += 1
                changed += len(touched)
                result(action_type, "done" if touched else "skipped", f"renamed {len(touched)} nodes, skipped {missing}", touched)
                skipped += missing
                continue

            if action_type in {"move_node", "move_nodes", "position_node", "position_nodes"}:
                ids = _codex_agent_action_target_ids(action, nodes, refs, selected_ids)
                if not ids and str(options.get("scope") or action.get("scope") or "").lower() == "viewport":
                    ids = _codex_agent_viewport_target_ids(nodes, canvas_context, context_rects)
                target_nodes = [node_by_id(node_id) for node_id in ids]
                target_nodes = [node for node in target_nodes if node]
                touched: List[Dict[str, Any]] = []
                placement_requested = any(options.get(key) is not None for key in ("placement_scope", "side", "anchor_node_id", "anchor_ref", "x", "y"))
                if target_nodes and placement_requested:
                    rects = [_codex_agent_node_rect(node, context_rects) for node in target_nodes]
                    bounds = _codex_agent_bounds_from_rects(rects) or {"x": 0, "y": 0, "width": 1, "height": 1}
                    origin = _codex_agent_find_block_origin(nodes, ids, bounds, options, canvas_context, refs, selected_ids, context_rects)
                    if not origin:
                        result(action_type, "skipped", "no_space: requested destination has no collision-free area")
                        continue
                    dx, dy = origin["x"] - bounds["x"], origin["y"] - bounds["y"]
                    for node in target_nodes:
                        node["x"] = round(float(node.get("x") or 0) + dx)
                        node["y"] = round(float(node.get("y") or 0) + dy)
                        touched.append(_codex_agent_node_summary(node))
                else:
                    source_items = action.get("items") or action.get("nodes") or action.get("targets") or [action]
                    items = source_items if isinstance(source_items, list) else [source_items]
                    for item in items:
                        data = item if isinstance(item, dict) else {"ref": item}
                        for node_id in _codex_agent_resolve_node_ids(data, nodes, refs, selected_ids):
                            node = node_by_id(node_id)
                            if not node:
                                continue
                            has_x = data.get("x") is not None
                            has_y = data.get("y") is not None
                            dx = float(data.get("dx", data.get("offset_x", data.get("offsetX", 0))) or 0)
                            dy = float(data.get("dy", data.get("offset_y", data.get("offsetY", 0))) or 0)
                            node["x"] = round(float(data.get("x")) if has_x else float(node.get("x") or 0) + dx)
                            node["y"] = round(float(data.get("y")) if has_y else float(node.get("y") or 0) + dy)
                            touched.append(_codex_agent_node_summary(node))
                changed += len(touched)
                result(action_type, "done" if touched else "skipped", f"moved {len(touched)} nodes", touched)
                continue

            if action_type in {"resize_node", "resize_nodes"}:
                ids = _codex_agent_action_target_ids(action, nodes, refs, selected_ids)
                if not ids and str(options.get("scope") or action.get("scope") or "").lower() == "viewport":
                    ids = _codex_agent_viewport_target_ids(nodes, canvas_context, context_rects)
                target_nodes = [node_by_id(node_id) for node_id in ids]
                target_nodes = [node for node in target_nodes if node]
                if not target_nodes:
                    result(action_type, "skipped", "no nodes to resize")
                    continue
                size_mode = str(options.get("size_mode") or "standard").lower()
                media_mode = str(options.get("media_size") or size_mode).lower()
                non_media_mode = str(options.get("non_media_size") or ("reset" if size_mode in {"standard", "reset"} else "keep")).lower()
                touched = []
                for node in target_nodes:
                    rect = _codex_agent_node_rect(node, context_rects)
                    if _codex_agent_apply_size_policy(node, rect, media_mode, non_media_mode):
                        context_rects.pop(str(node.get("id")), None)
                        touched.append(_codex_agent_node_summary(node))
                changed += len(touched)
                result(action_type, "done" if touched else "skipped", f"resized {len(touched)} nodes", touched)
                continue

            if action_type in {"arrange_node", "arrange_nodes", "layout_nodes", "organize_nodes"}:
                ids = _codex_agent_action_target_ids(action, nodes, refs, selected_ids)
                if not ids and str(options.get("scope") or action.get("scope") or "").lower() == "viewport":
                    ids = _codex_agent_viewport_target_ids(nodes, canvas_context, context_rects)
                target_nodes = [node_by_id(node_id) for node_id in ids]
                target_nodes = [node for node in target_nodes if node]
                if not target_nodes:
                    result(action_type, "skipped", "no nodes to arrange")
                    continue
                saved_states = {str(node.get("id")): json.loads(json.dumps(node)) for node in target_nodes}
                size_mode = str(options.get("size_mode") or "keep").lower()
                media_mode = str(options.get("media_size") or size_mode).lower()
                non_media_mode = str(options.get("non_media_size") or ("reset" if size_mode in {"standard", "reset"} else "keep")).lower()
                if media_mode != "keep" or non_media_mode != "keep":
                    for node in target_nodes:
                        _codex_agent_apply_size_policy(node, _codex_agent_node_rect(node, context_rects), media_mode, non_media_mode)
                        context_rects.pop(str(node.get("id")), None)
                old_rects = [_codex_agent_node_rect(saved_states[str(node.get("id"))], context_rects) for node in target_nodes]
                old_bounds = _codex_agent_bounds_from_rects(old_rects) or {"x": 0, "y": 0, "width": 1, "height": 1}
                mode = str(options.get("mode") or options.get("layout") or "horizontal").lower()
                relative, layout_bounds = _codex_agent_layout_positions(target_nodes, context_rects, mode, options, connections)
                placement_requested = any(options.get(key) is not None for key in ("placement_scope", "side", "anchor_node_id", "anchor_ref", "x", "y"))
                origin = {"x": round(old_bounds["x"]), "y": round(old_bounds["y"])}
                if placement_requested:
                    origin = _codex_agent_find_block_origin(nodes, ids, layout_bounds, options, canvas_context, refs, selected_ids, context_rects)
                    if not origin:
                        for node in target_nodes:
                            node_id = str(node.get("id"))
                            node.clear()
                            node.update(saved_states[node_id])
                        result(action_type, "skipped", "no_space: requested destination has no collision-free area")
                        continue
                touched = []
                for node, x, y in relative:
                    node["x"] = round(origin["x"] + x - layout_bounds["x"])
                    node["y"] = round(origin["y"] + y - layout_bounds["y"])
                    touched.append(_codex_agent_node_summary(node))
                changed += len(touched)
                result(action_type, "done", f"arranged {len(touched)} nodes as {mode}", touched)
                continue

            if action_type in {"arrange_node_tree", "arrange_tree", "layout_node_tree"}:
                ids = _codex_agent_action_target_ids(action, nodes, refs, selected_ids)
                if len(ids) != 1:
                    result(action_type, "skipped", "node tree requires exactly one selected or referenced root node")
                    continue
                root_id = ids[0]
                tree_scope = str(options.get("tree_scope") or "both").lower()
                tree = _codex_agent_tree_data(root_id, nodes, connections, tree_scope)
                if len(tree["ids"]) <= 1 or not tree["edges"]:
                    result(action_type, "skipped", "selected node has no connected node tree")
                    continue
                tree_nodes = [node for node in nodes if str(node.get("id")) in tree["ids"]]
                relative, layout_bounds = _codex_agent_tree_layout(tree_nodes, tree["levels"], context_rects, options)
                exact_destination = any(options.get(key) is not None for key in ("x", "y", "anchor_node_id", "anchor_ref"))
                if exact_destination:
                    origin = _codex_agent_find_block_origin(nodes, list(tree["ids"]), layout_bounds, options, canvas_context, refs, selected_ids, context_rects)
                else:
                    origin = _codex_agent_find_nearby_tree_origin(
                        nodes, list(tree["ids"]), relative, layout_bounds, root_id, options, canvas_context, context_rects
                    )
                if not origin:
                    result(action_type, "skipped", "no_space: no nearby collision-free area for node tree")
                    continue
                touched = []
                for node, x, y in relative:
                    node["x"] = round(origin["x"] + x - layout_bounds["x"])
                    node["y"] = round(origin["y"] + y - layout_bounds["y"])
                    touched.append(_codex_agent_node_summary(node))
                changed += len(touched)
                result(action_type, "done", f"arranged node tree with {len(touched)} nodes", touched)
                continue

            if action_type in {"group_node", "group_nodes", "create_group", "create_group_node"}:
                source_items = action.get("items") or action.get("nodes") or action.get("targets") or []
                ids: List[str] = []
                for item in (source_items if isinstance(source_items, list) else [source_items]):
                    for node_id in _codex_agent_resolve_node_ids(item, nodes, refs, selected_ids):
                        node = node_by_id(node_id)
                        if node and node.get("type") != "smart-group" and node_id not in ids:
                            ids.append(node_id)
                if not ids and (options.get("selected") or options.get("scope") == "selected"):
                    ids = [node_id for node_id in selected_ids if node_by_id(node_id)]
                selected = [node_by_id(node_id) for node_id in ids]
                selected = [node for node in selected if node and node.get("type") != "smart-group"]
                if not selected:
                    result(action_type, "skipped", "no nodes to group")
                    continue
                rects = [_codex_agent_node_rect(node, context_rects) for node in selected]
                min_x = min(r["x"] for r in rects)
                min_y = min(r["y"] for r in rects)
                max_x = max(r["x"] + r["width"] for r in rects)
                max_y = max(r["y"] + r["height"] for r in rects)
                group = {
                    "id": _codex_agent_uid("group"),
                    "type": "smart-group",
                    "x": round(min_x - 18),
                    "y": round(min_y - 44),
                    "w": max(340, round(max_x - min_x + 36)),
                    "h": max(220, round(max_y - min_y + 72)),
                    "title": str(options.get("title") or options.get("name") or "智能分组"),
                    "items": ids,
                    "images": [],
                    "created_at": _codex_agent_now(),
                }
                nodes.append(group)
                changed += 1
                result(action_type, "done", f"grouped {len(ids)} nodes", [_codex_agent_node_summary(group)])
                continue

            if action_type in {"ungroup_node", "ungroup_nodes", "split_group", "split_group_node"}:
                source_items = action.get("items") or action.get("nodes") or action.get("targets") or []
                ids: List[str] = []
                for item in (source_items if isinstance(source_items, list) else [source_items]):
                    for node_id in _codex_agent_resolve_node_ids(item, nodes, refs, selected_ids):
                        if node_id not in ids:
                            ids.append(node_id)
                before = len(nodes)
                nodes[:] = [node for node in nodes if not (str(node.get("id")) in ids and node.get("type") == "smart-group")]
                removed = before - len(nodes)
                changed += removed
                result(action_type, "done" if removed else "skipped", f"ungrouped {removed} groups")
                continue

            if action_type in {"create_image_generation_node", "create_image_generation_nodes", "create_video_generation_node", "create_video_generation_nodes", "generate_image", "generate_images", "create_image", "create_images", "generate_video", "generate_videos", "create_video", "create_videos"}:
                source_items = action.get("items") or [action]
                items = source_items if isinstance(source_items, list) else [source_items]
                is_video = action_type.startswith("create_video")
                native = canvas_context.get("native") if isinstance(canvas_context, dict) and isinstance(canvas_context.get("native"), dict) else {}
                defaults = native.get("videoGeneration" if is_video else "imageGeneration") if isinstance(native, dict) else {}
                defaults = defaults if isinstance(defaults, dict) else {}
                created = []
                # References are a self-contained run configuration: retain their
                # media/prompt data on the generated node, but do not draw graph
                # edges. Without references, selection is an upstream workflow
                # input and therefore gets an input edge and right-side placement.
                attached_refs = [ref for ref in refs if isinstance(ref, dict)]
                attached_media = [
                    {
                        "url": str(ref.get("url") or ""),
                        "name": str(ref.get("name") or ""),
                        "kind": str(ref.get("kind") or "image"),
                        "nodeId": str(ref.get("nodeId") or ref.get("node_id") or ""),
                        "imageIndex": ref.get("imageIndex", ref.get("image_index", "")),
                    }
                    for ref in attached_refs if str(ref.get("url") or "").strip()
                ]
                attached_prompts = [
                    {"title": str(ref.get("nodeTitle") or ref.get("name") or "提示词节点"), "text": str(ref.get("text") or "").strip(), "nodeId": str(ref.get("nodeId") or ref.get("node_id") or "")}
                    for ref in attached_refs if str(ref.get("kind") or "").lower() == "prompt" and str(ref.get("text") or "").strip()
                ]
                selected_upstreams = [node_id for node_id in selected_ids if node_by_id(node_id)] if not attached_refs else []
                placement_options = dict(options)
                if len(items) > 1:
                    # One Agent batch is a coherent set of alternatives, not a
                    # canvas-wide arrange request. Keep it in a compact column.
                    placement_options.setdefault("batch_layout", "vertical")
                if selected_upstreams:
                    placement_options.setdefault("scope", "node")
                    placement_options.setdefault("selected", True)
                    placement_options.setdefault("side", "right")
                batch_origin: Optional[Dict[str, int]] = None
                for index, item in enumerate(items):
                    data = {"prompt": item} if isinstance(item, str) else (item if isinstance(item, dict) else {})
                    prompt = str(data.get("prompt") or data.get("text") or "").strip()
                    if not prompt:
                        continue
                    prompt_context = [entry["text"] for entry in attached_prompts if entry.get("text")]
                    if prompt_context and not all(entry in prompt for entry in prompt_context):
                        prompt = "\n\n".join([*prompt_context, prompt])
                    if batch_origin is not None:
                        point = _codex_agent_next_vertical_batch_point(canvas, batch_origin, 360, 240, context_rects)
                    else:
                        point = _codex_agent_new_node_point(canvas, index, len(items), placement_options, 360, 240, canvas_context, refs, selected_ids, context_rects)
                        if len(items) > 1:
                            batch_origin = point
                    requested_provider = str(data.get("provider_id") or data.get("providerId") or data.get("provider") or "").strip()
                    if requested_provider:
                        resolved_provider = _codex_agent_resolve_generation_provider(requested_provider, is_video=is_video)
                        if not resolved_provider:
                            raise HTTPException(status_code=400, detail=f"未找到可用的生成平台「{requested_provider}」")
                        provider_id = str(resolved_provider.get("id") or "")
                        model = _codex_agent_generation_model_from_canvas_default(resolved_provider, data.get("model"), defaults, is_video=is_video)
                    else:
                        provider_id = str(defaults.get("provider_id") or "")
                        model = str(data.get("model") or defaults.get("model") or "")
                    run_settings: Dict[str, Any] = {
                        "engine": "api",
                        "apiKind": "video" if is_video else "image",
                        "provider_id": provider_id,
                        "model": model,
                        "count": max(1, min(8, int(data.get("count") or data.get("n") or defaults.get("count") or 1))),
                    }
                    if is_video:
                        run_settings.update({
                            "videoProvider": provider_id,
                            "videoModel": model,
                            "videoDuration": max(1, min(60, int(data.get("duration") or defaults.get("duration") or 5))),
                            "videoAspect": data.get("aspect_ratio") or data.get("aspect") or defaults.get("aspect_ratio") or "16:9",
                            "videoResolution": data.get("resolution") or defaults.get("resolution") or "",
                            "videoCameraFixed": bool(data.get("camerafixed", data.get("camera_fixed", defaults.get("camerafixed", False)))),
                            "videoGenerateAudio": bool(data.get("generate_audio", defaults.get("generate_audio", False))),
                        })
                    else:
                        image_size, image_ratio, image_resolution = _codex_agent_image_size_from_item(data, defaults)
                        run_settings.update({
                            # Keep the concrete request size and the visual controls
                            # in sync. Previously a 9:16 request only changed
                            # customSize while ratio stayed square, so the node UI
                            # displayed 1:1 and a rerun could regress to square.
                            "ratio": _codex_agent_image_ratio_key(image_ratio),
                            "resolution": image_resolution,
                            "customRatio": image_ratio,
                            "customSize": image_size,
                            "quality": data.get("quality") or defaults.get("quality") or "auto",
                        })
                    node = {
                        "id": _codex_agent_uid("smart"),
                        "type": "smart-image",
                        "x": point["x"],
                        "y": point["y"],
                        "w": 360,
                        "h": 240,
                        "title": str(data.get("title") or ("Video" if is_video else "Image")),
                        "images": [],
                        "scale": 1,
                        "agentGenerated": True,
                        "outputKind": "video" if is_video else "image",
                        "runPrompt": prompt,
                        "runModelPrompt": prompt,
                        "runPromptRefs": [],
                        "runInputRefs": attached_media,
                        "sourcePromptRefs": attached_prompts,
                        "runSettings": run_settings,
                        "runAt": _codex_agent_now(),
                        "created_at": _codex_agent_now(),
                    }
                    nodes.append(node)
                    for source_id in selected_upstreams:
                        if source_id == node["id"]:
                            continue
                        node.setdefault("inputNodeIds", []).append(source_id)
                        if not any(str(conn.get("from") or "") == source_id and str(conn.get("to") or "") == node["id"] and str(conn.get("kind") or "input") == "input" for conn in connections):
                            connections.append({"from": source_id, "to": node["id"], "kind": "input"})
                    created.append(_codex_agent_node_summary(node))
                changed += len(created)
                result(action_type, "done" if created else "skipped", f"created {len(created)} generation nodes", created)
                continue

            result(action_type or "unknown", "skipped", "unsupported action")
        except Exception as exc:
            result(action_type or "unknown", "error", str(exc))

    if changed:
        _codex_agent_record_undo(canvas_id, normalized_actions, undo_snapshot)
        canvas["nodes"] = nodes
        canvas["connections"] = connections
        save_canvas(canvas)
        current_revision = int(CODEX_AGENT_REVISION_STORE.observe(canvas_id, canvas).get("revision") or current_revision)
        _codex_agent_advance_context_snapshot(canvas_context, canvas_id, canvas, current_revision)
        await manager.broadcast_canvas_updated(canvas_id, int(canvas.get("updated_at") or _codex_agent_now()), "codex-agent")
    return {"ok": True, "results": results, "changed": changed, "skipped": skipped, "canvas_updated_at": canvas.get("updated_at", 0), "canvas_revision": current_revision}


def _codex_agent_action_type(action: Dict[str, Any]) -> str:
    return str(action.get("type") or action.get("action") or "").strip().lower().replace("-", "_")


def _codex_agent_action_item_count(action: Dict[str, Any]) -> int:
    for key in ("items", "nodes", "targets", "actions"):
        value = action.get(key)
        if isinstance(value, list):
            return len(value)
    return 1


def _codex_agent_public_action_summary(actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for action in actions[:20]:
        if not isinstance(action, dict):
            continue
        atype = _codex_agent_action_type(action)
        options = action.get("options") if isinstance(action.get("options"), dict) else {}
        out.append({
            "type": atype or "unknown",
            "count": _codex_agent_action_item_count(action),
            "title": str(options.get("title") or action.get("title") or action.get("name") or ""),
            "scope": str(options.get("scope") or action.get("scope") or ""),
        })
    return out


def _codex_agent_action_approval_requirement(actions: List[Dict[str, Any]], canvas_context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    ctx = canvas_context if isinstance(canvas_context, dict) else {}
    agent_input = ctx.get("agentInput") if isinstance(ctx.get("agentInput"), dict) else {}
    policy = str(agent_input.get("approvalPolicy") or agent_input.get("approval_policy") or "auto").strip().lower()
    always_confirm = {"delete_node", "delete_nodes", "remove_node", "remove_nodes", "undo_last_agent_action", "undo_agent_action"}
    if any(_codex_agent_action_type(action) in always_confirm for action in actions if isinstance(action, dict)):
        return {"required": True, "risk": "high", "reason": "删除节点或撤销画布动作必须由用户确认"}
    if policy in {"auto", "automatic", "none", ""}:
        return {"required": False}
    if policy in {"confirm", "always", "manual"}:
        return {"required": True, "risk": "normal", "reason": "当前策略要求执行前确认"}

    risky_types = {
        "generate_video", "generate_videos", "create_video", "create_videos",
        "create_video_generation_node", "create_video_generation_nodes",
        "ungroup_node", "ungroup_nodes", "split_group", "split_group_node",
        "remember_preference", "remember_preferences", "save_preference",
        "delete_node", "delete_nodes", "remove_node", "remove_nodes", "undo_last_agent_action", "undo_agent_action",
        "arrange_node_tree", "arrange_tree", "layout_node_tree",
    }
    moderate_types = {
        "rename_nodes", "rename_node", "move_nodes", "move_node", "position_nodes", "position_node",
        "arrange_nodes", "arrange_node", "layout_nodes", "organize_nodes",
        "resize_nodes", "resize_node",
        "group_nodes", "group_node", "create_group", "create_group_node",
        "generate_image", "generate_images", "create_image", "create_images",
        "create_image_generation_node", "create_image_generation_nodes",
    }
    reasons: List[str] = []
    risk = "normal"
    for action in actions:
        if not isinstance(action, dict):
            continue
        atype = _codex_agent_action_type(action)
        count = _codex_agent_action_item_count(action)
        options = action.get("options") if isinstance(action.get("options"), dict) else {}
        scope = str(options.get("scope") or action.get("scope") or "").lower()
        if atype in risky_types:
            risk = "high"
            reasons.append(f"{atype} 属于高风险/高成本动作")
        elif atype in moderate_types and (count >= 3 or scope in {"all", "canvas", "global"}):
            risk = "high"
            reasons.append(f"{atype} 将影响 {count} 项或全画布范围")
        elif atype in moderate_types and risk != "high":
            risk = "medium"
    if policy in {"confirm-risky", "risky", "high-risk"} and risk == "high":
        return {"required": True, "risk": risk, "reason": "；".join(reasons[:3]) or "检测到高风险画布动作"}
    return {"required": False}


async def _codex_agent_execute_actions_from_text(task_id: str, text: str, refs: List[Dict[str, Any]], canvas_context: Optional[Dict[str, Any]]) -> None:
    with _codex_agent_task_lock:
        task = _codex_agent_tasks.get(task_id) or {}
        canvas_id = task.get("canvas_id", "")
        seen = task.setdefault("executed_action_keys", set())
    blocks = _codex_agent_extract_canvas_action_blocks(text)
    if not blocks:
        return
    for raw in blocks:
        with _codex_agent_task_lock:
            task = _codex_agent_tasks.get(task_id) or {}
            seen = task.setdefault("executed_action_keys", set())
            if raw in seen:
                continue
            seen.add(raw)
        try:
            payload = json.loads(raw)
            actions = payload if isinstance(payload, list) else (payload.get("actions") if isinstance(payload, dict) and isinstance(payload.get("actions"), list) else [payload])
            actions = [action for action in actions if isinstance(action, dict)]
        except Exception as exc:
            _codex_agent_add_task_event(task_id, {"method": "canvas/action_result", "params": {"status": "error", "message": f"画布动作 JSON 解析失败：{exc}"}})
            continue
        approval = _codex_agent_action_approval_requirement(actions, canvas_context)
        if approval.get("required"):
            approval_id = _codex_agent_uid("approval")
            with _codex_agent_task_lock:
                task = _codex_agent_tasks.get(task_id)
                if task is not None:
                    task.setdefault("pending_action_approvals", {})[approval_id] = {
                        "approval_id": approval_id,
                        "actions": actions,
                        "refs": refs,
                        "canvas_context": canvas_context,
                        "created_at": _codex_agent_now(),
                        "status": "pending",
                        "reason": approval.get("reason", ""),
                        "risk": approval.get("risk", "normal"),
                    }
            _codex_agent_add_task_event(task_id, {
                "method": "canvas/action_pending",
                "params": {
                    "task_id": task_id,
                    "approval_id": approval_id,
                    "actions": _codex_agent_public_action_summary(actions),
                    "reason": approval.get("reason", "需要确认后执行"),
                    "risk": approval.get("risk", "normal"),
                    "count": len(actions),
                },
            })
            continue
        result = await _codex_agent_apply_canvas_actions(canvas_id, actions, refs, canvas_context)
        _codex_agent_add_task_event(task_id, {"method": "canvas/action_result", "params": result})


def _codex_agent_is_batch_tool_refusal(text: str) -> bool:
    """Detect a false claim that the registered batch queue tool is absent."""
    raw = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if not raw:
        return False
    mentions_batch = any(marker in raw for marker in ("批量任务", "create_batch_task"))
    claims_missing = any(marker in raw for marker in (
        "没有提供", "未提供", "缺少", "不可用", "不能调用", "无法调用", "尚未部署",
    ))
    suggests_reload = any(marker in raw for marker in ("刷新", "重新打开", "重开画布"))
    return mentions_batch and (claims_missing or suggests_reload)


def _codex_agent_canvas_tool_is_query(tool: str) -> bool:
    spec = _codex_agent_canvas_tool_registry().get(str(tool or "").strip().lower().replace("-", "_"))
    return bool(spec and spec.get("query"))


def _codex_agent_canvas_tool_result_text(results: List[Dict[str, Any]]) -> str:
    compact: List[Dict[str, Any]] = []
    for item in results[:12]:
        if not isinstance(item, dict):
            continue
        data = item.get("result") if isinstance(item.get("result"), dict) else {}
        clean = {
            "tool": item.get("tool") or data.get("tool") or "",
            "ok": data.get("ok", item.get("ok", True)),
        }
        for key in ("canvas_revision", "node_count", "connection_count", "selected_count", "type_counts", "bounds", "visible_world", "image_generation", "video_generation", "changed", "skipped"):
            if key in data:
                clean[key] = data.get(key)
        if isinstance(data.get("nodes"), list):
            clean["nodes"] = data.get("nodes")[:40]
        if isinstance(data.get("connections"), list):
            clean["connections"] = data.get("connections")[:80]
        if isinstance(data.get("results"), list):
            clean["results"] = data.get("results")[:20]
        if data.get("message"):
            clean["message"] = data.get("message")
        compact.append(clean)
    return (
        "<canvas_tool_result>\n"
        + json.dumps(compact, ensure_ascii=False)
        + "\n</canvas_tool_result>\n"
        + "请基于这些工具结果继续回答用户。不要暴露 canvas_agent_tool/canvas_agent_action/internal id；如涉及节点，用名称、图序、位置或简短描述表达。"
    )


async def _codex_agent_execute_tools_from_text(
    task_id: str,
    text: str,
    refs: List[Dict[str, Any]],
    canvas_context: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    with _codex_agent_task_lock:
        task = _codex_agent_tasks.get(task_id) or {}
        canvas_id = str(task.get("canvas_id") or "")
    blocks = _codex_agent_extract_canvas_tool_blocks(text)
    if not blocks:
        return []
    executed: List[Dict[str, Any]] = []
    for raw in blocks:
        with _codex_agent_task_lock:
            task = _codex_agent_tasks.get(task_id) or {}
            seen = task.setdefault("executed_tool_keys", set())
            if raw in seen:
                continue
            seen.add(raw)
        try:
            calls = _codex_agent_parse_canvas_tool_calls(raw)
        except Exception as exc:
            result = {"ok": False, "tool": "parse", "message": str(exc)}
            executed.append({"tool": "parse", "query": False, "result": result})
            _codex_agent_add_task_event(task_id, {"method": "canvas/tool_result", "params": result})
            continue
        for call in calls:
            tool = str(call.get("tool") or "").strip().lower().replace("-", "_")
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            query = _codex_agent_canvas_tool_is_query(tool)
            _codex_agent_add_task_event(task_id, {
                "method": "canvas/tool_call",
                "params": {"tool": tool, "args": args, "query": query},
            })
            try:
                payload = CodexAgentCanvasToolRequest(
                    canvas_id=canvas_id,
                    tool=tool,
                    args=args,
                    refs=refs,
                    canvas_context=canvas_context,
                )
                result = await _codex_agent_run_canvas_tool(payload)
            except HTTPException as exc:
                result = {"ok": False, "tool": tool, "message": str(exc.detail)}
            except Exception as exc:
                result = {"ok": False, "tool": tool, "message": str(exc)}
            executed.append({"tool": tool, "query": query, "result": result})
            _codex_agent_add_task_event(task_id, {
                "method": "canvas/tool_result",
                "params": {"tool": tool, "query": query, "result": result},
            })
    return executed


async def _codex_agent_add_generated_image(task_id: str, path: str, prompt: str, refs: List[Dict[str, Any]], canvas_context: Optional[Dict[str, Any]]) -> None:
    if not path:
        return
    action = {"type": "add_media", "items": [{"path": path, "name": _codex_agent_name_from_url(path, "codex-image.png"), "kind": "image", "prompt": prompt}], "options": {"cols": 1}}
    with _codex_agent_task_lock:
        task = _codex_agent_tasks.get(task_id) or {}
        if task.get("native_generation_submitted"):
            # The turn already submitted a provider task through the Canvas Tool.
            # Ignore an accidental App Server imageGeneration side effect instead
            # of adding a second image/node to the same user request.
            return
        key = f"generated-image:{path}"
        seen = task.setdefault("executed_action_keys", set())
        if key in seen:
            return
        seen.add(key)
    result = await _codex_agent_apply_canvas_actions(str(task.get("canvas_id") or ""), [action], refs, canvas_context)
    _codex_agent_add_task_event(task_id, {"method": "canvas/action_result", "params": result})


def _codex_agent_canvas_nodes(canvas_id: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    canvas = load_canvas(canvas_id)
    if not canvas:
        raise HTTPException(status_code=404, detail="Canvas not found")
    nodes = canvas.get("nodes") if isinstance(canvas.get("nodes"), list) else []
    connections = canvas.get("connections") if isinstance(canvas.get("connections"), list) else []
    return canvas, [node for node in nodes if isinstance(node, dict)], [conn for conn in connections if isinstance(conn, dict)]


def _codex_agent_context_snapshot_data(canvas_context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    ctx = canvas_context if isinstance(canvas_context, dict) else {}
    meta = ctx.get("_contextSnapshot") if isinstance(ctx.get("_contextSnapshot"), dict) else {}
    path_text = str(meta.get("snapshot_path") or "").strip()
    if not path_text:
        return {}
    try:
        root = CODEX_AGENT_CONTEXT_SNAPSHOT_DIR.resolve()
        path = _Path(path_text).resolve()
        if root not in path.parents or path.suffix != ".json" or not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _codex_agent_query_snapshot_canvas(payload: CodexAgentCanvasToolRequest) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]], str]:
    snapshot = _codex_agent_context_snapshot_data(payload.canvas_context)
    stored = snapshot.get("canvas_snapshot") if isinstance(snapshot.get("canvas_snapshot"), dict) else {}
    if stored:
        nodes = stored.get("nodes") if isinstance(stored.get("nodes"), list) else []
        connections = stored.get("connections") if isinstance(stored.get("connections"), list) else []
        canvas = {"name": stored.get("title") or "", "nodes": nodes, "connections": connections}
        return canvas, [item for item in nodes if isinstance(item, dict)], [item for item in connections if isinstance(item, dict)], str(snapshot.get("snapshot_id") or "")
    canvas, nodes, connections = _codex_agent_canvas_nodes(str(payload.canvas_id or ""))
    return canvas, nodes, connections, ""


def _codex_agent_connection_value(conn: Dict[str, Any], keys: Tuple[str, ...]) -> str:
    for key in keys:
        value = conn.get(key)
        if value:
            return str(value)
    return ""


def _codex_agent_tool_query_canvas(payload: CodexAgentCanvasToolRequest) -> Dict[str, Any]:
    tool = str(payload.tool or "").strip().lower()
    args = payload.args if isinstance(payload.args, dict) else {}
    canvas_id = str(payload.canvas_id or "").strip()
    if not canvas_id:
        raise HTTPException(status_code=400, detail="缺少 canvas_id")
    canvas, nodes, connections, snapshot_id = _codex_agent_query_snapshot_canvas(payload)
    context_rects = _codex_agent_context_node_rects(payload.canvas_context)
    selected_ids = _codex_agent_selected_ids(payload.canvas_context)
    by_id = {str(node.get("id")): node for node in nodes if node.get("id")}

    if tool == "get_generation_settings":
        ctx = payload.canvas_context if isinstance(payload.canvas_context, dict) else {}
        native = ctx.get("native") if isinstance(ctx.get("native"), dict) else {}
        requested_kind = str(args.get("kind") or "all").strip().lower()

        def compact_settings(raw: Any, provider_models_key: str) -> Dict[str, Any]:
            source = raw if isinstance(raw, dict) else {}
            result = {
                key: source.get(key)
                for key in ("provider_id", "model", "size", "quality", "count", "duration", "aspect_ratio", "resolution", "camerafixed", "generate_audio")
                if source.get(key) not in (None, "")
            }
            source_providers = source.get("providers") if isinstance(source.get("providers"), list) else []
            try:
                configured_providers = [
                    provider for provider in load_api_providers()
                    if isinstance(provider, dict) and provider.get("enabled", True)
                ]
            except Exception:
                configured_providers = []
            # Provider availability belongs to the server configuration. The
            # frontend snapshot contributes the currently selected defaults,
            # but an omitted/stale provider dump must never become "no provider".
            provider_sources = configured_providers or source_providers
            providers = []
            for provider in provider_sources:
                if not isinstance(provider, dict):
                    continue
                models = provider.get(provider_models_key) if isinstance(provider.get(provider_models_key), list) else []
                if not models:
                    continue
                providers.append({
                    "provider_id": provider.get("id") or "",
                    "name": provider.get("name") or "",
                    "protocol": provider.get("protocol") or "",
                    "models": [str(model) for model in models[:40]],
                })
            result["providers"] = providers[:30]
            return result

        result = {
            "ok": True,
            "tool": tool,
            "canvas_id": canvas_id,
            "snapshot_id": snapshot_id,
        }
        if requested_kind in {"all", "image"}:
            result["image_generation"] = compact_settings(native.get("imageGeneration") or native.get("image_generation"), "image_models")
        if requested_kind in {"all", "video"}:
            result["video_generation"] = compact_settings(native.get("videoGeneration") or native.get("video_generation"), "video_models")
        return result

    if tool == "get_canvas_summary":
        type_counts: Dict[str, int] = {}
        rects = []
        for node in nodes:
            ntype = str(node.get("type") or "smart-image")
            type_counts[ntype] = type_counts.get(ntype, 0) + 1
            rects.append(_codex_agent_node_rect(node, context_rects))
        bounds = {}
        if rects:
            min_x = min(rect["x"] for rect in rects)
            min_y = min(rect["y"] for rect in rects)
            max_x = max(rect["x"] + rect["width"] for rect in rects)
            max_y = max(rect["y"] + rect["height"] for rect in rects)
            bounds = {"x": round(min_x), "y": round(min_y), "width": round(max_x - min_x), "height": round(max_y - min_y)}
        return {
            "ok": True,
            "tool": tool,
            "canvas_id": canvas_id,
            "snapshot_id": snapshot_id,
            "title": canvas.get("name") or canvas.get("title") or "",
            "node_count": len(nodes),
            "connection_count": len(connections),
            "selected_count": len(selected_ids),
            "type_counts": type_counts,
            "bounds": bounds,
        }

    if tool == "get_selected_nodes":
        return {
            "ok": True,
            "tool": tool,
            "canvas_id": canvas_id,
            "snapshot_id": snapshot_id,
            "nodes": [_codex_agent_node_summary(by_id[node_id]) for node_id in selected_ids if node_id in by_id],
        }

    if tool == "get_viewport_nodes":
        visible = _codex_agent_visible_world(payload.canvas_context)
        if not visible:
            return {"ok": True, "tool": tool, "canvas_id": canvas_id, "snapshot_id": snapshot_id, "nodes": [], "visible_world": None}
        pad = float(args.get("pad") or 80)
        result_nodes = []
        for node in nodes:
            rect = _codex_agent_node_rect(node, context_rects)
            if not (
                rect["x"] + rect["width"] + pad < visible["x"] or
                visible["x"] + visible["width"] + pad < rect["x"] or
                rect["y"] + rect["height"] + pad < visible["y"] or
                visible["y"] + visible["height"] + pad < rect["y"]
            ):
                result_nodes.append(_codex_agent_node_summary(node))
        limit = max(1, min(120, int(args.get("limit") or 120)))
        return {"ok": True, "tool": tool, "canvas_id": canvas_id, "snapshot_id": snapshot_id, "visible_world": visible, "nodes": result_nodes[:limit]}

    if tool == "search_canvas_nodes":
        query = str(args.get("query") or "").strip().lower()
        requested_types = {str(item).strip().lower() for item in (args.get("types") or []) if str(item).strip()} if isinstance(args.get("types"), list) else set()
        limit = max(1, min(80, int(args.get("limit") or 30)))
        cursor = max(0, int(args.get("cursor") or 0))
        matches: List[Dict[str, Any]] = []
        for node in nodes:
            node_type = str(node.get("type") or "").lower()
            if requested_types and node_type not in requested_types:
                continue
            image_text = " ".join(str(image.get("name") or image.get("kind") or "") for image in (node.get("images") or []) if isinstance(image, dict))
            haystack = " ".join([node_type, str(node.get("title") or ""), str(node.get("text") or ""), image_text]).lower()
            if not query or query in haystack:
                matches.append(_codex_agent_node_summary(node))
        page = matches[cursor:cursor + limit]
        next_cursor: Optional[int] = cursor + len(page) if cursor + len(page) < len(matches) else None
        return {"ok": True, "tool": tool, "canvas_id": canvas_id, "snapshot_id": snapshot_id, "query": query, "nodes": page, "total": len(matches), "cursor": cursor, "next_cursor": next_cursor}

    if tool == "get_node_detail":
        ids = _codex_agent_resolve_node_ids(args, nodes, payload.refs or [], selected_ids)
        if not ids:
            return {
                "ok": False,
                "tool": tool,
                "canvas_id": canvas_id,
                "snapshot_id": snapshot_id,
                "message": "缺少节点目标。请传 node_id/ref，或在画布中只选中一个节点",
                "nodes": [],
            }
        return {
            "ok": True,
            "tool": tool,
            "canvas_id": canvas_id,
            "snapshot_id": snapshot_id,
            "nodes": [_codex_agent_node_summary(by_id[node_id]) for node_id in ids if node_id in by_id],
        }

    if tool == "get_layout_context":
        ids = _codex_agent_action_target_ids(args, nodes, payload.refs or [], selected_ids)
        if not ids and str(args.get("scope") or "").lower() == "viewport":
            ids = _codex_agent_viewport_target_ids(nodes, payload.canvas_context, context_rects)
        target_nodes = [by_id[node_id] for node_id in ids if node_id in by_id]
        if not target_nodes:
            return {"ok": False, "tool": tool, "canvas_id": canvas_id, "snapshot_id": snapshot_id, "message": "没有可分析的选中或引用节点", "nodes": []}
        target_rects = [_codex_agent_node_rect(node, context_rects) for node in target_nodes]
        bounds = _codex_agent_bounds_from_rects(target_rects) or {"x": 0, "y": 0, "width": 1, "height": 1}
        target_set = set(ids)
        obstacles = [(node, _codex_agent_node_rect(node, context_rects)) for node in nodes if str(node.get("id")) not in target_set]
        expanded = {"x": bounds["x"] - 360, "y": bounds["y"] - 360, "width": bounds["width"] + 720, "height": bounds["height"] + 720}
        nearby = [node for node, rect in obstacles if _codex_agent_rect_intersects(rect, expanded, 0)][:80]
        gap_x, gap_y = 80.0, 54.0
        side_points = {
            "left": (bounds["x"] - bounds["width"] - gap_x, bounds["y"]),
            "right": (bounds["x"] + bounds["width"] + gap_x, bounds["y"]),
            "top": (bounds["x"], bounds["y"] - bounds["height"] - gap_y),
            "bottom": (bounds["x"], bounds["y"] + bounds["height"] + gap_y),
        }
        open_sides: Dict[str, Dict[str, Any]] = {}
        for side, (x, y) in side_points.items():
            probe = {"x": x, "y": y, "width": bounds["width"], "height": bounds["height"]}
            collisions = [str(node.get("id")) for node, rect in obstacles if _codex_agent_rect_intersects(probe, rect, 24)]
            open_sides[side] = {"available": not collisions, "collision_count": len(collisions), "x": round(x), "y": round(y)}
        media_rows = []
        long_edges = []
        for node, rect in zip(target_nodes, target_rects):
            images = [item for item in (node.get("images") or []) if isinstance(item, dict)]
            ratio = _codex_agent_media_ratio(node, rect) if str(node.get("type") or "smart-image") == "smart-image" and len(images) == 1 else None
            orientation = "landscape" if ratio and ratio > 1.001 else "portrait" if ratio and ratio < 0.999 else "square" if ratio else "multi_or_non_media"
            if ratio:
                long_edges.append(max(rect["width"], rect["height"]))
            media_rows.append({"id": node.get("id"), "type": node.get("type") or "smart-image", "x": round(rect["x"]), "y": round(rect["y"]), "width": round(rect["width"]), "height": round(rect["height"]), "orientation": orientation, "image_count": len(images)})
        variation = 0.0
        if len(long_edges) > 1 and max(long_edges) > 0:
            variation = (max(long_edges) - min(long_edges)) / max(long_edges)
        selected_edges = _codex_agent_graph_rows(connections, set(ids))
        return {
            "ok": True,
            "tool": tool,
            "canvas_id": canvas_id,
            "snapshot_id": snapshot_id,
            "nodes": media_rows,
            "selection_bounds": {key: round(value) for key, value in bounds.items()},
            "visible_world": _codex_agent_visible_world(payload.canvas_context),
            "canvas_bounds": _codex_agent_bounds_from_rects([_codex_agent_node_rect(node, context_rects) for node in nodes]),
            "nearby_nodes": [_codex_agent_node_summary(node) for node in nearby],
            "open_sides": open_sides,
            "media_long_edge_variation": round(variation, 4),
            "suggest_uniform_media_size": variation > 0.15,
            "connections": [{"source": source, "target": target} for source, target in selected_edges],
        }

    if tool == "get_node_tree":
        ids = _codex_agent_action_target_ids(args, nodes, payload.refs or [], selected_ids)
        if len(ids) != 1:
            return {"ok": False, "tool": tool, "canvas_id": canvas_id, "snapshot_id": snapshot_id, "message": "节点树查询需要且只能指定一个根节点", "nodes": [], "connections": []}
        scope = str((args.get("options") or {}).get("tree_scope") if isinstance(args.get("options"), dict) else args.get("tree_scope") or "both").lower()
        tree = _codex_agent_tree_data(ids[0], nodes, connections, scope)
        tree_nodes = []
        for node_id in sorted(tree["ids"], key=lambda item: (tree["levels"].get(item, 0), float(by_id.get(item, {}).get("y") or 0), float(by_id.get(item, {}).get("x") or 0))):
            if node_id not in by_id:
                continue
            summary = _codex_agent_node_summary(by_id[node_id])
            summary["level"] = int(tree["levels"].get(node_id, 0))
            tree_nodes.append(summary)
        return {
            "ok": True,
            "tool": tool,
            "canvas_id": canvas_id,
            "snapshot_id": snapshot_id,
            "root_node_id": ids[0],
            "scope": scope,
            "nodes": tree_nodes,
            "connections": [{"source": source, "target": target} for source, target in tree["edges"]],
        }

    if tool == "get_connected_nodes":
        ids = _codex_agent_resolve_node_ids(args, nodes, payload.refs or [], selected_ids)
        if not ids:
            return {
                "ok": False,
                "tool": tool,
                "canvas_id": canvas_id,
                "snapshot_id": snapshot_id,
                "message": "缺少节点目标。请传 node_id/ref，或在画布中只选中一个节点",
                "nodes": [],
                "connections": [],
            }
        if len(ids) > 1:
            return {
                "ok": False,
                "tool": tool,
                "canvas_id": canvas_id,
                "snapshot_id": snapshot_id,
                "message": "get_connected_nodes 一次只能查询一个节点；请传一个 node_id/ref",
                "nodes": [],
                "connections": [],
            }
        node_id = ids[0]
        connected_ids: List[str] = []
        edge_rows = []
        for conn in connections:
            source = _codex_agent_connection_value(conn, ("source", "sourceId", "from", "fromNodeId", "start"))
            target = _codex_agent_connection_value(conn, ("target", "targetId", "to", "toNodeId", "end"))
            if node_id in {source, target}:
                other = target if source == node_id else source
                if other and other not in connected_ids:
                    connected_ids.append(other)
                edge_rows.append({"source": source, "target": target})
        return {
            "ok": True,
            "tool": tool,
            "canvas_id": canvas_id,
            "snapshot_id": snapshot_id,
            "node_id": node_id,
            "connections": edge_rows,
            "nodes": [_codex_agent_node_summary(by_id[item]) for item in connected_ids if item in by_id],
        }

    raise HTTPException(status_code=400, detail=f"不支持的查询工具: {tool}")


async def _codex_agent_run_canvas_tool(payload: CodexAgentCanvasToolRequest) -> Dict[str, Any]:
    tool = str(payload.tool or "").strip().lower().replace("-", "_")
    spec = _codex_agent_canvas_tool_registry().get(tool)
    if not spec:
        raise HTTPException(status_code=400, detail=f"不支持的 Canvas Tool: {tool}")
    args = payload.args if isinstance(payload.args, dict) else {}
    if tool in {"create_batch_task", "get_batch_task", "control_batch_task"}:
        engine = _complex_task_engine_required()
        try:
            if tool == "create_batch_task":
                raw_spec = dict(args.get("spec") or {}) if isinstance(args.get("spec"), dict) else {}
                batch_spec = normalize_batch_task_spec(raw_spec, canvas_id=str(payload.canvas_id or ""))
                task = engine.create(batch_spec, canvas_id=str(payload.canvas_id or ""))
                task_node_id = str((task.get("summary") or {}).get("task_node_id") or "")
                canvas = load_canvas(str(payload.canvas_id or ""))
                task_node = next((node for node in canvas.get("nodes") or [] if str(node.get("id") or "") == task_node_id), None)
                node_summary = _codex_agent_node_summary(task_node) if isinstance(task_node, dict) else None
                return {
                    "ok": True, "tool": tool, "task": task, "changed": 1,
                    "nodes": [node_summary] if node_summary else [],
                    "results": [{"type": "create_batch_task", "items": [node_summary] if node_summary else []}],
                    "message": f"已创建并启动批量任务《{task.get('title') or '批量任务'}》",
                }
            task_id = str(args.get("task_id") or "")
            if tool == "get_batch_task":
                return {"ok": True, "tool": tool, "task": engine.get(task_id)}
            if tool == "control_batch_task":
                return {"ok": True, "tool": tool, "task": engine.control(task_id, str(args.get("action") or ""))}
        except ComplexTaskError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    if tool in {"get_generation_queue", "get_generation_task", "cancel_generation_task", "retry_generation_task"}:
        return await _codex_agent_generation_control(tool, args, str(payload.canvas_id or ""))
    if spec.get("query"):
        result = _codex_agent_tool_query_canvas(payload)
        snapshot = _codex_agent_context_snapshot_data(payload.canvas_context)
        if snapshot.get("canvas_revision") is not None:
            result.setdefault("canvas_revision", int(snapshot.get("canvas_revision") or 0))
        else:
            try:
                live_canvas = load_canvas(str(payload.canvas_id or ""))
                result.setdefault("canvas_revision", int(CODEX_AGENT_REVISION_STORE.observe(str(payload.canvas_id or ""), live_canvas).get("revision") or 0))
            except Exception:
                pass
        return result
    actions = args.get("actions")
    if not isinstance(actions, list):
        action = dict(args)
        action["type"] = str(spec.get("action") or tool)
        if tool == "remember_preference" and not action.get("note"):
            action["note"] = action.get("text") or action.get("content") or ""
        actions = [action]
    refs = [item for item in (payload.refs or []) if isinstance(item, dict)]
    return await _codex_agent_apply_canvas_actions(str(payload.canvas_id or ""), actions, refs, payload.canvas_context)


def _codex_agent_native_tool_actions(tool: str, args: Dict[str, Any]) -> List[Dict[str, Any]]:
    spec = _codex_agent_canvas_tool_registry().get(tool) or {}
    action_type = str(spec.get("action") or "")
    if not action_type:
        return []
    action = dict(args)
    action.pop("actions", None)  # Native tools never accept arbitrary legacy action batches.
    action["type"] = action_type
    options = dict(action.get("options") or {}) if isinstance(action.get("options"), dict) else {}
    for key in (
        "scope", "all", "selected", "cols", "mode", "layout", "cellX", "cellY", "gapX", "gapY",
        "x", "y", "side", "placement_scope", "anchor_node_id", "anchor_ref", "size_mode",
        "media_size", "non_media_size", "preserve_aspect", "tree_scope", "title", "name",
    ):
        if key in action and key not in options:
            options[key] = action[key]
    if options:
        action["options"] = options
    if tool == "remember_preference" and not action.get("note"):
        action["note"] = action.get("text") or action.get("content") or ""
    return [action]


def _codex_agent_resolve_generation_provider(value: Any, is_video: bool = False) -> Optional[Dict[str, Any]]:
    """Resolve user-facing provider names without silently falling back.

    `get_api_provider()` intentionally keeps old UI calls working by falling back
    to the primary provider. That is unsafe for Agent generation: asking for
    Gemini must never become a RunningHub request merely because `gemini` is an
    alias rather than the saved provider id `gemini-cli`.
    """
    text = str(value or "").strip().lower()
    providers = [item for item in load_api_providers() if item.get("enabled", True)]
    if not text:
        return None
    exact = next((item for item in providers if text in {str(item.get("id") or "").lower(), str(item.get("name") or "").lower()}), None)
    if exact:
        return exact
    aliases = {
        "gemini": ("gemini-cli", "gemini"),
        "gemini cli": ("gemini-cli",),
        "antigravity": ("gemini-cli",),
        "antigravity cli": ("gemini-cli",),
        "agy": ("gemini-cli",),
        "gpt": ("codex",),
        "openai": ("codex",),
        "codex": ("codex",),
        "gpt cli": ("codex",),
        "jimeng": ("jimeng",),
        "即梦": ("jimeng",),
        "runninghub": ("runninghub",),
        "rh": ("runninghub",),
    }
    wanted = aliases.get(text, ())
    for provider_id in wanted:
        item = next((candidate for candidate in providers if str(candidate.get("id") or "").lower() == provider_id), None)
        if item:
            return item
    # Allow a useful partial match for configured custom providers, but only
    # when it uniquely identifies one. Ambiguity remains an explicit error.
    matches = [item for item in providers if text in f"{item.get('id') or ''} {item.get('name') or ''} {item.get('protocol') or ''}".lower()]
    if len(matches) == 1:
        return matches[0]
    return None


def _codex_agent_generation_model_for_provider(provider: Dict[str, Any], requested_model: Any, is_video: bool = False) -> str:
    requested = str(requested_model or "").strip()
    protocol = provider_protocol(provider)
    generic_names = {"gemini", "gemini cli", "antigravity", "antigravity cli", "agy", "gpt", "openai", "codex", "gpt cli"}
    if requested.lower() in generic_names:
        requested = ""
    if requested:
        return requested
    models = provider.get("video_models" if is_video else "image_models") or []
    if models:
        return str(models[0] or "")
    if protocol == "gemini-cli":
        return GEMINI_CLI_DEFAULT_IMAGE_MODELS[0] if not is_video else ""
    if protocol == "codex":
        return CODEX_DEFAULT_IMAGE_MODELS[0] if not is_video else ""
    return ""


def _codex_agent_generation_model_from_canvas_default(
    provider: Dict[str, Any], requested_model: Any, defaults: Dict[str, Any], is_video: bool = False,
) -> str:
    """优先保留画布中已选择的默认模型，Agent 不替用户改选模型。"""
    requested = str(requested_model or "").strip()
    generic_names = {"gemini", "gemini cli", "antigravity", "antigravity cli", "agy", "gpt", "openai", "codex", "gpt cli"}
    if requested.lower() in generic_names:
        requested = ""
    if requested:
        return requested
    configured_provider = str(defaults.get("provider_id") or "").strip()
    configured_model = str(defaults.get("model") or "").strip()
    if configured_model and configured_provider == str(provider.get("id") or ""):
        return configured_model
    return _codex_agent_generation_model_for_provider(provider, "", is_video=is_video)


def _codex_agent_image_ratio(value: Any) -> str:
    text = str(value or "").strip().lower().replace("：", ":").replace(" ", "")
    aliases = {
        "square": "1:1", "正方形": "1:1", "wide": "16:9", "横版": "16:9", "横屏": "16:9",
        "story": "9:16", "竖版": "9:16", "竖屏": "9:16", "portrait": "2:3",
        "landscape": "3:2", "portrait43": "3:4", "landscape43": "4:3",
        "ultrawide": "21:9", "ultratall": "9:21",
    }
    text = aliases.get(text, text)
    return text if text in CHAT_RATIO_SIZE_OPTIONS else ""


def _codex_agent_image_ratio_key(ratio: str) -> str:
    return {
        "1:1": "square", "2:3": "portrait", "3:2": "landscape", "3:4": "portrait43",
        "4:3": "landscape43", "9:16": "story", "16:9": "wide", "21:9": "ultrawide", "9:21": "ultratall",
    }.get(str(ratio or ""), "square")


def _codex_agent_image_resolution_key(data: Dict[str, Any], defaults: Dict[str, Any], ratio: str) -> str:
    hint = " ".join(str(value or "") for value in (
        data.get("resolution"), data.get("quality"), data.get("size"), defaults.get("size"), defaults.get("quality"),
    )).lower()
    if "4k" in hint or "3840" in hint or "4096" in hint:
        return "4k"
    if "2k" in hint or "2048" in hint:
        return "2k"
    # A current default size should keep its approximate quality tier when only
    # the requested aspect changes (e.g. default 1536 square -> 9:16 2K).
    width, height = parse_size_pair(defaults.get("size") or "")
    if max(width, height) >= 1900:
        return "2k"
    return "1k"


def _codex_agent_image_size_from_item(data: Dict[str, Any], defaults: Dict[str, Any]) -> Tuple[str, str, str]:
    """Return concrete size plus UI ratio/resolution for an Agent image node."""
    raw_size = str(data.get("size") or "").strip()
    width, height = parse_size_pair(raw_size)
    ratio = _codex_agent_image_ratio(data.get("aspect_ratio") or data.get("aspect") or data.get("ratio"))
    if not ratio and raw_size:
        ratio = _codex_agent_image_ratio(raw_size)
    if width and height:
        if not ratio:
            divisor = math.gcd(width, height) or 1
            ratio = _codex_agent_image_ratio(f"{width // divisor}:{height // divisor}")
        resolution = _codex_agent_image_resolution_key(data, defaults, ratio)
        return raw_size, ratio or "1:1", resolution
    if ratio:
        resolution = _codex_agent_image_resolution_key(data, defaults, ratio)
        options = CANVAS_AGENT_IMAGE_SIZE_OPTIONS.get(ratio) or CANVAS_AGENT_IMAGE_SIZE_OPTIONS["1:1"]
        index = {"1k": 0, "2k": 1, "4k": 2}.get(resolution, 0)
        return options[min(index, len(options) - 1)], ratio, resolution
    fallback = str(defaults.get("size") or "1024x1024").strip()
    width, height = parse_size_pair(fallback)
    if width and height:
        divisor = math.gcd(width, height) or 1
        inferred = _codex_agent_image_ratio(f"{width // divisor}:{height // divisor}") or "1:1"
        return fallback, inferred, _codex_agent_image_resolution_key(data, defaults, inferred)
    return "1024x1024", "1:1", "1k"


def _codex_agent_prepare_batch_spec(raw: Dict[str, Any], canvas_context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve provider/model defaults while keeping the public batch shape flat."""
    spec = dict(raw or {})
    native = canvas_context.get("native") if isinstance(canvas_context, dict) and isinstance(canvas_context.get("native"), dict) else {}
    image_defaults = native.get("imageGeneration") if isinstance(native.get("imageGeneration"), dict) else {}
    video_defaults = native.get("videoGeneration") if isinstance(native.get("videoGeneration"), dict) else {}
    prepared: List[Dict[str, Any]] = []
    for source in spec.get("items") or []:
        if not isinstance(source, dict):
            prepared.append(source)
            continue
        item = dict(source)
        kind = str(item.get("kind") or "image").strip().lower()
        is_video = kind == "video"
        requested_provider = str(item.get("provider_id") or "").strip()
        provider = _codex_agent_resolve_generation_provider(requested_provider, is_video=is_video)
        if not provider:
            raise ComplexTaskError(f"未找到可用的生成平台「{requested_provider or '空'}」")
        item["provider_id"] = str(provider.get("id") or "")
        defaults = video_defaults if is_video else image_defaults
        item["model"] = _codex_agent_generation_model_from_canvas_default(provider, item.get("model"), defaults, is_video=is_video)
        if is_video:
            item.setdefault("duration", defaults.get("duration") or 5)
            item.setdefault("aspect_ratio", defaults.get("aspect_ratio") or "16:9")
            if defaults.get("resolution") and not item.get("resolution"):
                item["resolution"] = defaults.get("resolution")
        else:
            item["size"], _ratio, _resolution = _codex_agent_image_size_from_item(item, image_defaults)
            item["ratio"] = _codex_agent_image_ratio_key(_ratio)
            item["resolution"] = _resolution
            item.setdefault("aspect_ratio", _ratio)
            item.setdefault("count", 1)
            item.setdefault("quality", image_defaults.get("quality") or "auto")
        prepared.append(item)
    spec["items"] = prepared
    return spec


def _codex_agent_batch_plan_rows(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    provider_names = {
        str(provider.get("id") or ""): str(provider.get("name") or provider.get("id") or "")
        for provider in load_api_providers()
        if isinstance(provider, dict)
    }
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for item in spec.get("items") or []:
        if not isinstance(item, dict):
            continue
        kind = "video" if str(item.get("kind") or "image") == "video" else "image"
        provider_id = str(item.get("provider_id") or "")
        key = (kind, provider_id)
        provider_label = "GPT" if provider_id.lower() == "codex" else (provider_names.get(provider_id) or provider_id)
        row = grouped.setdefault(key, {
            "kind": kind,
            "node_label": "视频节点" if kind == "video" else "生图节点",
            "provider_id": provider_id,
            "provider": provider_label,
            "node_count": 0,
            "output_count": 0,
            "unit": "个" if kind == "video" else "张",
        })
        row["node_count"] += 1
        row["output_count"] += 1 if kind == "video" else max(1, int(item.get("count") or 1))
    config = _complex_task_engine_required().config()
    configured = spec.get("concurrency") if isinstance(spec.get("concurrency"), dict) else {}
    provider_overrides = configured.get("providers") if isinstance(configured.get("providers"), dict) else {}
    for row in grouped.values():
        provider_id = str(row["provider_id"])
        if provider_id in provider_overrides:
            limit = int(provider_overrides[provider_id])
        elif row["kind"] == "video":
            limit = int(config.get("video") or 1)
        elif any(marker in provider_id.lower() for marker in ("gpt", "openai", "codex")):
            limit = int(config.get("gpt_image") or 3)
        else:
            limit = int(config.get("unknown_image") or 1)
        row["concurrency"] = max(1, limit)
    return list(grouped.values())


def _codex_agent_generation_request(tool: str, args: Dict[str, Any], canvas_context: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if tool not in {"generate_images", "generate_videos"}:
        return None
    is_video = tool == "generate_videos"
    native = canvas_context.get("native") if isinstance(canvas_context, dict) and isinstance(canvas_context.get("native"), dict) else {}
    defaults = native.get("videoGeneration" if is_video else "imageGeneration") if isinstance(native, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    raw_items = args.get("items") or args.get("prompts") or ([args] if args else [])
    source_items = raw_items if isinstance(raw_items, list) else [raw_items]
    items: List[Dict[str, Any]] = []
    image_count = 0
    for raw in source_items:
        item = {"prompt": raw} if isinstance(raw, str) else (dict(raw) if isinstance(raw, dict) else {})
        prompt = str(item.get("prompt") or item.get("text") or "").strip()
        if not prompt:
            continue
        if is_video:
            item.setdefault("duration", item.get("seconds") or defaults.get("duration") or 5)
        else:
            count = max(1, min(8, int(item.get("count") or item.get("n") or defaults.get("count") or 1)))
            item["count"] = count
            image_count += count
        requested_provider = str(item.get("provider_id") or item.get("providerId") or item.get("provider") or "").strip()
        if requested_provider:
            provider = _codex_agent_resolve_generation_provider(requested_provider, is_video=is_video)
            if not provider:
                raise HTTPException(status_code=400, detail=f"未找到可用的生成平台「{requested_provider}」。请使用当前画布已配置的平台名称或 provider_id。")
            item["provider_id"] = str(provider.get("id") or "")
            item["model"] = _codex_agent_generation_model_from_canvas_default(provider, item.get("model"), defaults, is_video=is_video)
        else:
            item["provider_id"] = str(defaults.get("provider_id") or "")
            item["model"] = str(item.get("model") or defaults.get("model") or "")
        if not is_video:
            item["size"], _ratio, _resolution = _codex_agent_image_size_from_item(item, defaults)
        items.append(item)
    if not items:
        return None
    video_count = len(items) if is_video else 0
    node_action = "create_video_generation_node" if is_video else "create_image_generation_node"
    options = args.get("options") if isinstance(args.get("options"), dict) else {}
    provider = str(items[0].get("provider_id") or "")
    model = str(items[0].get("model") or "")
    # Providers do not share a reliable credit/pricing schema. We only surface a
    # conservative workload warning, never a made-up credit estimate.
    high_workload = (video_count >= 2) or (image_count >= 4) or (len(items) >= 4)
    return {
        "kind": "video" if is_video else "image",
        "items": items,
        "options": options,
        "node_actions": [{"type": node_action, "items": items, "options": options}],
        "node_count": len(items),
        "image_count": image_count,
        "video_count": video_count,
        "provider_id": provider,
        "model": model,
        "high_workload": high_workload,
        "workload_warning": "本次任务规模较大，可能消耗大量积分" if high_workload else "",
    }


def _codex_agent_generation_summary_text(generation: Dict[str, Any]) -> str:
    nodes = int(generation.get("node_count") or 0)
    images = int(generation.get("image_count") or 0)
    videos = int(generation.get("video_count") or 0)
    parts = [f"将创建 {nodes} 个生成节点"]
    if images:
        parts.append(f"提交 {images} 张图片生成")
    if videos:
        parts.append(f"提交 {videos} 个视频生成")
    provider = str(generation.get("provider_id") or "")
    model = str(generation.get("model") or "")
    if provider or model:
        parts.append(f"模型：{provider or '默认'} / {model or '默认'}")
    if generation.get("workload_warning"):
        parts.append(str(generation.get("workload_warning")))
    parts.append("直接提交后由后端持续跟踪，页面刷新不影响任务")
    return "；".join(parts)


def _codex_agent_generation_reference_images(item: Dict[str, Any]) -> List[AIReference]:
    raw_refs = item.get("reference_images") or item.get("references") or item.get("refs") or []
    values = raw_refs if isinstance(raw_refs, list) else [raw_refs]
    out: List[AIReference] = []
    for raw in values[:8]:
        if isinstance(raw, str):
            raw = {"url": raw}
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or raw.get("path") or "").strip()
        if url:
            out.append(AIReference(url=url, name=str(raw.get("name") or ""), role=str(raw.get("role") or ""), kind=str(raw.get("kind") or "image")))
    return out


async def _codex_agent_watch_generation_task(canvas_id: str, node_id: str, task_id: str, kind: str) -> None:
    """Persist provider task completion into the smart canvas; browser lifetime is irrelevant."""
    for _ in range(3600):  # one hour maximum; long provider tasks keep their pending id on the node.
        await asyncio.sleep(1.5)
        with CANVAS_TASK_LOCK:
            task = dict(CANVAS_TASKS.get(task_id) or {})
        status = str(task.get("status") or "").lower()
        if status in {"queued", "running", ""}:
            continue
        if status == "jimeng_pending":
            return
        try:
            canvas = load_canvas(canvas_id)
            node = next((item for item in (canvas.get("nodes") or []) if str(item.get("id") or "") == node_id), None)
            if not node:
                return
            if status == "succeeded":
                result = task.get("result") if isinstance(task.get("result"), dict) else {}
                raw_items = result.get("image_items") if kind == "image" else result.get("video_items")
                if not isinstance(raw_items, list):
                    raw_items = result.get("images") if kind == "image" else result.get("videos")
                media = []
                for index, raw in enumerate(raw_items or []):
                    data = {"url": raw} if isinstance(raw, str) else (raw if isinstance(raw, dict) else {})
                    url = str(data.get("url") or "").strip()
                    if url:
                        media.append({"url": url, "name": str(data.get("name") or f"output-{index + 1}.{'png' if kind == 'image' else 'mp4'}"), "kind": str(data.get("kind") or kind), "generatedResult": True})
                node["images"] = media
                node["title"] = "Video" if kind == "video" else ("Group" if len(media) > 1 else "Image")
                node["outputKind"] = kind
                node.pop("generationError", None)
            else:
                node["generationError"] = {"message": str(task.get("error") or "生成任务失败"), "kind": kind, "logged": False}
            finished_at = _codex_agent_now()
            started_at = int(node.get("runStartedAt") or node.get("runAt") or finished_at)
            node["runStartedAt"] = started_at
            node["runFinishedAt"] = finished_at
            node["runElapsedMs"] = max(0, finished_at - started_at)
            node["runTimerHidden"] = False
            node["pending"] = 0
            node["running"] = False
            node["pendingTasks"] = [entry for entry in (node.get("pendingTasks") or []) if str(entry.get("taskId") or "") != task_id]
            save_canvas(canvas)
            await manager.broadcast_canvas_updated(canvas_id, int(canvas.get("updated_at") or _codex_agent_now()), "codex-agent")
        except Exception as exc:
            print(f"[codex-agent] generation task sync failed: {exc}")
        return


async def _codex_agent_resume_jimeng_generation_task(task_id: str) -> None:
    """Resume polling a persisted provider task after an app-server restart."""
    for _ in range(3600):
        with CANVAS_TASK_LOCK:
            task = CANVAS_TASKS.get(task_id)
            if not task or str(task.get("status") or "") in {"cancelled", "succeeded", "failed"}:
                return
            submit_id = str(task.get("submit_id") or task.get("upstream_task_id") or "")
            kind = str(task.get("kind") or "image")
        if not submit_id:
            return
        try:
            queried = await jimeng_query_result(submit_id, kind)
            urls = await jimeng_store_outputs(queried, kind, allow_query=False)
            with CANVAS_TASK_LOCK:
                task = CANVAS_TASKS.get(task_id)
                if not task:
                    return
                task.update({
                    "status": "succeeded",
                    "result": {"image_items" if kind == "image" else "video_items": urls},
                    "error": "",
                    "updated_at": time.time(),
                })
                _canvas_generation_task_persist(task)
                canvas_id = str(task.get("canvas_id") or "")
                node_id = str(task.get("node_id") or "")
            if canvas_id and node_id:
                asyncio.create_task(_codex_agent_watch_generation_task(canvas_id, node_id, task_id, kind))
            return
        except JimengPendingError as exc:
            with CANVAS_TASK_LOCK:
                task = CANVAS_TASKS.get(task_id)
                if task:
                    task.update({"status": "jimeng_pending", "submit_id": exc.submit_id, "queue_info": exc.queue_info, "updated_at": time.time()})
                    _canvas_generation_task_persist(task)
            await asyncio.sleep(4)
        except Exception as exc:
            with CANVAS_TASK_LOCK:
                task = CANVAS_TASKS.get(task_id)
                if task:
                    task.update({"status": "failed", "error": str(getattr(exc, "detail", None) or exc), "updated_at": time.time()})
                    _canvas_generation_task_persist(task)
            return


async def _codex_agent_restore_generation_tasks() -> None:
    """Rehydrate task/node links from SQLite. Known provider task ids resume polling safely."""
    rows = _canvas_generation_task_rows(["queued", "running", "jimeng_pending"])
    for row in rows:
        task_id = str(row.get("task_id") or "")
        if not task_id:
            continue
        task = {
            "id": task_id,
            "type": "canvas-video" if str(row.get("kind") or "") == "video" else "online-image",
            "kind": str(row.get("kind") or "image"),
            "status": str(row.get("status") or "queued"),
            "created_at": int(row.get("created_at") or _codex_agent_now()) / 1000,
            "updated_at": int(row.get("updated_at") or _codex_agent_now()) / 1000,
            "provider_id": str(row.get("provider_id") or ""), "model": str(row.get("model") or ""),
            "canvas_id": str(row.get("canvas_id") or ""), "node_id": str(row.get("node_id") or ""),
            "submit_id": str(row.get("upstream_task_id") or ""), "payload": row.get("payload") or {},
            "result": row.get("result") or {}, "error": str(row.get("error") or ""),
        }
        with CANVAS_TASK_LOCK:
            CANVAS_TASKS[task_id] = task
        if task["status"] == "jimeng_pending" and task["submit_id"]:
            asyncio.create_task(_codex_agent_resume_jimeng_generation_task(task_id))
        elif task["status"] in {"queued", "running"}:
            # The provider never returned a durable upstream id. Do not blindly
            # submit a duplicate billable job after restart; expose it for retry.
            with CANVAS_TASK_LOCK:
                task["status"] = "interrupted"
                task["error"] = "服务重启时上游任务尚未返回可恢复任务号；可安全重试"
                task["updated_at"] = time.time()
                _canvas_generation_task_persist(task)
        if task["canvas_id"] and task["node_id"]:
            asyncio.create_task(_codex_agent_watch_generation_task(task["canvas_id"], task["node_id"], task_id, task["kind"]))


async def startup_agent_backend() -> None:
    try:
        await _codex_agent_restore_generation_tasks()
    except Exception as exc:
        print(f"恢复画布生成任务失败: {exc}")
    try:
        if COMPLEX_TASK_ENGINE is not None:
            await COMPLEX_TASK_ENGINE.startup()
    except Exception as exc:
        print(f"恢复批量任务失败: {exc}")


def _codex_agent_generation_task_public(task: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "task_id": str(task.get("id") or task.get("task_id") or ""),
        "kind": str(task.get("kind") or ("video" if task.get("type") == "canvas-video" else "image")),
        "status": str(task.get("status") or "unknown"),
        "provider_id": str(task.get("provider_id") or ""), "model": str(task.get("model") or ""),
        "node_id": str(task.get("node_id") or ""), "canvas_id": str(task.get("canvas_id") or ""),
        "upstream_task_id": str(task.get("submit_id") or task.get("upstream_task_id") or ""),
        "error": str(task.get("error") or ""), "created_at": task.get("created_at"), "updated_at": task.get("updated_at"),
    }


_complex_task_runtimes: Dict[str, CodexAppServerRuntime] = {}


async def _complex_task_broadcast(canvas_id: str, updated_at: int) -> None:
    await manager.broadcast_canvas_updated(canvas_id, int(updated_at or _codex_agent_now()), "codex-agent-complex-task")


async def _complex_task_submit_generation(task_id: str, kind: str, payload: Dict[str, Any], node_id: str) -> Dict[str, Any]:
    task = _complex_task_engine_required().get(task_id, include_items=False)
    canvas_id = str(task.get("canvas_id") or "")
    if kind == "generate_video":
        request = CanvasVideoRequest(
            prompt=str(payload.get("prompt") or payload.get("text") or ""),
            provider_id=str(payload.get("provider_id") or "comfly"),
            model=str(payload.get("model") or ""),
            duration=max(1, min(60, int(payload.get("duration") or 5))),
            aspect_ratio=str(payload.get("aspect_ratio") or payload.get("aspect") or "16:9"),
            resolution=str(payload.get("resolution") or ""),
            images=_codex_agent_generation_reference_images(payload),
            camerafixed=bool(payload.get("camerafixed", payload.get("camera_fixed", False))),
            generate_audio=bool(payload.get("generate_audio", False)),
        )
        info = await create_canvas_video_task(request)
        media_kind = "video"
    else:
        request = OnlineImageRequest(
            prompt=str(payload.get("prompt") or payload.get("text") or ""),
            provider_id=str(payload.get("provider_id") or "comfly"),
            model=str(payload.get("model") or ""),
            size=str(payload.get("size") or "1024x1024"),
            quality=str(payload.get("quality") or "auto"),
            n=max(1, min(8, int(payload.get("count") or payload.get("n") or 1))),
            reference_images=_codex_agent_generation_reference_images(payload),
        )
        info = await create_canvas_image_task(request)
        media_kind = "image"
    provider_task_id = str(info.get("task_id") or "")
    with CANVAS_TASK_LOCK:
        provider_task = CANVAS_TASKS.get(provider_task_id)
        if provider_task:
            provider_task.update({"canvas_id": canvas_id, "node_id": node_id, "kind": media_kind, "complex_task_id": task_id})
            _canvas_generation_task_persist(provider_task)
    try:
        canvas = load_canvas(canvas_id)
        node = next((row for row in canvas.get("nodes") or [] if str(row.get("id")) == node_id), None)
        if node:
            node.update({"pending": 1, "running": True, "runStartedAt": _codex_agent_now(), "runTimerHidden": False})
            node.pop("queued", None)
            node.pop("generationError", None)
            node["pendingTasks"] = [{"taskId": provider_task_id, "kind": media_kind, "providerId": payload.get("provider_id") or "", "model": payload.get("model") or ""}]
            save_canvas(canvas)
            await _complex_task_broadcast(canvas_id, int(canvas.get("updated_at") or _codex_agent_now()))
    except Exception:
        pass
    return {"task_id": provider_task_id}


async def _complex_task_poll_generation(provider_task_id: str) -> Dict[str, Any]:
    for _ in range(7200):
        with CANVAS_TASK_LOCK:
            task = dict(CANVAS_TASKS.get(provider_task_id) or {})
        status = str(task.get("status") or "")
        if status == "succeeded":
            return {"status": status, **(task.get("result") if isinstance(task.get("result"), dict) else {})}
        if status in {"failed", "cancelled", "interrupted"}:
            return {"status": status, "error": str(task.get("error") or "生成任务失败")}
        if status == "jimeng_pending":
            submit_id = str(task.get("submit_id") or task.get("upstream_task_id") or "")
            if submit_id:
                try:
                    queried = await jimeng_query_result(submit_id, str(task.get("kind") or "image"))
                    urls = await jimeng_store_outputs(queried, str(task.get("kind") or "image"), allow_query=False)
                    key = "video_items" if str(task.get("kind") or "") == "video" else "image_items"
                    with CANVAS_TASK_LOCK:
                        live = CANVAS_TASKS.get(provider_task_id)
                        if live:
                            live.update({"status": "succeeded", "result": {key: urls}, "error": "", "updated_at": time.time()})
                            _canvas_generation_task_persist(live)
                    continue
                except JimengPendingError:
                    pass
                except Exception as exc:
                    with CANVAS_TASK_LOCK:
                        live = CANVAS_TASKS.get(provider_task_id)
                        if live:
                            live.update({"status": "failed", "error": str(exc), "updated_at": time.time()})
                            _canvas_generation_task_persist(live)
                    continue
        await asyncio.sleep(1.5)
    return {"status": "failed", "error": "批量任务等待 Provider 超时"}


def _complex_task_review_json(text: str) -> Dict[str, Any]:
    clean = str(text or "").strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", clean, re.I)
    candidates = [match.group(1).strip()] if match else []
    candidates.append(clean)
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict) and value.get("decision"):
                return value
        except Exception:
            continue
    return {}


def _complex_task_extract_video_frames(video_path: str, project_dir: str) -> List[str]:
    """Extract first/middle/last review frames; absence is reported to the task Agent."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return []
    duration = 0.0
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            probe = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", video_path],
                capture_output=True, text=True, timeout=20, check=False,
            )
            duration = max(0.0, float((probe.stdout or "0").strip() or 0))
        except Exception:
            duration = 0.0
    timestamps = [0.0] if duration <= 0 else [0.0, duration * 0.5, max(0.0, duration * 0.9)]
    target_dir = _codex_agent_refs_dir(project_dir)
    frames: List[str] = []
    for index, timestamp in enumerate(timestamps):
        target = target_dir / f"task-video-{uuid.uuid4().hex}-{index + 1}.jpg"
        try:
            result = subprocess.run(
                [ffmpeg, "-y", "-ss", f"{timestamp:.3f}", "-i", video_path, "-frames:v", "1", "-q:v", "3", str(target)],
                capture_output=True, timeout=45, check=False,
            )
            if result.returncode == 0 and target.is_file() and target.stat().st_size > 0:
                frames.append(str(target))
        except Exception:
            continue
    return frames


async def _complex_task_checkpoint(task: Dict[str, Any], checkpoint: Dict[str, Any]) -> Dict[str, Any]:
    task_id = str(task.get("id") or "")
    project_dir = _codex_agent_effective_project_dir(str(task.get("project_dir") or ""))
    runtime = _complex_task_runtimes.get(task_id)
    if runtime is None:
        runtime = CodexAppServerRuntime(f"complex:{task_id}", project_dir, _CODEX_AGENT_RUNTIME_DEPENDENCIES)
        _complex_task_runtimes[task_id] = runtime
    started = await runtime.start(thread_id=str(task.get("task_thread_id") or ""))
    _complex_task_engine_required()._set_task(task_id, str(task.get("status") or "reviewing"), task_thread_id=str(started.get("thread_id") or ""))
    completed = []
    failed = []
    image_paths: List[str] = []
    video_review_notes: List[Dict[str, Any]] = []
    stage_types = {str(stage.get("spec", {}).get("id") or ""): str(stage.get("type") or "") for stage in task.get("stages") or []}
    checkpoint_stage = str(checkpoint.get("stage_id") or "")
    canvas_skill = _canvas_agent_active_skill_context("/批量任务", {"command": "/批量任务", "intent": "global_canvas"})
    async with httpx.AsyncClient() as client:
        for item in task.get("items") or []:
            compact = {"item_id": str(item.get("id") or "").split(":", 1)[-1], "stage_id": item.get("stage_id"), "title": item.get("title"), "status": item.get("status"), "attempt_count": item.get("attempt_count"), "error": item.get("error") or ""}
            if item.get("status") == "completed":
                completed.append(compact)
                result = item.get("result") if isinstance(item.get("result"), dict) else {}
                should_attach = checkpoint_stage in {"", "__final__"} or checkpoint_stage == str(item.get("stage_id") or "")
                media = result.get("image_items") or result.get("images") or []
                for raw in (media[:3] if should_attach and len(image_paths) < 20 else []):
                    value = raw if isinstance(raw, dict) else {"url": raw}
                    url = str(value.get("url") or "")
                    if not url:
                        continue
                    try:
                        ref = await _codex_agent_prepare_attachment({"url": url, "name": value.get("name") or "task-output.png", "kind": "image"}, project_dir, client)
                        if ref.get("local_path"):
                            image_paths.append(str(ref["local_path"]))
                    except Exception:
                        pass
                videos = result.get("video_items") or result.get("videos") or []
                if should_attach and len(image_paths) < 20 and (stage_types.get(str(item.get("stage_id") or "")) == "generate_video" or videos):
                    extracted = 0
                    for raw in videos[:2]:
                        value = raw if isinstance(raw, dict) else {"url": raw}
                        url = str(value.get("url") or "")
                        if not url:
                            continue
                        try:
                            ref = await _codex_agent_prepare_attachment({"url": url, "name": value.get("name") or "task-output.mp4", "kind": "video"}, project_dir, client)
                            local_path = str(ref.get("local_path") or "")
                            if local_path:
                                frames = await asyncio.to_thread(_complex_task_extract_video_frames, local_path, project_dir)
                                image_paths.extend(frames)
                                extracted += len(frames)
                        except Exception:
                            pass
                    video_review_notes.append({"item_id": compact["item_id"], "frames_extracted": extracted, "visual_review_available": extracted > 0})
            elif item.get("status") in {"failed", "blocked", "interrupted"}:
                failed.append(compact)
    prompt = (
        "你是 Infinite Canvas 的历史任务验收器。\n"
        "你可以读取已安装 Codex Skills、当前 Canvas Skill 和画布查询工具，但不能直接写工作区或任意修改画布；任务修改只能使用任务 Dynamic Tools。\n"
        "检查结果后必须调用 submit_task_review，decision 只能是 accept/retry/revise_prompt/block/ask_user。\n"
        f"canvas_skill: {canvas_skill.get('instructions') or ''}\n"
        f"task_id: {task_id}\ncheckpoint: {json.dumps(checkpoint, ensure_ascii=False)}\n"
        f"task_spec: {json.dumps(task.get('spec') or {}, ensure_ascii=False)[:20000]}\n"
        f"completed_items: {json.dumps(completed, ensure_ascii=False)[:16000]}\n"
        f"failed_items: {json.dumps(failed, ensure_ascii=False)[:8000]}\n"
        f"video_review: {json.dumps(video_review_notes, ensure_ascii=False)}\n"
        "若视频没有可用抽帧，只能核对文件、时长和 Provider 状态；涉及内容质量时必须 ask_user，不能假装完成了视觉验收。"
    )
    review: Dict[str, Any] = {}
    last_text = ""
    async for event in runtime.send_user_message(prompt, image_paths, sandbox_policy={"type": "readOnly"}):
        method = str(event.get("method") or "")
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        if method == "item/tool/call":
            request_id = event.get("id")
            tool = str(params.get("tool") or "").strip().lower().replace("-", "_")
            raw_args = params.get("arguments")
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except Exception:
                    raw_args = {}
            args = dict(raw_args or {}) if isinstance(raw_args, dict) else {}
            args["task_id"] = task_id
            allowed_task_tools = {"get_task_checkpoint", "submit_task_review", "revise_pending_items", "retry_task_items", "ask_task_user"}
            spec = _codex_agent_canvas_tool_registry().get(tool) or {}
            if tool not in allowed_task_tools and not spec.get("query"):
                result = {"ok": False, "tool": tool, "message": "任务 Agent 只能查询画布；写入必须使用任务专用工具"}
            elif tool == "submit_task_review":
                review = args
                result = {"ok": True, "tool": tool, "message": "验收决定已接收"}
            elif tool == "ask_task_user":
                review = {"task_id": task_id, "decision": "ask_user", "question": args.get("question") or "", "reason": args.get("reason") or ""}
                result = {"ok": True, "tool": tool, "message": "用户问题已接收"}
            elif tool == "retry_task_items":
                review = {"task_id": task_id, "decision": "retry", "item_ids": args.get("item_ids") or []}
                result = {"ok": True, "tool": tool, "message": "重试决定已接收"}
            else:
                try:
                    result = await _codex_agent_run_canvas_tool(CodexAgentCanvasToolRequest(canvas_id=str(task.get("canvas_id") or ""), tool=tool, args=args, refs=[], canvas_context={"_complex_task_id": task_id}))
                except Exception as exc:
                    result = {"ok": False, "tool": tool, "message": str(getattr(exc, "detail", None) or exc)}
            await runtime.respond_dynamic_tool_call(request_id, _codex_agent_dynamic_tool_response(result, success=bool(result.get("ok", True))))
            continue
        if method == "item/completed":
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            if str(item.get("type") or "") == "agentMessage":
                last_text = str(item.get("text") or "")
    return review or _complex_task_review_json(last_text)


def _configure_complex_task_engine() -> None:
    global COMPLEX_TASK_ENGINE
    if not _host_dependencies:
        return
    COMPLEX_TASK_ENGINE = ComplexTaskEngine(
        CODEX_AGENT_HOME / "infinite-canvas-agent" / "history.sqlite",
        CODEX_AGENT_HOME / "infinite-canvas-agent" / "complex-task-config.json",
        load_canvas=load_canvas,
        save_canvas=save_canvas,
        broadcast_canvas=_complex_task_broadcast,
        submit_generation=_complex_task_submit_generation,
        poll_generation=_complex_task_poll_generation,
        run_checkpoint=None,
        revision_observer=lambda canvas_id, canvas: CODEX_AGENT_REVISION_STORE.observe(canvas_id, canvas),
        initialize=False,
    )


def _complex_task_engine_required() -> ComplexTaskEngine:
    if COMPLEX_TASK_ENGINE is None:
        raise HTTPException(status_code=503, detail="批量任务引擎尚未初始化")
    return COMPLEX_TASK_ENGINE


async def _codex_agent_generation_control(tool: str, args: Dict[str, Any], canvas_id: str) -> Dict[str, Any]:
    if tool == "get_generation_queue":
        statuses = args.get("statuses") if isinstance(args.get("statuses"), list) else None
        rows = _canvas_generation_task_rows([str(item) for item in statuses] if statuses else None)
        tasks = [row for row in rows if not canvas_id or str(row.get("canvas_id") or "") == canvas_id]
        return {"ok": True, "tool": tool, "tasks": [_codex_agent_generation_task_public(row) for row in tasks[:100]], "count": len(tasks)}
    task_id = str(args.get("task_id") or "").strip()
    with CANVAS_TASK_LOCK:
        task = dict(CANVAS_TASKS.get(task_id) or {})
    if not task:
        task = next((row for row in _canvas_generation_task_rows() if str(row.get("task_id") or "") == task_id), {})
        if task:
            task["id"] = task_id
    if not task or (canvas_id and str(task.get("canvas_id") or "") != canvas_id):
        return {"ok": False, "tool": tool, "message": "未找到当前画布中的生成任务"}
    if tool == "get_generation_task":
        return {"ok": True, "tool": tool, "task": _codex_agent_generation_task_public(task)}
    if tool == "cancel_generation_task":
        status = str(task.get("status") or "").lower()
        upstream_accepted = bool(task.get("submit_id") or task.get("upstream_task_id")) or status not in {"queued"}
        if upstream_accepted:
            return {"ok": False, "tool": tool, "message": "任务已提交上游，无法取消；对应节点将继续运行"}
        worker = CANVAS_TASK_WORKERS.get(task_id)
        if worker and not worker.done():
            worker.cancel()
        with CANVAS_TASK_LOCK:
            live = CANVAS_TASKS.get(task_id, task)
            live.update({"status": "cancelled", "error": "用户取消", "updated_at": time.time()})
            CANVAS_TASKS[task_id] = live
            _canvas_generation_task_persist(live)
        if live.get("canvas_id") and live.get("node_id"):
            canvas = load_canvas(str(live["canvas_id"]))
            node = next((item for item in (canvas.get("nodes") or []) if str(item.get("id") or "") == str(live["node_id"])), None)
            if node:
                node["running"] = False
                node["pending"] = 0
                node["generationError"] = {"message": "已取消生成任务", "kind": live.get("kind") or "image", "logged": False}
                node["pendingTasks"] = [entry for entry in (node.get("pendingTasks") or []) if str(entry.get("taskId") or "") != task_id]
                save_canvas(canvas)
                await manager.broadcast_canvas_updated(str(live["canvas_id"]), int(canvas.get("updated_at") or _codex_agent_now()), "codex-agent")
        return {"ok": True, "tool": tool, "changed": 1, "task": _codex_agent_generation_task_public(live), "message": "已取消尚未提交上游的排队任务"}
    if tool == "retry_generation_task":
        if str(task.get("status") or "") not in {"failed", "interrupted", "cancelled"}:
            return {"ok": False, "tool": tool, "message": "只有失败、中断或已取消的任务可以重试"}
        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        kind = str(task.get("kind") or "image")
        try:
            info = await (create_canvas_video_task(CanvasVideoRequest(**payload)) if kind == "video" else create_canvas_image_task(OnlineImageRequest(**payload)))
        except Exception as exc:
            return {"ok": False, "tool": tool, "message": f"重试创建失败：{exc}"}
        new_id = str(info.get("task_id") or "")
        with CANVAS_TASK_LOCK:
            live = CANVAS_TASKS.get(new_id) or {}
            live.update({"canvas_id": str(task.get("canvas_id") or ""), "node_id": str(task.get("node_id") or ""), "kind": kind})
            _canvas_generation_task_persist(live)
        if live.get("canvas_id") and live.get("node_id"):
            canvas = load_canvas(str(live["canvas_id"]))
            node = next((item for item in (canvas.get("nodes") or []) if str(item.get("id") or "") == str(live["node_id"])), None)
            if node:
                node.update({"pending": 1, "running": True, "runStartedAt": _codex_agent_now(), "runTimerHidden": False})
                node.pop("runFinishedAt", None)
                node.pop("runElapsedMs", None)
                node.pop("generationError", None)
                node["pendingTasks"] = [{"taskId": new_id, "kind": kind, "providerId": live.get("provider_id") or "", "model": live.get("model") or ""}]
                save_canvas(canvas)
                await manager.broadcast_canvas_updated(str(live["canvas_id"]), int(canvas.get("updated_at") or _codex_agent_now()), "codex-agent")
        if live.get("canvas_id") and live.get("node_id"):
            asyncio.create_task(_codex_agent_watch_generation_task(live["canvas_id"], live["node_id"], new_id, kind))
        return {"ok": True, "tool": tool, "changed": 1, "task": _codex_agent_generation_task_public(live), "message": "已创建重试任务"}
    return {"ok": False, "tool": tool, "message": "不支持的生成任务操作"}


async def _codex_agent_submit_generation(
    canvas_id: str,
    generation: Dict[str, Any],
    refs: List[Dict[str, Any]],
    canvas_context: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    kind = str(generation.get("kind") or "image")
    items = generation.get("items") if isinstance(generation.get("items"), list) else []
    node_actions = generation.get("node_actions") if isinstance(generation.get("node_actions"), list) else []
    created = await _codex_agent_apply_canvas_actions(canvas_id, node_actions, refs, canvas_context)
    node_rows = [item for row in (created.get("results") or []) for item in (row.get("items") or []) if isinstance(item, dict)]
    submitted = []
    for index, node in enumerate(node_rows):
        item = items[index] if index < len(items) and isinstance(items[index], dict) else {}
        prompt = str(item.get("prompt") or item.get("text") or "").strip()
        if not prompt:
            continue
        if kind == "video":
            request = CanvasVideoRequest(
                prompt=prompt,
                provider_id=str(item.get("provider_id") or "comfly"),
                model=str(item.get("model") or ""),
                duration=max(1, min(60, int(item.get("duration") or 5))),
                aspect_ratio=str(item.get("aspect_ratio") or item.get("aspect") or "16:9"),
                resolution=str(item.get("resolution") or ""),
                images=_codex_agent_generation_reference_images(item),
                camerafixed=bool(item.get("camerafixed", item.get("camera_fixed", False))),
                generate_audio=bool(item.get("generate_audio", False)),
            )
            task_info = await create_canvas_video_task(request)
        else:
            request = OnlineImageRequest(
                prompt=prompt,
                provider_id=str(item.get("provider_id") or "comfly"),
                model=str(item.get("model") or ""),
                size=str(item.get("size") or "1024x1024"),
                quality=str(item.get("quality") or "auto"),
                n=max(1, min(8, int(item.get("count") or item.get("n") or 1))),
                reference_images=_codex_agent_generation_reference_images(item),
            )
            task_info = await create_canvas_image_task(request)
        task_id = str(task_info.get("task_id") or "")
        with CANVAS_TASK_LOCK:
            task = CANVAS_TASKS.get(task_id)
            if task:
                task.update({"canvas_id": canvas_id, "node_id": str(node.get("id") or ""), "kind": kind})
                _canvas_generation_task_persist(task)
        submitted.append({"node_id": node.get("id"), "task_id": task_id, "kind": kind})

    canvas = load_canvas(canvas_id)
    for item in submitted:
        node = next((row for row in (canvas.get("nodes") or []) if str(row.get("id") or "") == str(item.get("node_id") or "")), None)
        if not node:
            continue
        node["pending"] = 1
        node["running"] = True
        node["runStartedAt"] = _codex_agent_now()
        node.pop("runFinishedAt", None)
        node.pop("runElapsedMs", None)
        node["runTimerHidden"] = False
        node["pendingTasks"] = [{"taskId": item["task_id"], "kind": kind, "providerId": generation.get("provider_id") or "", "model": generation.get("model") or ""}]
    if submitted:
        save_canvas(canvas)
        current_revision = int(CODEX_AGENT_REVISION_STORE.observe(canvas_id, canvas).get("revision") or 0)
        _codex_agent_advance_context_snapshot(canvas_context, canvas_id, canvas, current_revision)
        await manager.broadcast_canvas_updated(canvas_id, int(canvas.get("updated_at") or _codex_agent_now()), "codex-agent")
        for item in submitted:
            asyncio.create_task(_codex_agent_watch_generation_task(canvas_id, str(item.get("node_id") or ""), str(item.get("task_id") or ""), kind))
    return {"ok": bool(submitted), "changed": len(submitted), "skipped": max(0, len(items) - len(submitted)), "results": [{"type": "generate_" + kind, "status": "submitted", "items": node_rows}], "submitted": submitted, "message": _codex_agent_generation_summary_text(generation) + "；已由后端提交并持续跟踪"}


async def _codex_agent_handle_native_tool_call(
    task_id: str,
    runtime: CodexAppServerRuntime,
    event: Dict[str, Any],
    refs: List[Dict[str, Any]],
    canvas_context: Optional[Dict[str, Any]],
) -> None:
    params = event.get("params") if isinstance(event.get("params"), dict) else {}
    request_id = event.get("id")
    namespace = str(params.get("namespace") or "").strip()
    tool = str(params.get("tool") or "").strip().lower().replace("-", "_")
    raw_args = params.get("arguments")
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except Exception:
            raw_args = {}
    args = raw_args if isinstance(raw_args, dict) else {}
    registry = _codex_agent_canvas_tool_registry()
    spec = registry.get(tool)
    if namespace and namespace != CODEX_AGENT_DYNAMIC_TOOL_NAMESPACE:
        result = {"ok": False, "tool": tool or "unknown", "message": "不支持的原生命名空间"}
        await runtime.respond_dynamic_tool_call(request_id, _codex_agent_dynamic_tool_response(result, success=False))
        return
    if not spec:
        result = {"ok": False, "tool": tool or "unknown", "message": "不支持的画布工具"}
        await runtime.respond_dynamic_tool_call(request_id, _codex_agent_dynamic_tool_response(result, success=False))
        return

    with _codex_agent_task_lock:
        host_task = _codex_agent_tasks.get(task_id) or {}
        canvas_id = str(host_task.get("canvas_id") or "")
    user_text = str((canvas_context or {}).get("_agent_user_text") or "")
    is_batch_turn = bool(re.search(r"/(?:批量任务)(?:\s|$)", user_text))
    if is_batch_turn and tool in {"generate_images", "generate_videos"}:
        result = {
            "ok": False, "tool": tool,
            "message": "批量任务必须先由 create_batch_task 提交完整清单，不能绕过批量队列直接生成。",
        }
        await runtime.respond_dynamic_tool_call(request_id, _codex_agent_dynamic_tool_response(result, success=False))
        _codex_agent_add_task_event(task_id, {"method": "canvas/tool_result", "params": {"tool": tool, "result": result, "native": True}})
        return

    batch_tools = {"create_batch_task", "get_batch_task", "control_batch_task"}
    if tool in batch_tools:
        if tool == "create_batch_task":
            raw_spec = args.get("spec") if isinstance(args.get("spec"), dict) else {}
            try:
                prepared_spec = _codex_agent_prepare_batch_spec(raw_spec, canvas_context)
                preview_spec = normalize_batch_task_spec(prepared_spec, canvas_id=canvas_id)
            except ComplexTaskError as exc:
                result = {"ok": False, "tool": tool, "message": str(exc)}
                await runtime.respond_dynamic_tool_call(request_id, _codex_agent_dynamic_tool_response(result, success=False))
                return
            rows = _codex_agent_batch_plan_rows(prepared_spec)
            item_count = len(prepared_spec.get("items") or [])
            provider_calls = sum(int(row.get("output_count") or 0) for row in rows)
            approval_id = _codex_agent_uid("approval")
            with _codex_agent_task_lock:
                host_task = _codex_agent_tasks.get(task_id)
                if host_task is not None:
                    host_task.setdefault("pending_action_approvals", {})[approval_id] = {
                        "approval_id": approval_id, "kind": "batch_task", "request_id": request_id,
                        "tool": tool, "args": {"spec": prepared_spec}, "canvas_id": canvas_id,
                        "created_at": _codex_agent_now(), "status": "pending",
                    }
            _codex_agent_add_task_event(task_id, {"method": "canvas/action_pending", "params": {
                "task_id": task_id, "approval_id": approval_id, "tool": tool, "native": True,
                "confirmation_kind": "batch_plan", "title": "批量执行确认", "risk": "normal",
                "reason": f"已规划 {item_count} 个节点、预计 {provider_calls} 次生成调用。确认后将创建批量节点，并按各平台并发限制排队执行。",
                "count": item_count, "summary_rows": rows, "actions": [],
                "options": [{"label": "确认执行", "value": "execute", "action": "resolve_canvas_action"}, {"label": "取消", "value": "skip", "action": "resolve_canvas_action"}],
            }})
            return
        try:
            result = await _codex_agent_run_canvas_tool(CodexAgentCanvasToolRequest(canvas_id=canvas_id, tool=tool, args=args, refs=refs, canvas_context=canvas_context))
        except HTTPException as exc:
            result = {"ok": False, "tool": tool, "message": str(exc.detail)}
        await runtime.respond_dynamic_tool_call(request_id, _codex_agent_dynamic_tool_response(result, success=bool(result.get("ok", True))))
        _codex_agent_add_task_event(task_id, {"method": "canvas/tool_result", "params": {"tool": tool, "query": bool(spec.get("query")), "result": result, "native": True}})
        return

    query = bool(spec.get("query"))
    _codex_agent_add_task_event(task_id, {
        "method": "canvas/tool_call",
        "params": {"tool": tool, "args": args, "query": query, "native": True},
    })
    with _codex_agent_task_lock:
        task = _codex_agent_tasks.get(task_id) or {}
        canvas_id = str(task.get("canvas_id") or "")

    try:
        tool_payload = CodexAgentCanvasToolRequest(
            canvas_id=canvas_id,
            tool=tool,
            args=args,
            refs=refs,
            canvas_context=canvas_context,
        )
        if query:
            result = await _codex_agent_run_canvas_tool(tool_payload)
            _codex_agent_add_task_event(task_id, {"method": "canvas/tool_result", "params": {"tool": tool, "query": True, "result": result, "native": True}})
            await runtime.respond_dynamic_tool_call(request_id, _codex_agent_dynamic_tool_response(result))
            return

        if tool in {"cancel_generation_task", "retry_generation_task"}:
            result = await _codex_agent_generation_control(tool, args, canvas_id)
            _codex_agent_add_task_event(task_id, {"method": "canvas/tool_result", "params": {"tool": tool, "query": False, "result": result, "native": True}})
            await runtime.respond_dynamic_tool_call(request_id, _codex_agent_dynamic_tool_response(result, success=bool(result.get("ok"))))
            return
        actions = _codex_agent_native_tool_actions(tool, args)
        approval = _codex_agent_action_approval_requirement(actions, canvas_context)
        generation = _codex_agent_generation_request(tool, args, canvas_context)
        if generation:
            agent_input = canvas_context.get("agentInput") if isinstance(canvas_context, dict) and isinstance(canvas_context.get("agentInput"), dict) else {}
            generation_policy = str(agent_input.get("approvalPolicy") or agent_input.get("approval_policy") or "auto").strip().lower()
            if generation_policy in {"confirm-risky", "risky", "high-risk"} and int(generation.get("image_count") or 0) >= 3 and not approval.get("required"):
                approval = {"required": True, "risk": "high", "reason": f"将提交 {generation.get('image_count')} 张图片生成"}
            if not approval.get("required"):
                result = await _codex_agent_submit_generation(canvas_id, generation, refs, canvas_context)
                result["tool"] = tool
                if result.get("ok"):
                    with _codex_agent_task_lock:
                        task = _codex_agent_tasks.get(task_id)
                        if task is not None:
                            task["native_generation_submitted"] = True
                _codex_agent_add_task_event(task_id, {"method": "canvas/tool_result", "params": {"tool": tool, "query": False, "result": result, "native": True}})
                await runtime.respond_dynamic_tool_call(request_id, _codex_agent_dynamic_tool_response(result))
                return
            approval_id = _codex_agent_uid("approval")
            with _codex_agent_task_lock:
                task = _codex_agent_tasks.get(task_id)
                if task is not None:
                    task.setdefault("pending_action_approvals", {})[approval_id] = {
                        "approval_id": approval_id,
                        "kind": "generation_action",
                        "request_id": request_id,
                        "call_id": str(params.get("callId") or ""),
                        "tool": tool,
                        "actions": generation.get("node_actions") or [],
                        "generation": generation,
                        "refs": refs,
                        "canvas_context": canvas_context,
                        "created_at": _codex_agent_now(),
                        "status": "pending",
                        "reason": approval.get("reason", ""),
                        "risk": approval.get("risk", "normal"),
                    }
            _codex_agent_add_task_event(task_id, {
                "method": "canvas/action_pending",
                "params": {
                    "task_id": task_id,
                    "approval_id": approval_id,
                    "actions": _codex_agent_public_action_summary(generation.get("node_actions") or []),
                    "reason": _codex_agent_generation_summary_text(generation),
                    "risk": approval.get("risk", "normal"),
                    "count": int(generation.get("node_count") or 0),
                    "tool": tool,
                    "native": True,
                    "options": [
                        {"label": "直接提交生成", "value": "run_generation", "action": "resolve_canvas_action"},
                        {"label": "仅创建生成节点", "value": "create_nodes", "action": "resolve_canvas_action"},
                        {"label": "取消", "value": "skip", "action": "resolve_canvas_action"},
                    ],
                },
            })
            return
        if approval.get("required"):
            approval_id = _codex_agent_uid("approval")
            with _codex_agent_task_lock:
                task = _codex_agent_tasks.get(task_id)
                if task is not None:
                    task.setdefault("pending_action_approvals", {})[approval_id] = {
                        "approval_id": approval_id,
                        "kind": "native_tool",
                        "request_id": request_id,
                        "call_id": str(params.get("callId") or ""),
                        "tool": tool,
                        "actions": actions,
                        "refs": refs,
                        "canvas_context": canvas_context,
                        "created_at": _codex_agent_now(),
                        "status": "pending",
                        "reason": approval.get("reason", ""),
                        "risk": approval.get("risk", "normal"),
                    }
            _codex_agent_add_task_event(task_id, {
                "method": "canvas/action_pending",
                "params": {
                    "task_id": task_id,
                    "approval_id": approval_id,
                    "actions": _codex_agent_public_action_summary(actions),
                    "reason": approval.get("reason", "需要确认后执行"),
                    "risk": approval.get("risk", "normal"),
                    "count": len(actions),
                    "tool": tool,
                    "native": True,
                },
            })
            return

        result = await _codex_agent_apply_canvas_actions(canvas_id, actions, refs, canvas_context)
        result["tool"] = tool
        _codex_agent_add_task_event(task_id, {"method": "canvas/tool_result", "params": {"tool": tool, "query": False, "result": result, "native": True}})
        await runtime.respond_dynamic_tool_call(request_id, _codex_agent_dynamic_tool_response(result))
    except HTTPException as exc:
        result = {"ok": False, "tool": tool, "message": str(exc.detail)}
        _codex_agent_add_task_event(task_id, {"method": "canvas/tool_result", "params": {"tool": tool, "query": query, "result": result, "native": True}})
        await runtime.respond_dynamic_tool_call(request_id, _codex_agent_dynamic_tool_response(result, success=False))
    except Exception as exc:
        result = {"ok": False, "tool": tool, "message": str(exc)}
        _codex_agent_add_task_event(task_id, {"method": "canvas/tool_result", "params": {"tool": tool, "query": query, "result": result, "native": True}})
        await runtime.respond_dynamic_tool_call(request_id, _codex_agent_dynamic_tool_response(result, success=False))


async def _codex_agent_execute_direct_native_tool_call(
    runtime: CodexAppServerRuntime,
    event: Dict[str, Any],
    canvas_id: str,
    refs: List[Dict[str, Any]],
    canvas_context: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compatibility handler for the legacy streaming endpoint, which has no approval UI."""
    params = event.get("params") if isinstance(event.get("params"), dict) else {}
    request_id = event.get("id")
    tool = str(params.get("tool") or "").strip().lower().replace("-", "_")
    namespace = str(params.get("namespace") or "").strip()
    raw_args = params.get("arguments")
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except Exception:
            raw_args = {}
    args = raw_args if isinstance(raw_args, dict) else {}
    spec = _codex_agent_canvas_tool_registry().get(tool)
    query = bool(spec and spec.get("query"))
    if (namespace and namespace != CODEX_AGENT_DYNAMIC_TOOL_NAMESPACE) or not spec:
        result = {"ok": False, "tool": tool or "unknown", "message": "不支持的画布工具"}
        await runtime.respond_dynamic_tool_call(request_id, _codex_agent_dynamic_tool_response(result, success=False))
        return {"tool": tool, "query": query, "result": result}
    try:
        payload = CodexAgentCanvasToolRequest(canvas_id=canvas_id, tool=tool, args=args, refs=refs, canvas_context=canvas_context)
        if query:
            result = await _codex_agent_run_canvas_tool(payload)
        else:
            actions = _codex_agent_native_tool_actions(tool, args)
            approval = _codex_agent_action_approval_requirement(actions, canvas_context)
            generation = _codex_agent_generation_request(tool, args, canvas_context)
            if generation and not approval.get("required"):
                result = await _codex_agent_submit_generation(canvas_id, generation, refs, canvas_context)
                result["tool"] = tool
            elif approval.get("required"):
                result = {"ok": False, "tool": tool, "message": "此接口不支持交互确认；请在画布 Agent 面板中执行该操作"}
            else:
                result = await _codex_agent_apply_canvas_actions(canvas_id, actions, refs, canvas_context)
                result["tool"] = tool
        await runtime.respond_dynamic_tool_call(request_id, _codex_agent_dynamic_tool_response(result))
    except HTTPException as exc:
        result = {"ok": False, "tool": tool, "message": str(exc.detail)}
        await runtime.respond_dynamic_tool_call(request_id, _codex_agent_dynamic_tool_response(result, success=False))
    except Exception as exc:
        result = {"ok": False, "tool": tool, "message": str(exc)}
        await runtime.respond_dynamic_tool_call(request_id, _codex_agent_dynamic_tool_response(result, success=False))
    return {"tool": tool, "query": query, "result": result}


async def _codex_agent_run_background_task(task_id: str, payload: CodexAgentTurnRequest) -> None:
    _codex_agent_set_task_status(task_id, "running")
    _codex_agent_add_task_event(task_id, {"method": "task/started", "params": {"task_id": task_id}})
    try:
        runtime = await _codex_agent_runtime_for_payload(payload)
    except Exception as exc:
        _codex_agent_set_task_status(task_id, "failed", error=f"Agent Runtime 未打开: {payload.project_dir}")
        _codex_agent_add_task_event(task_id, {"method": "error", "params": {"message": f"Agent Runtime 未打开: {payload.project_dir}: {exc}"}})
        return
    if runtime.resume_warning:
        _codex_agent_add_task_event(task_id, {"method": "warning", "params": {"message": runtime.resume_warning, "thread_id": runtime.thread_id}})
    with _codex_agent_task_lock:
        task = _codex_agent_tasks.get(task_id)
        if task is not None:
            task["native_tools_enabled"] = runtime.native_tools_enabled
    refs: List[Dict[str, Any]] = []
    ref_errors: List[str] = []
    try:
        task_canvas_context = dict(payload.canvas_context or {}) if isinstance(payload.canvas_context, dict) else {}
        task_canvas_context["_agent_user_text"] = payload.text
        task_canvas_context["_native_tools_enabled"] = runtime.native_tools_enabled
        if payload.attachments:
            async with httpx.AsyncClient() as client:
                for item in payload.attachments:
                    try:
                        refs.append(await _codex_agent_prepare_attachment(item, _codex_agent_effective_project_dir(payload.project_dir), client))
                    except Exception as e:
                        ref_errors.append(f"{item}: {e}")
        image_refs = [ref for ref in refs if str(ref.get("kind") or "image").lower() == "image"]
        context_text = _codex_agent_build_turn_context_text(payload.project_dir, refs, task_canvas_context, payload)
        turn_text = f"{context_text}\n\n用户请求：\n{payload.text}" if context_text else payload.text
        for err in ref_errors:
            _codex_agent_add_task_event(task_id, {"method": "error", "params": {"message": f"ref download failed: {err}"}})
        pending_turn_text = turn_text
        pending_image_refs = image_refs
        is_batch_request = bool(re.search(r"/(?:批量任务)(?:\s|$)", str(payload.text or "")))
        max_tool_rounds = 2 if runtime.native_tools_enabled and is_batch_request else (1 if runtime.native_tools_enabled else 3)
        batch_create_called = False
        for tool_round in range(max_tool_rounds):
            query_results_for_followup: List[Dict[str, Any]] = []
            batch_tool_refused = False
            async for event in runtime.send_user_message(pending_turn_text, pending_image_refs):
                _codex_agent_add_task_event(task_id, event)
                method = str(event.get("method") or "")
                params = event.get("params") or {}
                item = params.get("item") if isinstance(params, dict) else {}
                if method == "item/tool/call" and runtime.native_tools_enabled:
                    called_tool = str(params.get("tool") or "").strip().lower().replace("-", "_")
                    if called_tool == "create_batch_task":
                        batch_create_called = True
                    await _codex_agent_handle_native_tool_call(task_id, runtime, event, refs, task_canvas_context)
                    continue
                if method == "item/completed" and isinstance(item, dict):
                    if str(item.get("type") or "") == "agentMessage":
                        agent_text = str(item.get("text") or "")
                        if is_batch_request and _codex_agent_is_batch_tool_refusal(agent_text):
                            batch_tool_refused = True
                        if not runtime.native_tools_enabled:
                            tool_results = await _codex_agent_execute_tools_from_text(task_id, agent_text, refs, task_canvas_context)
                            query_results_for_followup.extend([res for res in tool_results if res.get("query")])
                            await _codex_agent_execute_actions_from_text(task_id, agent_text, refs, task_canvas_context)
                    if str(item.get("type") or "") == "imageGeneration":
                        await _codex_agent_add_generated_image(task_id, str(item.get("savedPath") or item.get("path") or ""), str(item.get("prompt") or ""), refs, task_canvas_context)
                if method == "turn/completed":
                    if (
                        runtime.native_tools_enabled
                        and is_batch_request
                        and batch_tool_refused
                        and not batch_create_called
                        and tool_round + 1 < max_tool_rounds
                    ):
                        pending_turn_text = (
                            "系统纠正：Infinite Canvas 已注册 infinite_canvas.create_batch_task，"
                            "且本会话可以调用 Dynamic Tools。不要让用户刷新，也不要逐个调用普通生图/视频工具。"
                            "请基于已查询到的画布素材与生成设置，规划完整的独立生成节点清单，"
                            "调用 create_batch_task。服务端会展示一次批量执行确认卡。"
                        )
                        pending_image_refs = []
                        _codex_agent_add_task_event(task_id, {
                            "method": "warning",
                            "params": {"message": "检测到批量任务能力误判，正在自动纠正并重试。"},
                        })
                        break
                    if not runtime.native_tools_enabled and query_results_for_followup and tool_round < 2:
                        pending_turn_text = _codex_agent_canvas_tool_result_text(query_results_for_followup)
                        pending_image_refs = []
                        _codex_agent_add_task_event(task_id, {
                            "method": "canvas/tool_followup",
                            "params": {"round": tool_round + 1, "count": len(query_results_for_followup)},
                        })
                        break
                    _codex_agent_set_task_status(task_id, "completed")
                    _codex_agent_add_task_event(task_id, {"method": "task/completed", "params": {"task_id": task_id}})
                    return
                if method in {"fatal", "error", "turn/timeout"}:
                    await runtime.fail_pending_dynamic_tools("本轮已超时或失败，工具调用未执行")
                    message = ""
                    if isinstance(params, dict):
                        message = str(params.get("message") or params.get("error") or "")
                    _codex_agent_set_task_status(task_id, "failed", error=message)
                    _codex_agent_add_task_event(task_id, {"method": "task/completed", "params": {"task_id": task_id, "status": "failed"}})
                    return
            else:
                break
        _codex_agent_set_task_status(task_id, "completed")
        _codex_agent_add_task_event(task_id, {"method": "task/completed", "params": {"task_id": task_id}})
    except asyncio.CancelledError:
        try:
            await runtime.fail_pending_dynamic_tools("用户停止了当前任务，工具调用未执行")
        except Exception:
            pass
        _codex_agent_set_task_status(task_id, "stopped", error="stopped by user")
        _codex_agent_add_task_event(task_id, {"method": "task/completed", "params": {"task_id": task_id, "status": "stopped"}})
        raise
    except Exception as exc:
        try:
            await runtime.fail_pending_dynamic_tools("任务异常结束，工具调用未执行")
        except Exception:
            pass
        _codex_agent_set_task_status(task_id, "failed", error=str(exc))
        _codex_agent_add_task_event(task_id, {"method": "error", "params": {"message": str(exc)}})
        _codex_agent_add_task_event(task_id, {"method": "task/completed", "params": {"task_id": task_id, "status": "failed"}})


@router.get("/api/codex-agent/commands")
async def codex_agent_commands():
    """Canvas Agent 专用斜杠命令注册表；前端不再维护第二份业务定义。"""
    return {"schema": 1, "commands": _canvas_agent_public_commands()}


@router.get("/api/codex-agent/complex-tasks")
async def complex_task_list(canvas_id: str = "", active_only: bool = False):
    return {"tasks": _complex_task_engine_required().list(canvas_id, active_only=active_only)}


@router.post("/api/codex-agent/complex-tasks")
async def complex_task_create(payload: ComplexTaskCreateRequest):
    try:
        spec = dict(payload.spec or {})
        normalized = normalize_batch_task_spec(spec, canvas_id=str(payload.canvas_id or ""))
        return {"ok": True, "task": _complex_task_engine_required().create(normalized, canvas_id=str(payload.canvas_id or ""))}
    except ComplexTaskError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/api/codex-agent/complex-tasks/{task_id}")
async def complex_task_detail(task_id: str, after: int = 0):
    try:
        return {"ok": True, "task": _complex_task_engine_required().get(task_id, after=after)}
    except ComplexTaskError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/api/codex-agent/complex-tasks/{task_id}/control")
async def complex_task_control(task_id: str, payload: ComplexTaskControlRequest):
    try:
        return {"ok": True, "task": _complex_task_engine_required().control(task_id, payload.action)}
    except ComplexTaskError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/codex-agent/board/open")
async def codex_agent_board_open(payload: CodexAgentBoardOpenRequest):
    """打开（或复用）一个 Canvas Agent Runtime。Codex thread 只是底层执行指针。"""
    project_dir = str(payload.project_dir or "").strip()
    return await _codex_agent_open_runtime(
        project_dir=project_dir,
        thread_id=str(payload.thread_id or ""),
        canvas_id=str(payload.canvas_id or ""),
        conversation_id=str(payload.conversation_id or ""),
    )


@router.post("/api/codex-agent/board/close")
async def codex_agent_board_close(payload: CodexAgentBoardCloseRequest):
    """关闭一个 Agent Runtime。没有指定 thread/conversation 时关闭该目录下的运行时。"""
    closed = await _codex_agent_close_runtime(
        project_dir=str(payload.project_dir or ""),
        canvas_id=str(payload.canvas_id or ""),
        conversation_id=str(payload.conversation_id or ""),
        thread_id=str(payload.thread_id or ""),
    )
    return {"ok": True, "closed": closed}


@router.get("/api/codex-agent/history/projects")
async def codex_agent_history_projects(canvas_id: str = "", include_hidden: bool = False):
    return {
        "projects": _codex_agent_history_projects(canvas_id, include_hidden),
        "home": str(CODEX_AGENT_HISTORY_DB),
    }


@router.get("/api/codex-agent/history/conversations")
async def codex_agent_history_conversations(project_dir: Optional[str] = None, canvas_id: str = "", include_archived: bool = False):
    return {
        "conversations": _codex_agent_history_conversations(project_dir, canvas_id, include_archived),
        "home": str(CODEX_AGENT_HISTORY_DB),
    }


@router.post("/api/codex-agent/history/project-visibility")
async def codex_agent_history_project_visibility(payload: CodexAgentProjectVisibilityRequest):
    try:
        _codex_agent_set_project_visibility(payload.canvas_id, payload.project_dir, payload.hidden)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@router.get("/api/codex-agent/workdirs/presets")
async def codex_agent_workdir_presets_get():
    return {"presets": _codex_agent_read_workdir_presets(), "home": str(CODEX_AGENT_WORKDIR_PRESETS_FILE)}


@router.post("/api/codex-agent/workdirs/presets")
async def codex_agent_workdir_presets_add(payload: CodexAgentWorkdirPresetRequest):
    path = str(payload.path or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="缺少预设路径")
    try:
        resolved = str(_Path(path).expanduser().resolve())
    except Exception:
        raise HTTPException(status_code=400, detail="预设路径格式不正确")
    if not os.path.isdir(resolved):
        raise HTTPException(status_code=400, detail=f"预设路径不存在: {resolved}")
    presets = _codex_agent_read_workdir_presets()
    if resolved not in presets:
        presets.append(resolved)
        _codex_agent_write_workdir_presets(presets)
    return {"ok": True, "presets": presets}


@router.delete("/api/codex-agent/workdirs/presets")
async def codex_agent_workdir_presets_delete(payload: CodexAgentWorkdirPresetRequest):
    path = str(payload.path or "").strip()
    try:
        resolved = str(_Path(path).expanduser().resolve()) if path else ""
    except Exception:
        resolved = path
    presets = [item for item in _codex_agent_read_workdir_presets() if item != resolved and item != path]
    _codex_agent_write_workdir_presets(presets)
    return {"ok": True, "presets": presets}


@router.get("/api/codex-agent/workdirs/children")
async def codex_agent_workdir_children(root: str = ""):
    root = str(root or "").strip()
    if not root:
        return {"children": []}
    try:
        resolved = _Path(root).expanduser().resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="根目录格式不正确")
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail=f"根目录不存在: {resolved}")
    children = []
    try:
        for child in resolved.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                children.append({"path": str(child), "name": child.name})
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"没有权限读取目录: {resolved}")
    children.sort(key=lambda item: item["name"].lower())
    return {"children": children}


@router.get("/api/codex-agent/history/conversation")
async def codex_agent_history_conversation(conversation_id: str = ""):
    if not conversation_id:
        raise HTTPException(status_code=400, detail="缺少 conversation_id")
    data = _codex_agent_history_conversation_state(conversation_id)
    return _codex_agent_panel_state_public(data)


@router.get("/api/codex-agent/history/latest")
async def codex_agent_history_latest(canvas_id: str = "", project_dir: Optional[str] = None):
    data = _codex_agent_history_latest_state(canvas_id, project_dir)
    return _codex_agent_panel_state_public(data)


@router.get("/api/codex-agent/panel-state")
async def codex_agent_panel_state_get(canvas_id: str = "", project_dir: str = "", thread_id: str = ""):
    data = _codex_agent_read_panel_state(canvas_id, project_dir, thread_id)
    return _codex_agent_panel_state_public(data)


@router.get("/api/codex-agent/panel-state/latest")
async def codex_agent_panel_state_latest(canvas_id: str = "", project_dir: Optional[str] = None):
    data = _codex_agent_latest_panel_state(canvas_id, project_dir)
    return _codex_agent_panel_state_public(data)


@router.post("/api/codex-agent/panel-state")
async def codex_agent_panel_state_save(payload: CodexAgentPanelStateRequest):
    data = _codex_agent_write_panel_state(payload)
    return {"ok": True, **_codex_agent_panel_state_public(data)}


@router.post("/api/codex-agent/preferences/remember")
async def codex_agent_preferences_remember(payload: CodexAgentPreferenceRequest):
    try:
        path = _codex_agent_append_preference(payload.note)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "path": path}


@router.post("/api/codex-agent/turn")
async def codex_agent_turn(payload: CodexAgentTurnRequest):
    """
    发送 user message 到 Codex session，事件流以 SSE 推回。
    每个 event 是 {"method": "item/started" | "item/updated" | "item/completed" | "turn/completed" | "error", "params": {...}}
    最终一条：{"method": "done"}
    """
    runtime = await _codex_agent_runtime_for_payload(payload)

    # 处理 attachments：把所有形式（file:// / 本地路径 / HTTP URL / data URL）转成
    # Codex app-server 稳定支持的本地 localImage 路径。
    refs: List[Dict[str, Any]] = []
    ref_errors: List[str] = []
    if payload.attachments:
        async with httpx.AsyncClient() as client:
            for item in payload.attachments:
                try:
                    refs.append(await _codex_agent_prepare_attachment(item, _codex_agent_effective_project_dir(payload.project_dir), client))
                except Exception as e:
                    ref_errors.append(f"{item}: {e}")

    image_refs = [ref for ref in refs if str(ref.get("kind") or "image").lower() == "image"]
    direct_canvas_context = dict(payload.canvas_context or {}) if isinstance(payload.canvas_context, dict) else {}
    direct_canvas_context["_native_tools_enabled"] = runtime.native_tools_enabled
    context_text = _codex_agent_build_turn_context_text(payload.project_dir, refs, direct_canvas_context, payload)
    turn_text = f"{context_text}\n\n用户请求：\n{payload.text}" if context_text else payload.text
    canvas_id = _codex_agent_canvas_id_from_payload(payload)

    async def event_stream():
        if runtime.resume_warning:
            data = json.dumps({"method": "warning", "params": {"message": runtime.resume_warning, "thread_id": runtime.thread_id}}, ensure_ascii=False)
            yield f"data: {data}\n\n"
        for err in ref_errors:
            data = json.dumps({"method": "error", "params": {"message": f"ref download failed: {err}"}}, ensure_ascii=False)
            yield f"data: {data}\n\n"
        try:
            async for event in runtime.send_user_message(turn_text, image_refs):
                if str(event.get("method") or "") == "item/tool/call" and runtime.native_tools_enabled:
                    native_result = await _codex_agent_execute_direct_native_tool_call(runtime, event, canvas_id, refs, direct_canvas_context)
                    yield "data: " + json.dumps({"method": "canvas/tool_call", "params": {"tool": native_result.get("tool"), "query": native_result.get("query"), "native": True}}, ensure_ascii=False) + "\n\n"
                    yield "data: " + json.dumps({"method": "canvas/tool_result", "params": {**native_result, "native": True}}, ensure_ascii=False) + "\n\n"
                    continue
                data = json.dumps(event, ensure_ascii=False)
                yield f"data: {data}\n\n"
        except Exception as e:
            if isinstance(e, HTTPException) and e.status_code == 504:
                await _codex_agent_close_runtime(
                    project_dir=str(payload.project_dir or ""),
                    canvas_id=_codex_agent_canvas_id_from_payload(payload),
                    conversation_id=str(payload.conversation_id or ""),
                    thread_id=str(payload.thread_id or ""),
                )
            err = {"method": "error", "params": {"message": str(e)}}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        finally:
            yield "data: {\"method\":\"done\"}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/api/codex-agent/turn/background")
async def codex_agent_turn_background(payload: CodexAgentTurnRequest):
    """
    后台托管 Agent turn。页面刷新/关闭后，只要服务进程还在，任务会继续运行；
    前端通过 /turn/status 轮询事件日志恢复展示。
    """
    runtime = await _codex_agent_runtime_for_payload(payload)
    canvas_id = _codex_agent_canvas_id_from_payload(payload)
    task_id = uuid.uuid4().hex
    task: Dict[str, Any] = {
        "task_id": task_id,
        "project_dir": payload.project_dir,
        "canvas_id": canvas_id,
        "thread_id": runtime.thread_id,
        "conversation_id": str(payload.conversation_id or ""),
        "runtime_key": runtime.runtime_key,
        "status": "queued",
        "created_at": _codex_agent_now(),
        "updated_at": _codex_agent_now(),
        "events": [],
        "summary": {},
        "executed_action_keys": set(),
    }
    with _codex_agent_task_lock:
        _codex_agent_tasks[task_id] = task
    bg_task = asyncio.create_task(_codex_agent_run_background_task(task_id, payload))
    with _codex_agent_task_lock:
        _codex_agent_tasks[task_id]["asyncio_task"] = bg_task
    return _codex_agent_task_public(task)


@router.get("/api/codex-agent/turn/status")
async def codex_agent_turn_status(task_id: str, after: int = 0):
    with _codex_agent_task_lock:
        task = _codex_agent_tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Agent task not found")
        return _codex_agent_task_public(task, after)


@router.get("/api/codex-agent/turn/active")
async def codex_agent_turn_active(project_dir: Optional[str] = None, canvas_id: str = "", conversation_id: str = ""):
    with _codex_agent_task_lock:
        tasks = list(_codex_agent_tasks.values())
        tasks = [
            task for task in tasks
            if (project_dir is None or task.get("project_dir") == str(project_dir or ""))
            and (not canvas_id or task.get("canvas_id") == canvas_id)
            and (not conversation_id or task.get("conversation_id") == conversation_id)
            and task.get("status") in {"queued", "running"}
        ]
        tasks.sort(key=lambda task: int(task.get("updated_at") or task.get("created_at") or 0), reverse=True)
        if not tasks:
            return {"task": None}
        return {"task": _codex_agent_task_public(tasks[0], 0)}


@router.post("/api/codex-agent/turn/stop")
async def codex_agent_turn_stop(payload: CodexAgentTaskStatusRequest):
    with _codex_agent_task_lock:
        task = _codex_agent_tasks.get(payload.task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Agent task not found")
        async_task = task.get("asyncio_task")
        project_dir = task.get("project_dir", "")
        canvas_id = task.get("canvas_id", "")
        conversation_id = task.get("conversation_id", "")
        thread_id = task.get("thread_id", "")
    if async_task and not async_task.done():
        async_task.cancel()
    await _codex_agent_close_runtime(
        project_dir=str(project_dir or ""),
        canvas_id=str(canvas_id or ""),
        conversation_id=str(conversation_id or ""),
        thread_id=str(thread_id or ""),
    )
    _codex_agent_set_task_status(payload.task_id, "stopped", error="stopped by user")
    _codex_agent_add_task_event(payload.task_id, {"method": "task/completed", "params": {"task_id": payload.task_id, "status": "stopped"}})
    return {"ok": True, "task": _codex_agent_task_public(_codex_agent_tasks[payload.task_id])}


@router.post("/api/codex-agent/action/resolve")
async def codex_agent_action_resolve(payload: CodexAgentActionResolveRequest):
    decision = str(payload.decision or "approve").strip().lower()
    with _codex_agent_task_lock:
        task = _codex_agent_tasks.get(payload.task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Agent task not found")
        pending = task.setdefault("pending_action_approvals", {}).get(payload.approval_id)
        if not pending:
            raise HTTPException(status_code=404, detail="待确认动作不存在或已处理")
        if pending.get("status") != "pending":
            raise HTTPException(status_code=409, detail="待确认动作已处理")
        if task.get("status") not in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="当前任务已结束，不能再执行待确认动作")
        pending["status"] = "resolving" if decision in {"approve", "run", "execute", "run_generation", "create_nodes"} else "skipped"
        canvas_id = task.get("canvas_id", "")
        runtime_key = str(task.get("runtime_key") or "")

    native_tool = pending.get("kind") in {"native_tool", "generation_action", "batch_task"}
    generation_action = pending.get("kind") == "generation_action"
    batch_task_action = pending.get("kind") == "batch_task"
    runtime = None
    if native_tool:
        with _codex_agent_lock:
            runtime = _codex_agent_sessions.get(runtime_key)
        if not runtime:
            with _codex_agent_task_lock:
                task = _codex_agent_tasks.get(payload.task_id)
                stored = task.get("pending_action_approvals", {}).get(payload.approval_id) if task else None
                if isinstance(stored, dict):
                    stored["status"] = "pending"
            raise HTTPException(status_code=409, detail="原生工具会话已结束，不能继续执行")

    if decision not in {"approve", "run", "execute", "run_generation", "create_nodes"}:
        result = {
            "ok": True,
            "approval_id": payload.approval_id,
            "tool": pending.get("tool") or "",
            "changed": 0,
            "skipped": len(pending.get("actions") or []),
            "results": [],
            "message": "用户跳过了待确认画布动作",
        }
        if native_tool:
            await runtime.respond_dynamic_tool_call(pending.get("request_id"), _codex_agent_dynamic_tool_response({**result, "ok": False}, success=False))
        _codex_agent_add_task_event(payload.task_id, {
            "method": "canvas/action_result",
            "params": result,
        })
        return {"ok": True, "decision": "skipped", "result": result}

    if batch_task_action:
        args = dict(pending.get("args") or {}) if isinstance(pending.get("args"), dict) else {}
        result = await _codex_agent_run_canvas_tool(CodexAgentCanvasToolRequest(
            canvas_id=str(canvas_id or pending.get("canvas_id") or ""), tool="create_batch_task", args=args,
            refs=[], canvas_context={},
        ))
    elif generation_action and decision in {"approve", "run", "execute", "run_generation"}:
        generation = pending.get("generation") if isinstance(pending.get("generation"), dict) else {}
        result = await _codex_agent_submit_generation(
            str(canvas_id or ""), generation,
            pending.get("refs") if isinstance(pending.get("refs"), list) else [],
            pending.get("canvas_context") if isinstance(pending.get("canvas_context"), dict) else {},
        )
        if result.get("ok"):
            with _codex_agent_task_lock:
                current_task = _codex_agent_tasks.get(payload.task_id)
                if current_task is not None:
                    current_task["native_generation_submitted"] = True
    else:
        result = await _codex_agent_apply_canvas_actions(
            str(canvas_id or ""),
            pending.get("actions") if isinstance(pending.get("actions"), list) else [],
            pending.get("refs") if isinstance(pending.get("refs"), list) else [],
            pending.get("canvas_context") if isinstance(pending.get("canvas_context"), dict) else {},
        )
    with _codex_agent_task_lock:
        task = _codex_agent_tasks.get(payload.task_id)
        if task:
            stored = task.setdefault("pending_action_approvals", {}).get(payload.approval_id)
            if stored:
                stored["status"] = "approved"
                stored["resolved_at"] = _codex_agent_now()
    event_result = dict(result)
    event_result["approval_id"] = payload.approval_id
    if pending.get("tool"):
        event_result["tool"] = pending.get("tool")
    if native_tool:
        responded = await runtime.respond_dynamic_tool_call(pending.get("request_id"), _codex_agent_dynamic_tool_response(event_result))
        if not responded:
            event_result["response_warning"] = "原生工具调用已结束，结果未能回传给 Agent"
    _codex_agent_add_task_event(payload.task_id, {"method": "canvas/action_result", "params": event_result})
    return {"ok": bool(event_result.get("ok", True)), "decision": "approved", "result": event_result}


@router.post("/api/codex-agent/tools/canvas")
async def codex_agent_canvas_tool(payload: CodexAgentCanvasToolRequest):
    """
    Internal Canvas Tools bridge.
    v1 exposes structured query tools and low-risk canvas operations while the model-facing path
    still uses canvas_agent_action as a compatibility protocol.
    """
    return await _codex_agent_run_canvas_tool(payload)


@router.get("/api/codex-agent/threads/replay")
async def codex_agent_threads_replay(session_id: str = ""):
    """
    从 ~/.codex/sessions/ 读历史 jsonl，回放为消息列表（用于切换历史会话时显示）。
    session_id 可以是：
      - 完整 UUID（最常见）
      - 部分字符串（会做包含匹配）
    返回：{ messages: [{role, blocks: [...]}, ...], session_id, rollout_path }
    """
    if not session_id:
        raise HTTPException(status_code=400, detail="缺少 session_id")

    sd = CODEX_AGENT_HOME / "sessions"
    if not sd.exists():
        return {"messages": [], "session_id": session_id, "rollout_path": ""}

    # 找匹配的文件
    target = None
    needle = session_id.strip()
    for path in sd.rglob("*.jsonl"):
        if needle in path.stem:
            target = path
            break

    if not target:
        return {"messages": [], "session_id": session_id, "rollout_path": "",
                "note": "no matching rollout file found"}

    messages: List[Dict[str, Any]] = []
    current_bot: Optional[Dict[str, Any]] = None
    session_meta_id = ""

    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                ptype = str(obj.get("type", ""))
                payload = obj.get("payload", {}) or {}

                if ptype == "session_meta":
                    session_meta_id = str(payload.get("session_id", ""))

                elif ptype == "response_item":
                    rp = payload.get("payload", payload)  # 兼容嵌套
                    role = str(rp.get("role", ""))
                    content = rp.get("content", [])
                    if role == "user" and isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "input_text":
                                txt = str(c.get("text", ""))
                                if txt and not _codex_agent_is_internal_context_text(txt):
                                    clean_txt, refs = _codex_agent_parse_ref_context(txt)
                                    blocks = []
                                    if refs:
                                        blocks.append({"type": "attach", "items": refs})
                                    if clean_txt:
                                        blocks.append({"type": "text", "text": clean_txt})
                                    if blocks:
                                        messages.append({"role": "user", "blocks": blocks})
                                    current_bot = None
                                    break
                    # 注意：assistant role 的 response_item 里也有 output_text，
                    # 但我们改用 event_msg.agent_message 解析（更精确），避免重复。

                elif ptype == "event_msg":
                    etype = str(payload.get("type", ""))
                    if etype == "user_message":
                        txt = str(payload.get("message") or "")
                        if _codex_agent_is_internal_context_text(txt):
                            continue
                        clean_txt, refs = _codex_agent_parse_ref_context(txt)
                        if clean_txt or refs:
                            if messages and messages[-1].get("role") == "user":
                                prev_text = ""
                                for b in messages[-1].get("blocks") or []:
                                    if b.get("type") == "text":
                                        prev_text = str(b.get("text") or "")
                                        break
                                if prev_text == clean_txt:
                                    continue
                            blocks = []
                            if refs:
                                blocks.append({"type": "attach", "items": refs})
                            if clean_txt:
                                blocks.append({"type": "text", "text": clean_txt})
                            messages.append({"role": "user", "blocks": blocks})
                            current_bot = None
                    elif etype == "agent_message":
                        txt = str(payload.get("message") or payload.get("text") or "")
                        txt = _codex_agent_strip_agent_markup(txt)
                        if txt:
                            if current_bot is None:
                                current_bot = {"role": "bot", "blocks": []}
                                messages.append(current_bot)
                            current_bot["blocks"].append({"type": "text", "text": txt})
                    elif etype in ("agent_reasoning", "agent_reasoning_section_break"):
                        txt = str(payload.get("text") or "")
                        txt = _codex_agent_strip_agent_markup(txt)
                        if txt:
                            if current_bot is None:
                                current_bot = {"role": "bot", "blocks": []}
                                messages.append(current_bot)
                            current_bot["blocks"].append({"type": "thinking", "text": txt})
    except Exception as e:
        return {"messages": messages, "session_id": session_meta_id,
                "rollout_path": str(target), "error": str(e)}

    return {"messages": messages, "session_id": session_meta_id, "rollout_path": str(target)}


def _codex_cli_resolve() -> Dict[str, Any]:
    """检测 codex CLI 是否安装 + 版本。"""
    info: Dict[str, Any] = {"found": False, "path": "", "version": ""}
    cli = codex_cli_executable()
    if not cli:
        return info
    info["found"] = True
    info["path"] = cli
    try:
        out = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=5)
        ver_text = (out.stdout or out.stderr or "").strip()
        info["version"] = ver_text.split("\n")[0] if ver_text else ""
    except Exception as e:
        info["version"] = f"(检测失败: {e})"
    return info


@router.get("/api/codex-agent/status")
async def codex_agent_status():
    """
    检测 Codex CLI 状态 + home 目录。
    返回：
      {
        found, path, version,           # CLI 状态
        home, home_exists,                # Codex home
        auth_file_exists,                 # 登录态（auth.json）
        sessions_dir_exists,              # 会话存档目录
        skills_dir_exists,                # 系统 skills 目录
        sessions_count_hint,              # ~/.codex/sessions/ 下 jsonl 总数（粗估）
        active_sessions,                  # 当前激活的 app-server 进程列表
      }
    """
    cli = _codex_cli_resolve()
    home = CODEX_AGENT_HOME
    auth_file = home / "auth.json"
    sessions_dir = home / "sessions"
    skills_dir = home / "skills"
    return {
        **cli,
        "home": str(home),
        "home_exists": home.exists(),
        "auth_file_exists": auth_file.exists(),
        "sessions_dir_exists": sessions_dir.exists(),
        "skills_dir_exists": skills_dir.exists(),
        "sessions_count_hint": _codex_agent_sessions_count_quick(),
        "runtime_env": _codex_agent_env_summary(),
        "active_sessions": [
            {
                "runtime_key": key,
                "project_dir": runtime.project_dir,
                "thread_id": runtime.thread_id,
            }
            for key, runtime in _codex_agent_sessions.items()
        ],
    }


def _codex_agent_sessions_count_quick() -> int:
    """粗估 ~/.codex/sessions/ 下 jsonl 文件总数。"""
    sd = CODEX_AGENT_HOME / "sessions"
    if not sd.exists():
        return 0
    try:
        return sum(1 for _ in sd.rglob("*.jsonl"))
    except Exception:
        return -1


@router.get("/api/codex-agent/sessions/list")
async def codex_agent_sessions_list(project_dir: str = ""):
    """
    扫 ~/.codex/sessions/，按 cwd（项目目录）分组列出所有 session。
    返回：
      {
        by_project: { cwd: [ {session_id, started_at, cwd, model, preview, rollout_path}, ... ] },
        total: int,
        home: str,
      }
    """
    sd = CODEX_AGENT_HOME / "sessions"
    by_project: Dict[str, list] = {}
    total = 0

    if sd.exists():
        try:
            for path in sd.rglob("*.jsonl"):
                total += 1
                try:
                    meta = _codex_session_meta_quick(path)
                except Exception:
                    continue
                meta["source"] = meta.get("source") or "codex"
                cwd = meta.get("cwd") or "(unknown)"
                if project_dir and cwd != project_dir:
                    continue
                by_project.setdefault(cwd, []).append(meta)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"扫描会话失败: {e}")

    seen_panel_keys = set()
    for meta in _codex_agent_panel_state_metas(project_dir):
        cwd = meta.get("cwd") or "(unknown)"
        panel_key = meta.get("panel_state_key") or f"{cwd}:{meta.get('session_id')}"
        if panel_key in seen_panel_keys:
            continue
        seen_panel_keys.add(panel_key)
        existing = by_project.setdefault(cwd, [])
        same_thread = next((item for item in existing if item.get("session_id") and item.get("session_id") == meta.get("session_id")), None)
        if same_thread:
            same_thread["panel_state_path"] = meta.get("panel_state_path", "")
            same_thread["canvas_id"] = meta.get("canvas_id", "")
            same_thread["source"] = "codex+panel"
            if meta.get("updated_at"):
                same_thread["updated_at"] = meta.get("updated_at")
        else:
            existing.append(meta)

    for k in by_project:
        by_project[k].sort(key=lambda x: (int(x.get("updated_at") or 0), str(x.get("started_at") or "")), reverse=True)

    return {"by_project": by_project, "total": total, "home": str(sd), "panel_home": str(CODEX_AGENT_PANEL_STATE_DIR)}


def _codex_session_meta_quick(path: _Path) -> Dict[str, Any]:
    """
    快速读一个 rollout jsonl 提取元数据（cwd / model / preview）。
    完整解析由阶段 2/3 的 thread/resume 流程处理。
    """
    meta: Dict[str, Any] = {
        "session_id": "",
        "rollout_path": str(path),
        "started_at": "",
        "cwd": "",
        "model": "",
        "preview": "",
        "preview_media": "",
    }

    # 文件名格式: rollout-2026-07-04T21-05-37-019f2d3c-...jsonl
    # 文件名中冒号不能用作时间分隔，所以是横线 "21-05-37"，需要转成冒号 "21:05:37"
    name = path.stem
    if name.startswith("rollout-"):
        iso_part = name[len("rollout-"):]
        if len(iso_part) >= 19:
            date_part = iso_part[:10]               # 2026-07-04
            time_part = iso_part[11:19].replace("-", ":")  # 21-05-37 → 21:05:37
            meta["started_at"] = f"{date_part} {time_part}Z"

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 50:  # 只扫前 50 行
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                ptype = obj.get("type", "")
                payload = obj.get("payload", {}) or {}

                if ptype == "session_meta" and not meta["session_id"]:
                    meta["session_id"] = payload.get("session_id", "")

                if ptype == "turn_context" and not meta["cwd"]:
                    meta["cwd"] = payload.get("cwd", "")
                    meta["model"] = payload.get("model", "")

                if ptype == "response_item" and not meta["preview"]:
                    role = payload.get("role", "")
                    content = payload.get("content", [])
                    if role == "user" and isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "input_text":
                                txt = c.get("text", "")
                                clean_txt, refs = _codex_agent_parse_ref_context(txt)
                                clean_txt = re.sub(r"\s+", " ", clean_txt).strip()
                                if clean_txt:
                                    meta["preview"] = clean_txt[:120]
                                    break
                                if refs and not meta["preview_media"]:
                                    names = [str(ref.get("name") or "").strip() for ref in refs[:2] if ref.get("name")]
                                    meta["preview_media"] = "、".join(names) if names else "含图片/附件的对话"
                            elif isinstance(c, dict) and not meta["preview_media"]:
                                ctype = str(c.get("type") or "").lower()
                                if any(key in ctype for key in ("image", "video", "file", "attachment")):
                                    raw_name = c.get("name") or c.get("filename") or c.get("path") or c.get("url") or ""
                                    name = os.path.basename(str(raw_name)) if raw_name else ""
                                    meta["preview_media"] = name or "含图片/附件的对话"
    except Exception:
        pass

    if not meta["preview"] and meta.get("preview_media"):
        meta["preview"] = meta["preview_media"]

    return meta


@router.get("/api/codex-agent/file/view")
async def codex_agent_file_view(path: str):
    """
    暴露本地文件给前端（用于显示 Codex 生成的图）。
    安全防护：
      1. 路径必须存在且是文件
      2. 文件大小 ≤ 20MB
      3. mime 必须在白名单（图片 / 文本 / 视频）
    """
    if not path:
        raise HTTPException(status_code=400, detail="缺少 path 参数")

    try:
        p = _Path(path).resolve()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"路径非法: {e}")

    if not p.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
    if not p.is_file():
        raise HTTPException(status_code=400, detail="不是文件")

    try:
        size = p.stat().st_size
    except OSError:
        raise HTTPException(status_code=400, detail="无法读取文件信息")
    if size > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件过大（>20MB）")

    mime, _ = mimetypes.guess_type(str(p))
    allowed_prefixes = ("image/", "text/", "video/")
    if mime and not any(mime.startswith(pfx) for pfx in allowed_prefixes):
        raise HTTPException(status_code=415, detail=f"不支持的文件类型: {mime}")

    return FileResponse(str(p))


# ============================================================
