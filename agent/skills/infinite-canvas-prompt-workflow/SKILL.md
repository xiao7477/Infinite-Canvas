---
name: infinite-canvas-prompt-workflow
description: 根据主题或参考素材生成并组织 Canvas 提示词。
---

# Canvas 提示词工作流

- 有附件时优先按 `ref_1`、`ref_2` 的顺序理解参考素材。
- 需要画布内容时先查询相关节点，不读取无关的全画布。
- 用户要求放回画布时，使用 `create_prompt_nodes`。
- 提示词节点保留与参考素材的语义关系，但不自行篡改原素材。
