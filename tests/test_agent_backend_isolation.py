import os
import inspect
import tempfile
import unittest
from unittest import mock

import agent.backend as backend
import main
from PIL import Image


class CanvasAgentBackendIsolationTests(unittest.TestCase):
    def test_all_agent_api_paths_are_owned_by_agent_router(self):
        app_paths = {
            path
            for path in main.app.openapi()["paths"]
            if path.startswith("/api/codex-agent/")
        }
        router_paths = {
            route.path
            for route in backend.router.routes
            if getattr(route, "path", "").startswith("/api/codex-agent/")
        }
        self.assertEqual(app_paths, router_paths)
        self.assertGreaterEqual(len(app_paths), 20)

    def test_main_contains_only_agent_integration_bridge(self):
        source = inspect.getsource(main)
        self.assertNotIn("def _codex_agent_", source)
        self.assertNotIn("async def _codex_agent_", source)
        self.assertNotIn('@app.get("/api/codex-agent/', source)
        self.assertNotIn('@app.post("/api/codex-agent/', source)
        self.assertIn("configure_agent_backend(", source)
        self.assertIn("app.include_router(codex_agent_router)", source)

    def test_backend_dependencies_are_explicit_and_complete(self):
        configured = set(backend._host_dependencies)
        self.assertEqual(configured - {"codex_cli_resolver"}, set(backend._HOST_DEPENDENCY_NAMES))
        self.assertTrue(callable(backend._host_dependencies["codex_cli_resolver"]))

    def test_canvas_generation_routes_keep_persistence_bridge_after_extraction(self):
        self.assertIs(main._canvas_generation_task_persist, backend.persist_canvas_generation_task)
        self.assertIs(main._canvas_task_payload_dict, backend.canvas_task_payload_dict)
        payload = main.OnlineImageRequest(prompt="bridge regression")
        self.assertEqual(main._canvas_task_payload_dict(payload)["prompt"], "bridge regression")


class CliImageOutputIsolationTests(unittest.IsolatedAsyncioTestCase):
    def test_cli_fallback_copies_only_explicit_task_output(self):
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(main, "OUTPUT_OUTPUT_DIR", tmpdir):
            source = os.path.join(tmpdir, "provider-result.jpg")
            unrelated = os.path.join(tmpdir, "existing-canvas-asset.jpg")
            Image.new("RGB", (16, 12), "red").save(source, format="JPEG")
            Image.new("RGB", (10, 10), "blue").save(unrelated, format="JPEG")
            with open(unrelated, "rb") as file:
                unrelated_before = file.read()
            target = main.cli_image_output_path("gemini-cli", "canvas_img_test")

            resolved = main.cli_finalize_image_output(
                {"text": f"完成：{source}"},
                target,
                provider_id="gemini-cli",
            )

            self.assertEqual(resolved, target)
            self.assertTrue(os.path.isfile(source), "兼容回退只能复制，不能移走 Provider 原文件")
            self.assertTrue(os.path.isfile(target))
            with open(unrelated, "rb") as file:
                self.assertEqual(file.read(), unrelated_before)

    async def test_canvas_task_id_is_forwarded_to_each_cli_output(self):
        payload = main.OnlineImageRequest(
            prompt="task id isolation",
            provider_id="codex",
            model="gpt-image",
            n=2,
        )
        provider = {"id": "codex", "name": "GPT", "image_models": ["gpt-image"]}
        generated = (
            {"type": "url", "value": "/assets/output/result.png"},
            {"images": [{"type": "url", "value": "/assets/output/result.png"}]},
        )

        async def keep_local_url(item, prefix="online_"):
            return item.get("value") if isinstance(item, dict) else str(item)

        with (
            mock.patch.object(main, "get_api_provider", return_value=provider),
            mock.patch.object(main, "generate_ai_image", new=mock.AsyncMock(return_value=generated)) as generate,
            mock.patch.object(main, "save_ai_image_to_output", side_effect=keep_local_url),
            mock.patch.object(main, "save_to_history"),
        ):
            await main.build_online_image_result(payload, "canvas_img_test")

        output_keys = {call.args[-1] for call in generate.await_args_list}
        self.assertEqual(output_keys, {"canvas_img_test_1", "canvas_img_test_2"})


if __name__ == "__main__":
    unittest.main()
