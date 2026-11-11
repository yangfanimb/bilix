# 开发笔记索引

本目录存放开发/维护过程的技术存档，不面向最终用户（未接入 VitePress 导航；如需公开请同步 `docs/.vitepress/config.ts` 与 `docs/en/`）。

| 文档 | 内容 |
|---|---|
| [2026-11-bilibili-repair-notes.md](./2026-11-bilibili-repair-notes.md) | B站主站功能修复全记录：背景、故障盘点、方案设计与取舍、实现要点、验证矩阵、踩坑与经验教训、遗留事项 |
| [bilibili-api-behaviors.md](./bilibili-api-behaviors.md) | B站接口行为参考（实测）：风控/签名/搜索/番剧/收藏夹/UP 投稿等接口的当前行为与坑，含"接口变更快速复核清单" |
| [site-support-status.md](./site-support-status.md) | 站点支持状态板 + 各平台方案记录 + "修复漂移站点"标准流程（playbook）——长效机制核心文档 |
| [../bx-guide.md](../bx-guide.md) | 用户侧文档：`bx.py` 便捷启动器（cookie 一次配置自动注入、浏览器导入、默认目录/画质） |

## 一页速览：本次修了什么（2026-11）

1. 番剧 ep/ss 解析崩溃 → 改用 `pgc/view/web/season` API（页面数据已被 B站移除）
2. `get_cate` 失效 → 关键词走 `wbi search/type`（主分区 tid + 降级全站告警），无关键词走 `newlist`
3. `get_up` / `get_favour` 裸 ID 无法分发 → 分发放行数字 ID
4. 收藏夹无权限崩溃 → 语义化报错 + downloader 层友好日志
5. wbi 412 缓解 → `_add_sign` 前 best-effort 引导 buvid3/buvid4

改动文件：`bilix/sites/bilibili/api.py`、`bilix/sites/bilibili/downloader.py`
