---
name: infinite-canvas-asset-naming
description: 为 Infinite Canvas 素材生成显示名并批量重命名。
---

# Canvas 素材命名

- 先查询目标素材，向用户展示时用图序、当前名或内容标签，不显示内部 ID。
- 使用 `rename_assets`，只修改 `images[index].name` 对应的素材显示名。
- 不修改真实文件名、URL、路径或内部 `node.title`。
- 批量命名保持统一结构，并在需要时使用稳定序号。
