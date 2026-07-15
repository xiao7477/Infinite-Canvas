import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent.complex_tasks import ComplexTaskEngine, ComplexTaskError, infer_complex_task_mode, normalize_batch_task_spec, normalize_complex_task_spec


class ComplexTaskSpecTests(unittest.TestCase):
    def test_flat_batch_is_converted_to_deterministic_provider_stages(self):
        spec = normalize_batch_task_spec({"title": "six", "items": [
            {"id": f"gpt_{index}", "kind": "image", "provider_id": "codex", "prompt": f"gpt {index}"}
            for index in range(3)
        ] + [
            {"id": f"jimeng_{index}", "kind": "image", "provider_id": "jimeng", "prompt": f"jimeng {index}"}
            for index in range(3)
        ]}, canvas_id="c1")
        self.assertEqual(spec["mode"], "deterministic")
        self.assertEqual(spec["canvas_id"], "c1")
        self.assertEqual(sum(len(stage["items"]) for stage in spec["stages"]), 6)
        self.assertTrue(all(stage["type"] == "generate_image" for stage in spec["stages"]))

    def test_batch_rejects_old_dag_shape(self):
        with self.assertRaises(ComplexTaskError):
            normalize_batch_task_spec({"title": "old", "stages": [{"id": "images", "items": [{"prompt": "x"}]}]})

    def test_mode_is_inferred_from_agent_review(self):
        spec = {"stages": [{"id": "images", "type": "generate_image", "acceptance": {"review": "agent"}, "items": [{"prompt": "a"}]}]}
        self.assertEqual(infer_complex_task_mode(spec), "agentic")

    def test_explicit_deterministic_mode_wins(self):
        spec = {"mode": "deterministic", "stages": [{"id": "review", "type": "review", "items": [{}]}]}
        self.assertEqual(infer_complex_task_mode(spec), "deterministic")

    def test_rejects_cycles(self):
        with self.assertRaises(ComplexTaskError):
            normalize_complex_task_spec({"stages": [{"id": "one", "type": "prompt", "items": [{"id": "a", "depends_on": ["b"]}, {"id": "b", "depends_on": ["a"]}]}]}, canvas_id="c1")


class ComplexTaskEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.canvases = {"c1": {"id": "c1", "kind": "smart", "nodes": [], "connections": [], "updated_at": 1}}
        self.submitted = {}
        self.active = 0
        self.max_active = 0
        self.checkpoint_calls = 0
        self.checkpoint_contexts = []

        def load_canvas(canvas_id):
            return self.canvases[canvas_id]

        def save_canvas(canvas):
            canvas["updated_at"] = int(canvas.get("updated_at") or 0) + 1
            self.canvases[canvas["id"]] = canvas

        async def broadcast(canvas_id, updated_at):
            return None

        async def submit(task_id, kind, payload, node_id):
            task_id_value = f"provider-{len(self.submitted) + 1}"
            self.submitted[task_id_value] = {"kind": kind, "payload": payload, "node_id": node_id}
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            return {"task_id": task_id_value}

        async def poll(task_id_value):
            await asyncio.sleep(0.015)
            self.active -= 1
            kind = self.submitted[task_id_value]["kind"]
            return {"status": "succeeded", "video_items" if kind == "generate_video" else "image_items": [{"url": f"/{task_id_value}.{'mp4' if kind == 'generate_video' else 'png'}"}]}

        async def checkpoint(task, context):
            self.checkpoint_calls += 1
            self.checkpoint_contexts.append(dict(context))
            return {"decision": "accept"}

        self.engine = ComplexTaskEngine(
            Path(self.tmp.name) / "history.sqlite", Path(self.tmp.name) / "config.json",
            load_canvas=load_canvas, save_canvas=save_canvas, broadcast_canvas=broadcast,
            submit_generation=submit, poll_generation=poll, run_checkpoint=checkpoint,
        )
        self.callbacks = {"load_canvas": load_canvas, "save_canvas": save_canvas, "broadcast_canvas": broadcast, "submit_generation": submit, "poll_generation": poll, "run_checkpoint": checkpoint}

    async def asyncTearDown(self):
        for worker in self.engine._workers.values():
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*self.engine._workers.values(), return_exceptions=True)
        self.tmp.cleanup()

    async def wait_terminal(self, task_id, timeout=5):
        end = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < end:
            task = self.engine.get(task_id)
            if task["status"] in {"completed", "partially_completed", "failed", "cancelled", "waiting_user"}:
                return task
            await asyncio.sleep(0.02)
        self.fail("complex task did not reach a terminal state")

    async def test_one_hundred_program_nodes_need_no_agent_thread(self):
        spec = {"title": "100 prompts", "mode": "deterministic", "concurrency": {"global": 1}, "stages": [{"id": "prompts", "type": "prompt", "items": [{"text": f"prompt {index}"} for index in range(100)]}]}
        task = self.engine.create(spec, canvas_id="c1")
        done = await self.wait_terminal(task["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.checkpoint_calls, 0)
        self.assertEqual(len([node for node in self.canvases["c1"]["nodes"] if node.get("type") == "smart-prompt"]), 100)

    async def test_gpt_image_concurrency_never_exceeds_three(self):
        spec = {"title": "20 images", "mode": "deterministic", "concurrency": {"global": 4}, "stages": [{"id": "images", "type": "generate_image", "items": [{"prompt": f"shot {index}", "provider_id": "gpt-image"} for index in range(20)]}]}
        task = self.engine.create(spec, canvas_id="c1")
        done = await self.wait_terminal(task["id"])
        self.assertEqual(done["status"], "completed")
        self.assertLessEqual(self.max_active, 3)

    async def test_task_node_uses_prompt_node_default_size_and_generation_metadata_survives(self):
        spec = normalize_batch_task_spec({"title": "metadata", "items": [{
            "id": "cover", "kind": "image", "provider_id": "gpt-image", "model": "gpt-image-2",
            "prompt": "football cover", "reference_images": [{"url": "/Users/demo/ref.png", "name": "参考图"}],
            "size": "1024x1536", "ratio": "portrait", "resolution": "1k", "quality": "high", "count": 1,
        }]}, canvas_id="c1")
        task = self.engine.create(spec, canvas_id="c1")
        done = await self.wait_terminal(task["id"])
        task_node = next(node for node in self.canvases["c1"]["nodes"] if node.get("type") == "smart-agent-task")
        output_node = next(node for node in self.canvases["c1"]["nodes"] if node.get("complexTaskItemId") == "cover")
        self.assertEqual((task_node["w"], task_node["h"]), (316, 240))
        self.assertEqual(output_node["runPrompt"], "football cover")
        self.assertEqual(output_node["runInputRefs"][0]["url"], "/Users/demo/ref.png")
        self.assertEqual(output_node["runInputRefs"][0]["displayUrl"], "/api/codex-agent/file/view?path=%2FUsers%2Fdemo%2Fref.png")
        self.assertEqual(output_node["runSettings"]["model"], "gpt-image-2")
        self.assertEqual(output_node["runSettings"]["customSize"], "1024x1536")
        self.assertEqual(output_node["runSettings"]["ratio"], "portrait")
        self.assertEqual(done["summary"]["processed"], 1)
        self.assertEqual(len(self.canvases["c1"]["logs"]), 1)
        canvas_log = self.canvases["c1"]["logs"][0]
        self.assertEqual(canvas_log["status"], "success")
        self.assertEqual(canvas_log["nodeId"], output_node["id"])
        self.assertEqual(canvas_log["prompt"], "football cover")
        self.assertEqual(canvas_log["request"]["provider_id"], "gpt-image")
        self.assertEqual(canvas_log["outputs"][0]["url"], "/provider-1.png")

    async def test_finished_portrait_batch_is_reflowed_without_overlap(self):
        spec = normalize_batch_task_spec({"title": "portrait batch", "items": [
            {
                "id": f"portrait_{index}", "kind": "image", "provider_id": "gpt-image",
                "prompt": f"portrait {index}", "size": "1024x1536", "ratio": "portrait",
            }
            for index in range(3)
        ]}, canvas_id="c1")
        task = self.engine.create(spec, canvas_id="c1")
        done = await self.wait_terminal(task["id"])
        self.assertEqual(done["status"], "completed")
        output_nodes = sorted(
            (
                node for node in self.canvases["c1"]["nodes"]
                if node.get("complexTaskId") == task["id"] and node.get("type") == "smart-image"
            ),
            key=lambda node: node["y"],
        )
        self.assertEqual(len(output_nodes), 3)
        self.assertTrue(all((node["w"], node["h"]) == (293, 440) for node in output_nodes))
        self.assertTrue(all(node["x"] == output_nodes[0]["x"] for node in output_nodes))
        for previous, current in zip(output_nodes, output_nodes[1:]):
            self.assertGreaterEqual(current["y"], previous["y"] + previous["h"] + 48)

    async def test_progress_is_published_before_the_whole_concurrent_batch_finishes(self):
        release_slow = asyncio.Event()
        submitted = {}

        async def submit(task_id, kind, payload, node_id):
            provider_id = f"provider-{payload['prompt']}"
            submitted[provider_id] = {"kind": kind, "payload": payload, "node_id": node_id}
            return {"task_id": provider_id}

        async def poll(provider_id):
            if provider_id.endswith("slow"):
                await release_slow.wait()
            return {"status": "succeeded", "image_items": [{"url": f"/{provider_id}.png"}]}

        self.engine.submit_generation = submit
        self.engine.poll_generation = poll
        task = self.engine.create({"mode": "deterministic", "concurrency": {"global": 2}, "stages": [{
            "id": "images", "type": "generate_image", "items": [
                {"id": "fast", "prompt": "fast", "provider_id": "fast-provider"},
                {"id": "slow", "prompt": "slow", "provider_id": "slow-provider"},
            ],
        }]}, canvas_id="c1")
        for _ in range(100):
            current = self.engine.get(task["id"])
            if current["summary"].get("completed") == 1:
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("first item progress was not published while the second item was running")
        self.assertEqual(current["summary"]["processed"], 1)
        self.assertEqual(current["summary"]["running"], 1)
        task_node = next(node for node in self.canvases["c1"]["nodes"] if node.get("type") == "smart-agent-task")
        self.assertEqual(task_node["taskProgress"]["completed"], 1)
        release_slow.set()
        done = await self.wait_terminal(task["id"])
        self.assertEqual(done["status"], "completed")

    async def test_later_queued_item_runs_before_a_failed_item_retry(self):
        order = []
        attempts = {}
        submitted = {}

        async def submit(task_id, kind, payload, node_id):
            prompt = payload["prompt"]
            attempts[prompt] = attempts.get(prompt, 0) + 1
            provider_id = f"provider-{prompt}-{attempts[prompt]}"
            submitted[provider_id] = {"kind": kind, "payload": payload, "node_id": node_id}
            order.append(prompt)
            return {"task_id": provider_id}

        async def poll(provider_id):
            prompt = submitted[provider_id]["payload"]["prompt"]
            if prompt == "first" and attempts[prompt] == 1:
                return {"status": "failed", "error": "temporary"}
            return {"status": "succeeded", "image_items": [{"url": f"/{provider_id}.png"}]}

        self.engine.submit_generation = submit
        self.engine.poll_generation = poll
        task = self.engine.create({"mode": "deterministic", "concurrency": {"global": 2}, "limits": {"max_retries_per_item": 1}, "stages": [{
            "id": "images", "type": "generate_image", "items": [
                {"id": "first", "prompt": "first", "provider_id": "same-provider"},
                {"id": "second", "prompt": "second", "provider_id": "same-provider"},
            ],
        }]}, canvas_id="c1")
        done = await self.wait_terminal(task["id"], timeout=8)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(order, ["first", "second", "first"])
        first = next(item for item in done["items"] if item["id"].endswith(":first"))
        self.assertEqual(first["retry_after"], 0)

    async def test_stream_pipeline_connects_matching_image_to_video(self):
        spec = {"mode": "deterministic", "stages": [
            {"id": "images", "type": "generate_image", "items": [{"key": "1", "prompt": "image 1", "provider_id": "gpt-image"}, {"key": "2", "prompt": "image 2", "provider_id": "gpt-image"}]},
            {"id": "videos", "type": "generate_video", "depends_on": ["images"], "unlock": "stream", "items": [{"key": "1", "prompt": "video 1", "provider_id": "jimeng"}, {"key": "2", "prompt": "video 2", "provider_id": "jimeng"}]},
        ]}
        task = self.engine.create(spec, canvas_id="c1")
        done = await self.wait_terminal(task["id"])
        self.assertEqual(done["status"], "completed")
        videos = [item for item in done["items"] if item["stage_id"] == "videos"]
        self.assertTrue(all(len(item["depends_on"]) == 1 for item in videos))
        submitted_videos = [row for row in self.submitted.values() if row["kind"] == "generate_video"]
        self.assertTrue(all(row["payload"].get("reference_images") for row in submitted_videos))
        self.assertGreaterEqual(len(self.canvases["c1"]["connections"]), 4)

    async def test_agentic_task_uses_stage_and_final_checkpoints(self):
        spec = {"mode": "agentic", "stages": [{"id": "images", "type": "generate_image", "acceptance": {"review": "agent"}, "items": [{"prompt": "image", "provider_id": "gpt-image"}]}]}
        task = self.engine.create(spec, canvas_id="c1")
        done = await self.wait_terminal(task["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.checkpoint_calls, 2)
        self.assertEqual([row["stage_id"] for row in self.checkpoint_contexts], ["images", "__final__"])

    async def test_provider_failure_auto_retries_twice(self):
        attempts = 0

        async def always_fail(task_id_value):
            nonlocal attempts
            attempts += 1
            return {"status": "failed", "error": "rate limited"}

        self.engine.poll_generation = always_fail
        task = self.engine.create({"mode": "deterministic", "stages": [{"id": "images", "type": "generate_image", "items": [{"prompt": "image", "provider_id": "gpt-image"}]}]}, canvas_id="c1")
        done = await self.wait_terminal(task["id"], timeout=12)
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["items"][0]["attempt_count"], 3)
        self.assertEqual(attempts, 3)
        self.assertEqual(len(self.canvases["c1"]["logs"]), 1)
        self.assertEqual(self.canvases["c1"]["logs"][0]["status"], "failed")
        self.assertIn("rate limited", self.canvases["c1"]["logs"][0]["error"])

    async def test_waiting_user_reply_resumes_only_task(self):
        calls = 0

        async def ask_then_accept(task, context):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"decision": "ask_user", "question": "是否接受当前结果？"}
            return {"decision": "accept"}

        self.engine.run_checkpoint = ask_then_accept
        task = self.engine.create({"mode": "agentic", "stages": [{"id": "prompt", "type": "prompt", "items": [{"text": "hello"}]}]}, canvas_id="c1")
        waiting = await self.wait_terminal(task["id"])
        self.assertEqual(waiting["status"], "waiting_user")
        self.assertIn("是否接受", waiting["question"])
        self.engine.reply(task["id"], "接受")
        done = await self.wait_terminal(task["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["spec"]["runtime"]["last_user_reply"], "接受")

    async def test_cancel_does_not_cancel_already_submitted_poll(self):
        submitted = asyncio.Event()
        release = asyncio.Event()

        async def slow_poll(task_id_value):
            submitted.set()
            await release.wait()
            self.active -= 1
            return {"status": "succeeded", "image_items": [{"url": "/paid-result.png"}]}

        self.engine.poll_generation = slow_poll
        task = self.engine.create({"mode": "deterministic", "stages": [{"id": "images", "type": "generate_image", "items": [{"prompt": "image", "provider_id": "gpt-image"}]}]}, canvas_id="c1")
        await asyncio.wait_for(submitted.wait(), timeout=2)
        self.engine.control(task["id"], "cancel")
        release.set()
        await asyncio.sleep(0.05)
        cancelled = self.engine.get(task["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["items"][0]["status"], "completed")

    async def test_restart_resumes_provider_id_without_resubmitting(self):
        task = self.engine.create({"mode": "deterministic", "stages": [{"id": "images", "type": "generate_image", "items": [{"id": "image_1", "prompt": "image", "provider_id": "gpt-image"}]}]}, canvas_id="c1")
        worker = self.engine._workers[task["id"]]
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        node_id = "tasknode_recovered"
        self.canvases["c1"]["nodes"].append({"id": node_id, "type": "smart-image", "images": [], "complexTaskId": task["id"], "complexTaskItemId": "image_1", "complexTaskStageId": "images"})
        provider_id = "provider-existing"
        self.submitted[provider_id] = {"kind": "generate_image", "payload": {}, "node_id": node_id}
        self.active = 1
        with sqlite3.connect(str(Path(self.tmp.name) / "history.sqlite")) as conn:
            conn.execute("UPDATE complex_tasks SET status='running' WHERE id=?", (task["id"],))
            conn.execute("UPDATE complex_task_items SET status='waiting_provider', provider_task_id=?, node_id=? WHERE task_id=?", (provider_id, node_id, task["id"]))
            conn.commit()
        before = len(self.submitted)
        self.engine = ComplexTaskEngine(Path(self.tmp.name) / "history.sqlite", Path(self.tmp.name) / "config.json", **self.callbacks)
        await self.engine.startup()
        done = await self.wait_terminal(task["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(len(self.submitted), before)

    async def test_deleted_dependency_pauses_only_affected_branch(self):
        async def delete_image_at_checkpoint(task, context):
            if context.get("stage_id") == "images":
                image_item = next(item for item in task["items"] if item["stage_id"] == "images")
                self.canvases["c1"]["nodes"] = [node for node in self.canvases["c1"]["nodes"] if node.get("id") != image_item["node_id"]]
            return {"decision": "accept"}

        self.engine.run_checkpoint = delete_image_at_checkpoint
        spec = {"mode": "agentic", "stages": [
            {"id": "images", "type": "generate_image", "acceptance": {"review": "agent"}, "items": [{"key": "1", "prompt": "image", "provider_id": "gpt-image"}]},
            {"id": "videos", "type": "generate_video", "depends_on": ["images"], "unlock": "stream", "items": [{"key": "1", "prompt": "video", "provider_id": "jimeng"}]},
        ]}
        task = self.engine.create(spec, canvas_id="c1")
        waiting = await self.wait_terminal(task["id"])
        self.assertEqual(waiting["status"], "waiting_user")
        video = next(item for item in waiting["items"] if item["stage_id"] == "videos")
        self.assertEqual(video["status"], "blocked")
        self.assertIn("依赖节点", video["error"])

    async def test_agent_revision_changes_only_new_attempt_payload(self):
        reviews = 0

        async def revise_once(task, context):
            nonlocal reviews
            reviews += 1
            if context.get("stage_id") == "images" and reviews == 1:
                return {"decision": "revise_prompt", "item_ids": ["image_1"], "changes": [{"item_id": "image_1", "payload": {"prompt": "improved image"}}]}
            return {"decision": "accept"}

        self.engine.run_checkpoint = revise_once
        task = self.engine.create({"mode": "agentic", "stages": [{"id": "images", "type": "generate_image", "acceptance": {"review": "agent"}, "items": [{"id": "image_1", "prompt": "image", "provider_id": "gpt-image"}]}]}, canvas_id="c1")
        done = await self.wait_terminal(task["id"])
        self.assertEqual(done["status"], "completed")
        prompts = [row["payload"].get("prompt") for row in self.submitted.values()]
        self.assertEqual(prompts, ["image", "improved image"])
        self.assertEqual(done["items"][0]["attempt_count"], 2)


if __name__ == "__main__":
    unittest.main()
