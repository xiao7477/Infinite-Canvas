---
name: infinite-canvas-generation
description: 在 Infinite Canvas 中创建或执行图片和视频生成。
---

# Canvas 生成

- 生图和生视频只使用 `infinite_canvas.generate_images` / `generate_videos` 或对应的仅创建节点工具，不调用 Codex 内置生图。
- 未指定 Provider/模型时直接使用画布当前默认值，无需先查询列表。
- 用户指定平台/模型或询问可用选项时，再调用 `get_generation_settings`。
- 参考素材按 `ref_1`、`ref_2` 顺序绑定，不丢失用户明确指定的比例、数量、时长或固定镜头等参数。
- 模糊且明显高成本的任务先做一次简短确认；用户明确要求直接运行时使用当前默认值。
