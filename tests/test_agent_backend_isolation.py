import inspect
import unittest

import agent.backend as backend
import main


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


if __name__ == "__main__":
    unittest.main()
