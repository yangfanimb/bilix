# 经验教训总结（bilix 修复工程全记录提炼）

> 2026-11 两轮修复工程的最终沉淀：第一轮 B站主站（详见 [repair-notes](./2026-11-bilibili-repair-notes.md)），第二轮外部平台 + 长效机制（详见 [site-support-status](./site-support-status.md)）。
> 本文按主题归类，供后续维护者快速吸收。

## 一、排查方法论

1. **接口必然漂移**：两年前的"正确代码"今天就是错的。排查顺序固定：抓**原始响应**（curl/httpx，绕过异常包装）→ 对照代码期望的结构 → 最后才改代码。
2. **分层定位**：先用分层探针区分故障层级（网络可达 → 信息解析 → 媒体可下载），`python -m bilix doctor --site <站点>` 就是这个思路的工具化。
3. **对比实验定真伪**：判断"参数被忽略"还是"过滤生效"，用同关键词换参数值对比结果集；单看有无结果会误判（tiktok tids_1 案例）。
4. **警惕验证环境失真**：site-packages 旧拷贝曾让一轮"修复验证"测的是旧代码。**改代码后第一件事：确认 `bilix.__file__` 指向工作区**。`python -I` 忽略 PYTHONPATH（蕴含-E）。
5. **双态验证**：登录/匿名、不同IP/网络都要测——大量"接口失效"其实是风控灰度（bilibili 412、douyin 空数据、jable 403 都是间歇性的）。
6. **文献结论必须实测**：本项目三平台调研均要求代理区分"文献说"与"实测过"，避免把过期方案搬进代码。

## 二、代码模式（防漂移四件套）

1. **多来源 fallback**：youtube 三级（ANDROID直链→IOS HLS→muxed）、tiktok 双路（网页→tikwm）、douyin 多轮重试。每级 fallback 失败要透明（logger.warning/异常语义化）。
2. **解析器容错**：不依赖 JSON key 的字面形态（douyin loaderData 遍历取值；youtube 清单属性顺序无关逐行判断）；对 `null` 值防御（`v` 可以是 None）。
3. **语义化错误分层**：API 层抛语义化异常（APIResourceError/APIUnsupportedError + 用户可行动的提示"需登录/可重试/换网络"）→ downloader 层转日志 → CLI 不裸奔 traceback。
4. **版本敏感点显式标注**：youtube clientVersion 硬校验、依赖外部项目的地方，在代码头部 docstring 和状态板双处标注"失效时跟谁更新"。

## 三、容易踩的坑（各平台速查）

| 平台 | 坑 |
|---|---|
| bilibili | 页面数据可被整体移除（番剧 episodes）；wbi 风控 412 靠 buvid+重试；`code:0 + medias:null` 是权限问题非空集合 |
| douyin | 桌面端 douyin.com 需 a_bogus 签名勿碰；分享页两步法必须移动端UA；间歇性空数据需整链路重试 |
| tiktok | playAddr 下载需 UA+Referer+会话cookie 三者齐全；WAF 挑战用 PoW 解算；旧 aweme feed 全线失效 |
| youtube | SABR 后 adaptiveFormats 无直链需 PO token；clientVersion 硬校验；**HLS 中间文件必须带扩展名**（ffmpeg.concat 按扩展名推断容器，无后缀 exit 234）；IOS HLS 音频分段为 ADTS AAC 且常无 EXT-X-MAP |
| 通用 | 无扩展名的中间文件是 ffmpeg 大坑；测试URL会过期，回归用例要从当天可达资源里取 |

## 四、工程与协作

1. **文档承诺=契约**：CLI 帮助写了裸 ID 就必须支持；文档与实现不一致按 bug 修。
2. **降级要透明**：换接口、换语义、第三方兜底都必须显式告知（日志/文档标注），不能静默换行为。
3. **长效机制优于一次性修复**：doctor 注册表 + CI 定时巡检 + 状态板 + playbook，把"发现漂移"的成本从用户报障降到一条命令。
4. **修复闭环**：改代码 → 单点验证 → doctor L1/L2 → 端到端 → 状态板更新日期与方法 → 提交。六步缺一不可。

## 五、安全与凭证管理（铁律）

> **重要的秘密信息绝不能上传云端**（用户明确要求，永久生效）。

- 凭证（SESSDATA/浏览器cookie/API密钥/密码）只经**环境变量**注入进程，或存于用户本机（`~/.bilix/config.json`，权限 0600）
- **永不写入**：仓库文件、文档、代码注释、commit message、公开对话
- **每次 push 前必做**：对暂存内容扫描凭证特征（如 SESSDATA 值片段、token、密码字样）；发现即中止并报告
- 文档提及凭证时只描述**管理方式**（如"经环境变量注入，未落盘"），绝不引用值
- 若疑似泄露：立即在平台侧使凭证失效（如B站退出所有设备），而非只删文件

## 六、验证清单（每次改动的最小闭环）

- [ ] `bilix.__file__` 指向工作区
- [ ] `compileall` 通过
- [ ] 相关站点 API 单测/探针通过（L1）
- [ ] doctor `--site` 通过（L1+L2）
- [ ] 端到端 CLI 下载 + ffprobe 验证音视频流
- [ ] 回归：不受影响的站点抽查
- [ ] 状态板更新日期与方法
- [ ] **凭证扫描**（见第五节）
- [ ] 逻辑化 commit message（动机+方案）
