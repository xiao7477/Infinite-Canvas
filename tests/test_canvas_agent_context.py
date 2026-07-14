import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.canvas_skills import active_skill_context, normalize_context_profile, public_commands
from agent.context import build_minimal_context_envelope
from agent.revision import CanvasRevisionConflict, CanvasRevisionStore


class CanvasAgentSkillRoutingTests(unittest.TestCase):
    def test_public_commands_use_canonical_batch_name_with_legacy_alias(self):
        batch = next(item for item in public_commands() if item["id"] == "batch")
        self.assertEqual(batch["command"], "/批量任务")
        self.assertIn("/批量处理", batch["aliases"])
        self.assertFalse(batch["task_node_available"])

    def test_command_registry_overrides_frontend_guess(self):
        profile = normalize_context_profile(
            "/总结画布 帮我看一下",
            {"level": 0, "intent": "chat", "command": "/总结画布"},
            0,
        )
        self.assertEqual(profile["level"], 3)
        self.assertEqual(profile["intent"], "global_canvas")
        self.assertEqual(profile["command_id"], "summarize")

    def test_only_matching_canvas_skill_is_loaded(self):
        skill = active_skill_context("把选中素材重命名", {"intent": "canvas_operation"})
        self.assertEqual(skill["id"], "infinite-canvas-asset-naming")
        self.assertIn("rename_assets", skill["instructions"])
        self.assertNotIn("generate_videos", skill["instructions"])

    def test_vague_organize_skill_requires_layout_advice_before_write(self):
        skill = active_skill_context("/整理 整理一下选中的节点", {"intent": "canvas_operation", "command": "/整理"})
        self.assertEqual(skill["id"], "infinite-canvas-organize")
        self.assertIn("必须先调用 `get_layout_context`", skill["instructions"])
        self.assertIn("只提出建议，不调用写工具", skill["instructions"])
        self.assertIn("用一个简短问题确认", skill["instructions"])


class CanvasAgentEnvelopeTests(unittest.TestCase):
    def test_plain_chat_excludes_preferences_and_tool_catalog(self):
        text = build_minimal_context_envelope(
            metadata={"project_dir": "/tmp/project", "context_level": 0, "context_intent": "chat"},
            refs=[],
            preferences="- 默认使用 16:9",
            skill={"id": "", "instructions": ""},
        )
        self.assertIn("app_server_minimal_envelope_v2", text)
        self.assertNotIn("默认使用 16:9", text)
        self.assertNotIn("可用操作工具", text)
        self.assertNotIn("available_image_providers", text)

    def test_envelope_includes_canvas_revision(self):
        text = build_minimal_context_envelope(
            metadata={"project_dir": "/tmp/project", "context_level": 1, "context_intent": "canvas_operation", "canvas_revision": 7},
            refs=[],
        )
        self.assertIn("canvas_revision: 7", text)

    def test_generation_skill_is_injected_without_provider_dump(self):
        skill = active_skill_context("生一张图", {"intent": "generation"})
        text = build_minimal_context_envelope(
            metadata={"project_dir": "/tmp/project", "context_level": 2, "context_intent": "generation"},
            refs=[{"ref_id": "ref_1", "name": "reference.png", "kind": "image", "local_path": "/tmp/reference.png"}],
            preferences="- 默认使用 16:9",
            skill=skill,
        )
        self.assertIn("active_canvas_skill: infinite-canvas-generation", text)
        self.assertIn("get_generation_settings", text)
        self.assertIn("ref_1", text)
        self.assertIn("默认使用 16:9", text)
        self.assertNotIn("available_image_providers", text)


class CanvasRevisionStoreTests(unittest.TestCase):
    def test_observe_detects_structural_changes_but_ignores_viewport(self):
        with TemporaryDirectory() as tmp:
            store = CanvasRevisionStore(Path(tmp))
            canvas = {"id": "c1", "kind": "smart", "nodes": [], "connections": [], "viewport": {"x": 0}}
            self.assertEqual(store.observe("c1", canvas)["revision"], 0)
            canvas["viewport"] = {"x": 100}
            self.assertEqual(store.observe("c1", canvas)["revision"], 0)
            canvas["nodes"].append({"id": "n1", "type": "smart-image", "x": 0, "y": 0})
            self.assertEqual(store.observe("c1", canvas)["revision"], 1)

    def test_expected_revision_conflict_is_structured(self):
        with TemporaryDirectory() as tmp:
            store = CanvasRevisionStore(Path(tmp))
            canvas = {"id": "c1", "kind": "smart", "nodes": [], "connections": []}
            store.observe("c1", canvas)
            canvas["nodes"].append({"id": "n1"})
            with self.assertRaises(CanvasRevisionConflict) as caught:
                store.assert_expected("c1", canvas, 0)
            self.assertEqual(caught.exception.expected, 0)
            self.assertEqual(caught.exception.current, 1)


if __name__ == "__main__":
    unittest.main()
