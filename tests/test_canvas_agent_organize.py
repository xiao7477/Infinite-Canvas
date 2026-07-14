import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import agent.backend as backend


class _RevisionStore:
    def observe(self, canvas_id, canvas):
        return {"revision": 0}

    def assert_expected(self, canvas_id, canvas, expected):
        return None


def image_node(node_id, x, y, width, height, natural_w, natural_h):
    return {
        "id": node_id,
        "type": "smart-image",
        "x": x,
        "y": y,
        "w": width,
        "h": height,
        "scale": 1,
        "images": [{"url": f"/output/{node_id}.png", "natural_w": natural_w, "natural_h": natural_h}],
    }


class CanvasAgentOrganizeGeometryTests(unittest.TestCase):
    def test_registry_exposes_v3_organize_tools(self):
        registry = backend._codex_agent_canvas_tool_registry()
        self.assertEqual(backend.CODEX_AGENT_DYNAMIC_TOOL_REGISTRY_VERSION, 3)
        for tool in ("get_layout_context", "get_node_tree", "resize_nodes", "arrange_node_tree"):
            self.assertIn(tool, registry)

    def test_standard_size_rules_and_non_media_reset(self):
        landscape = image_node("landscape", 0, 0, 100, 100, 1600, 900)
        portrait = image_node("portrait", 0, 0, 100, 100, 900, 1600)
        square = image_node("square", 0, 0, 100, 100, 1024, 1024)
        prompt = {"id": "prompt", "type": "smart-prompt", "x": 0, "y": 0, "w": 700, "h": 600, "text": "hello"}
        backend._codex_agent_apply_size_policy(landscape, {"width": 100, "height": 100}, "standard", "reset")
        backend._codex_agent_apply_size_policy(portrait, {"width": 100, "height": 100}, "standard", "reset")
        backend._codex_agent_apply_size_policy(square, {"width": 100, "height": 100}, "standard", "reset")
        backend._codex_agent_apply_size_policy(prompt, {"width": 700, "height": 600}, "standard", "reset")
        self.assertEqual((landscape["w"], landscape["h"]), (520, 292))
        self.assertEqual((portrait["w"], portrait["h"]), (248, 440))
        self.assertEqual((square["w"], square["h"]), (440, 440))
        self.assertNotIn("w", prompt)
        self.assertNotIn("h", prompt)

    def test_multi_media_resets_to_default_grid(self):
        node = image_node("multi", 0, 0, 900, 700, 1024, 1024)
        node["images"].append({"url": "/output/second.png", "natural_w": 1024, "natural_h": 1024})
        backend._codex_agent_apply_size_policy(node, {"width": 900, "height": 700}, "standard", "reset")
        self.assertNotIn("w", node)
        self.assertNotIn("h", node)
        self.assertEqual(node["scale"], 0.8)
        rect = backend._codex_agent_node_rect(node)
        self.assertEqual((rect["width"], rect["height"]), (406, 211))

    def test_default_media_rect_matches_frontend_layout_rules(self):
        landscape = image_node("landscape", 0, 0, 100, 100, 1600, 900)
        portrait = image_node("portrait", 0, 0, 100, 100, 900, 1600)
        square = image_node("square", 0, 0, 100, 100, 1024, 1024)
        for node in (landscape, portrait, square):
            node.pop("w")
            node.pop("h")
            node["scale"] = 2
        self.assertEqual((backend._codex_agent_node_rect(landscape)["width"], backend._codex_agent_node_rect(landscape)["height"]), (520, 292))
        self.assertEqual((backend._codex_agent_node_rect(portrait)["width"], backend._codex_agent_node_rect(portrait)["height"]), (248, 440))
        self.assertEqual((backend._codex_agent_node_rect(square)["width"], backend._codex_agent_node_rect(square)["height"]), (440, 440))

    def test_grid_uses_real_column_and_row_extents(self):
        nodes = [
            image_node("a", 0, 0, 520, 292, 1600, 900),
            image_node("b", 0, 100, 248, 440, 900, 1600),
            image_node("c", 0, 200, 440, 440, 1024, 1024),
        ]
        changes, bounds = backend._codex_agent_layout_positions(nodes, {}, "grid", {"cols": 2, "gapX": 80, "gapY": 54})
        positions = {node["id"]: (x, y) for node, x, y in changes}
        self.assertEqual(positions["a"], (0, 74))
        self.assertEqual(positions["b"], (600, 0))
        self.assertEqual(positions["c"], (40, 494))
        self.assertEqual(bounds["width"], 848)
        self.assertEqual(bounds["height"], 934)

    def test_explicit_layout_preserves_upstream_reading_order(self):
        nodes = [
            image_node("down", 0, 0, 200, 120, 1600, 900),
            image_node("up", 0, 200, 200, 120, 1600, 900),
            image_node("middle", 0, 400, 200, 120, 1600, 900),
        ]
        connections = [{"from": "up", "to": "middle"}, {"from": "middle", "to": "down"}]
        changes, _ = backend._codex_agent_layout_positions(nodes, {}, "horizontal", {}, connections)
        self.assertEqual([node["id"] for node, _, _ in changes], ["up", "middle", "down"])

    def test_tree_collects_both_directions_and_handles_cycle(self):
        nodes = [{"id": item} for item in "abcde"]
        connections = [
            {"from": "a", "to": "b"},
            {"from": "b", "to": "c"},
            {"from": "c", "to": "b"},
            {"from": "b", "to": "d"},
        ]
        tree = backend._codex_agent_tree_data("b", nodes, connections, "both")
        self.assertEqual(tree["ids"], {"a", "b", "c", "d"})
        self.assertEqual(tree["levels"]["a"], -1)
        self.assertEqual(tree["levels"]["b"], 0)
        self.assertEqual(tree["levels"]["c"], 0)
        self.assertEqual(tree["levels"]["d"], 1)

    def test_tree_nearby_placement_uses_node_shapes_instead_of_solid_outer_block(self):
        up = image_node("up", -2400, 0, 200, 120, 1600, 900)
        root = image_node("root", 0, 0, 200, 120, 1600, 900)
        down = image_node("down", 2400, 0, 200, 120, 1600, 900)
        obstacle = image_node("obstacle", 260, 260, 120, 120, 1600, 900)
        nodes = [up, root, down, obstacle]
        relative, bounds = backend._codex_agent_tree_layout(
            [up, root, down], {"up": -1, "root": 0, "down": 1}, {}, {}
        )
        origin = backend._codex_agent_find_nearby_tree_origin(
            nodes, ["up", "root", "down"], relative, bounds, "root", {},
            {"native": {"visibleWorld": {"x": -400, "y": -300, "width": 800, "height": 600}}}, {},
        )
        self.assertIsNotNone(origin)
        placed = {
            node["id"]: {
                "x": origin["x"] + x - bounds["x"],
                "y": origin["y"] + y - bounds["y"],
                "width": node["w"],
                "height": node["h"],
            }
            for node, x, y in relative
        }
        obstacle_rect = backend._codex_agent_node_rect(obstacle)
        self.assertTrue(all(not backend._codex_agent_rect_intersects(rect, obstacle_rect, 24) for rect in placed.values()))
        self.assertLess(abs(placed["root"]["x"] - root["x"]), 1000)

    def test_tree_merge_uses_longest_path_layer(self):
        nodes = [{"id": item} for item in ("root", "a", "b", "c", "d")]
        connections = [
            {"from": "root", "to": "a"},
            {"from": "root", "to": "b"},
            {"from": "a", "to": "c"},
            {"from": "b", "to": "d"},
            {"from": "d", "to": "c"},
        ]
        tree = backend._codex_agent_tree_data("root", nodes, connections, "downstream")
        self.assertEqual(tree["levels"]["c"], 3)

    def test_completed_media_merge_keeps_remote_layout_and_local_completion_timer(self):
        source = (Path(__file__).parents[1] / "static/js/smart-canvas.js").read_text(encoding="utf-8")

        def function_block(name, next_name):
            start = source.index(f"function {name}")
            end = source.index(f"\nfunction {next_name}", start)
            return source[start:end]

        complete_fn = function_block("completeSmartNodeWithImages", "syncRunButtonState")
        merge_fn = function_block("mergeSmartNode(local, remote)", "mergeSmartNodeLists")
        script = f"""
function smartNodeHasDisplayResult(node) {{ return Boolean((node?.images || []).some(item => item?.url)); }}
function smartNodeHasCompletedResult(node) {{ return smartNodeHasDisplayResult(node) && !node?.pending && !node?.queued; }}
function smartNodeInFlight(node) {{ return Boolean(node?.running || node?.pending || node?.queued); }}
function markSmartNodeComplete(node) {{ node.running=false; node.pending=0; node.queued=false; return node; }}
function mergeSmartImageLists(local, remote) {{
  const byUrl = new Map();
  [...(local || []), ...(remote || [])].forEach(item => byUrl.set(item.url, item));
  return [...byUrl.values()];
}}
{complete_fn}
{merge_fn}
const local={{id:'media',x:10,y:20,w:100,h:100,images:[{{url:'/output/a.png'}}],runStartedAt:100,runFinishedAt:200,runElapsedMs:100,runTimerHidden:true}};
const remote={{id:'media',x:400,y:500,w:520,h:292,images:[{{url:'/output/a.png'}}]}};
process.stdout.write(JSON.stringify(mergeSmartNode(local, remote)));
"""
        completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
        merged = json.loads(completed.stdout)
        self.assertEqual((merged["x"], merged["y"], merged["w"], merged["h"]), (400, 500, 520, 292))
        self.assertEqual(merged["runFinishedAt"], 200)
        self.assertEqual(merged["runElapsedMs"], 100)
        self.assertTrue(merged["runTimerHidden"])

    def test_layout_context_reports_variation_and_available_sides(self):
        nodes = [
            image_node("a", 0, 0, 520, 292, 1600, 900),
            image_node("b", 0, 400, 200, 356, 900, 1600),
            image_node("near", 650, 0, 200, 200, 1024, 1024),
        ]
        payload = backend.CodexAgentCanvasToolRequest(
            canvas_id="canvas",
            tool="get_layout_context",
            args={"scope": "selected"},
            canvas_context={
                "native": {
                    "selectedNodeIds": ["a", "b"],
                    "visibleWorld": {"x": -100, "y": -100, "width": 1400, "height": 1000},
                    "allNodes": [{"id": node["id"], "x": node["x"], "y": node["y"], "width": node["w"], "height": node["h"]} for node in nodes],
                }
            },
        )
        with patch.object(backend, "_codex_agent_query_snapshot_canvas", return_value=({"nodes": nodes, "connections": []}, nodes, [], "snapshot")):
            result = backend._codex_agent_tool_query_canvas(payload)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["nodes"]), 2)
        self.assertTrue(result["suggest_uniform_media_size"])
        self.assertIn("right", result["open_sides"])


class CanvasAgentOrganizeActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved = []
        self.canvas = {
            "id": "canvas-organize",
            "kind": "smart",
            "nodes": [
                image_node("landscape", 0, 0, 100, 100, 1600, 900),
                image_node("portrait", 50, 50, 100, 100, 900, 1600),
                {"id": "prompt", "type": "smart-prompt", "x": 100, "y": 100, "w": 600, "h": 500, "text": "prompt"},
            ],
            "connections": [],
        }
        self.patches = [
            patch.object(backend, "load_canvas", side_effect=lambda canvas_id: self.canvas, create=True),
            patch.object(backend, "save_canvas", side_effect=lambda canvas: self.saved.append(json.loads(json.dumps(canvas))), create=True),
            patch.object(backend, "normalize_canvas_kind", side_effect=lambda kind: kind, create=True),
            patch.object(backend, "CODEX_AGENT_REVISION_STORE", _RevisionStore()),
            patch.object(backend, "_codex_agent_record_undo"),
            patch.object(backend, "manager", new=SimpleNamespace(broadcast_canvas_updated=AsyncMock()), create=True),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()

    async def test_arrange_can_resize_and_layout_atomically(self):
        result = await backend._codex_agent_apply_canvas_actions(
            self.canvas["id"],
            [{
                "type": "arrange_nodes",
                "items": [{"node_id": "landscape"}, {"node_id": "portrait"}, {"node_id": "prompt"}],
                "options": {"mode": "vertical", "size_mode": "standard"},
            }],
            [],
            {},
        )
        self.assertEqual(result["changed"], 3)
        self.assertEqual(len(self.saved), 1)
        by_id = {node["id"]: node for node in self.canvas["nodes"]}
        self.assertEqual((by_id["landscape"]["w"], by_id["landscape"]["h"]), (520, 292))
        self.assertEqual((by_id["portrait"]["w"], by_id["portrait"]["h"]), (248, 440))
        self.assertNotIn("w", by_id["prompt"])
        ordered = sorted(self.canvas["nodes"], key=lambda node: node["y"])
        for previous, current in zip(ordered, ordered[1:]):
            previous_rect = backend._codex_agent_node_rect(previous)
            self.assertEqual(current["y"] - (previous["y"] + previous_rect["height"]), 54)

    async def test_tree_layout_moves_only_connected_component(self):
        self.canvas["nodes"] = [
            image_node("up", 0, 0, 200, 120, 1600, 900),
            image_node("root", 250, 0, 200, 120, 1600, 900),
            image_node("down1", 500, -100, 200, 120, 1600, 900),
            image_node("down2", 500, 100, 200, 120, 1600, 900),
            image_node("other", 1000, 300, 200, 120, 1600, 900),
        ]
        self.canvas["connections"] = [
            {"id": "c1", "from": "up", "to": "root", "kind": "input"},
            {"id": "c2", "from": "root", "to": "down1", "kind": "flow"},
            {"id": "c3", "from": "root", "to": "down2", "kind": "flow"},
        ]
        original_connections = json.loads(json.dumps(self.canvas["connections"]))
        other_before = next(node for node in self.canvas["nodes"] if node["id"] == "other").copy()
        result = await backend._codex_agent_apply_canvas_actions(
            self.canvas["id"],
            [{"type": "arrange_node_tree", "items": [{"node_id": "root"}], "options": {"tree_scope": "both"}}],
            [],
            {},
        )
        self.assertEqual(result["changed"], 4)
        other_after = next(node for node in self.canvas["nodes"] if node["id"] == "other")
        self.assertEqual((other_after["x"], other_after["y"]), (other_before["x"], other_before["y"]))
        self.assertEqual(self.canvas["connections"], original_connections)
        by_id = {node["id"]: node for node in self.canvas["nodes"]}
        self.assertLess(by_id["up"]["x"], by_id["root"]["x"])
        self.assertGreater(by_id["down1"]["x"], by_id["root"]["x"])
        self.assertGreater(by_id["down2"]["x"], by_id["root"]["x"])

    async def test_tree_layout_stays_near_root_when_viewport_is_smaller_than_tree(self):
        self.canvas["nodes"] = [
            image_node("up", -2200, 0, 520, 292, 1600, 900),
            image_node("root", 100, 100, 520, 292, 1600, 900),
            image_node("down", 2600, 0, 520, 292, 1600, 900),
            image_node("remote", 12000, 0, 520, 292, 1600, 900),
        ]
        self.canvas["connections"] = [
            {"id": "c1", "from": "up", "to": "root"},
            {"id": "c2", "from": "root", "to": "down"},
        ]
        result = await backend._codex_agent_apply_canvas_actions(
            self.canvas["id"],
            [{"type": "arrange_node_tree", "items": [{"node_id": "root"}], "options": {"tree_scope": "both"}}],
            [],
            {"native": {"visibleWorld": {"x": 0, "y": 0, "width": 900, "height": 650}}},
        )
        self.assertEqual(result["changed"], 3)
        by_id = {node["id"]: node for node in self.canvas["nodes"]}
        self.assertLess(abs(by_id["root"]["x"] - 100), 1500)
        self.assertLess(max(by_id[node_id]["x"] for node_id in ("up", "root", "down")), 4000)
        self.assertEqual(by_id["remote"]["x"], 12000)
        self.assertEqual(by_id["root"]["x"] - (by_id["up"]["x"] + by_id["up"]["w"]), 72)
        self.assertEqual(by_id["down"]["x"] - (by_id["root"]["x"] + by_id["root"]["w"]), 72)

    async def test_block_move_preserves_internal_offsets_and_avoids_group(self):
        self.canvas["nodes"] = [
            image_node("a", 0, 0, 200, 120, 1600, 900),
            image_node("b", 260, 60, 200, 120, 1600, 900),
            image_node("obstacle", 800, 0, 200, 120, 1600, 900),
        ]
        before_dx = self.canvas["nodes"][1]["x"] - self.canvas["nodes"][0]["x"]
        before_dy = self.canvas["nodes"][1]["y"] - self.canvas["nodes"][0]["y"]
        result = await backend._codex_agent_apply_canvas_actions(
            self.canvas["id"],
            [{
                "type": "move_nodes",
                "items": [{"node_id": "a"}, {"node_id": "b"}],
                "options": {"placement_scope": "global", "side": "right"},
            }],
            [],
            {},
        )
        self.assertEqual(result["changed"], 2)
        by_id = {node["id"]: node for node in self.canvas["nodes"]}
        self.assertEqual(by_id["b"]["x"] - by_id["a"]["x"], before_dx)
        self.assertEqual(by_id["b"]["y"] - by_id["a"]["y"], before_dy)
        self.assertGreater(by_id["a"]["x"], by_id["obstacle"]["x"] + by_id["obstacle"]["w"])

    async def test_move_can_target_anchor_side(self):
        self.canvas["nodes"] = [
            image_node("moving", 0, 0, 200, 120, 1600, 900),
            image_node("anchor", 500, 200, 200, 120, 1600, 900),
        ]
        result = await backend._codex_agent_apply_canvas_actions(
            self.canvas["id"],
            [{
                "type": "move_nodes",
                "items": [{"node_id": "moving"}],
                "options": {"placement_scope": "node", "anchor_node_id": "anchor", "side": "right"},
            }],
            [],
            {},
        )
        self.assertEqual(result["changed"], 1)
        by_id = {node["id"]: node for node in self.canvas["nodes"]}
        self.assertEqual(by_id["moving"]["x"], 780)
        self.assertEqual(by_id["moving"]["y"], 200)

    async def test_move_can_find_empty_viewport_slot(self):
        self.canvas["nodes"] = [
            image_node("moving", -500, -500, 200, 120, 1600, 900),
            image_node("obstacle", 24, 24, 200, 120, 1600, 900),
        ]
        canvas_context = {
            "native": {
                "visibleWorld": {"x": 0, "y": 0, "width": 1000, "height": 800},
                "allNodes": [
                    {"id": "moving", "x": -500, "y": -500, "width": 200, "height": 120},
                    {"id": "obstacle", "x": 24, "y": 24, "width": 200, "height": 120},
                ],
            }
        }
        result = await backend._codex_agent_apply_canvas_actions(
            self.canvas["id"],
            [{
                "type": "move_nodes",
                "items": [{"node_id": "moving"}],
                "options": {"placement_scope": "viewport", "side": "left"},
            }],
            [],
            canvas_context,
        )
        self.assertEqual(result["changed"], 1)
        by_id = {node["id"]: node for node in self.canvas["nodes"]}
        moving_rect = backend._codex_agent_node_rect(by_id["moving"])
        obstacle_rect = backend._codex_agent_node_rect(by_id["obstacle"])
        self.assertGreaterEqual(moving_rect["x"], 0)
        self.assertGreaterEqual(moving_rect["y"], 0)
        self.assertLessEqual(moving_rect["x"] + moving_rect["width"], 1000)
        self.assertLessEqual(moving_rect["y"] + moving_rect["height"], 800)
        self.assertFalse(backend._codex_agent_rect_intersects(moving_rect, obstacle_rect, 24))

    async def test_arrange_after_multi_media_reset_uses_default_grid_outer_frame(self):
        multi = image_node("multi", 0, 0, 900, 700, 1024, 1024)
        multi["images"].append({"url": "/output/second.png", "natural_w": 1024, "natural_h": 1024})
        single = image_node("single", 0, 900, 300, 300, 1024, 1024)
        self.canvas["nodes"] = [multi, single]
        result = await backend._codex_agent_apply_canvas_actions(
            self.canvas["id"],
            [{
                "type": "arrange_nodes",
                "items": [{"node_id": "multi"}, {"node_id": "single"}],
                "options": {"mode": "horizontal", "size_mode": "standard", "gapX": 80},
            }],
            [],
            {},
        )
        self.assertEqual(result["changed"], 2)
        by_id = {node["id"]: node for node in self.canvas["nodes"]}
        multi_rect = backend._codex_agent_node_rect(by_id["multi"])
        single_rect = backend._codex_agent_node_rect(by_id["single"])
        self.assertEqual((multi_rect["width"], multi_rect["height"]), (406, 211))
        self.assertEqual(single_rect["x"] - (multi_rect["x"] + multi_rect["width"]), 80)


if __name__ == "__main__":
    unittest.main()
