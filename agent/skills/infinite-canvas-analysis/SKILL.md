---
name: infinite-canvas-analysis
description: 总结、定位并分析 Infinite Canvas 节点和连接。
---

# Canvas 分析

- 局部问题使用选中/视口/节点详情查询，只有全局总结才查询全画布概要和分页索引。
- 理解上下游时使用 `get_connected_nodes`，不根据坐标猜测连接。
- 分析任务默认只读；用户没有要求修改时不执行画布写操作。
- 对用户用素材名、图序和位置描述，不暴露内部 ID。
