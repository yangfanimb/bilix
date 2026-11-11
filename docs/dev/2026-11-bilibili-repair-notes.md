# bilix B站主站功能修复记录（2026-11）

> 本文是本次修复工作的完整技术存档：分析结果、方案设计、实现要点、验证结论、遇到的问题与经验教训。
> 修复范围经确认限定为 **B站主站（bilibili）**，其他站点（douyin/tiktok/youtube 等）问题仅记录在附录，未修改。

---

## 1. 背景与基线

| 项 | 值 |
|---|---|
| 仓库 | `yangfanimb/bilix`（fork 自 [HFrost0/bilix](https://github.com/HFrost0/bilix)） |
| 基线版本 | v0.18.9，commit `bb5b234`（2024-09-20），工作区与 origin/master 一致、无本地改动 |
| 架构 | asyncio + httpx 异步下载器；CLI（click）→ `cli/assign.py` 分发 → 各站点 Downloader → API 层 |
| 站点模块 | bilibili（主站）、cctv、douyin、youtube、tiktok、yhdmp、yinghuacd、jable、hanime1 |
| 本次改动 | `bilix/sites/bilibili/api.py`（+150/-48）、`bilix/sites/bilibili/downloader.py`（+26/-9） |

**代码失效根因**：上游最后提交距今约两年，B站前端与接口发生多处不兼容变更（页面结构移除数据、旧接口下线、风控加强），导致部分功能静默或崩溃。

---

## 2. 运行环境备忘

- 可用解释器：`pyenv` 的 **3.12.0**（含全部依赖 + 可编辑安装），系统 Python 3.14 缺依赖，不要用。
- `ffmpeg/ffprobe` 位于 `/usr/local/bin`，合并功能依赖它。
- ⚠️ **site-packages 里存在一份旧的 bilix 0.18.9 真实拷贝**（非 editable 指向）：
  - `python script.py` / `python -I script.py` 命中 site-packages 旧代码（诊断结论因此曾"失真"一次）；
  - `python -m bilix` 需要 **cwd 在 `src/` 下**才会优先命中工作区代码；
  - `python -I` 会忽略 `PYTHONPATH`（-I 蕴含 -E）；
  - **建议执行** `pip install -e /opt/workspace/bilix/src` 让 `bilix` 命令行走工作区代码（未擅自执行）。

---

## 3. 修复前功能状态盘点（实测）

### 3.1 正常（端到端验证过）

| 功能 | 验证方式与结果 |
|---|---|
| `get_video` 单视频 | BV1GJ411x7h7 → HEVC+AAC 下载并 ffmpeg 合并（ffprobe 确认双流） |
| `--subtitle` / `--dm` / `--image` / `-oa` | srt 多语言、ass 弹幕、封面、纯音频均正常产出 |
| `get_up`（space URL + cookie） | 可下载；偶发 412 靠 req_retry 重试通过 |
| `get_collect` / `get_list` | API 层正常 |
| `get_favour`（可见收藏夹） | API 层正常（fid=69370754 返回 20 条） |
| cctv 站点 | `get_id → get_media_info` 正常 |
| 普通BV HTML 解析（`cidMap` 等） | 仍有效 |

### 3.2 损坏（含根因）

| # | 现象 | 根因（探测结论） |
|---|---|---|
| 1 | 番剧 `ep/ss` 页解析 KeyError | B站已把剧集数据从 `__NEXT_DATA__` 中**彻底移除**（整页只剩 season 骨架，`positive` 仅含 id/title），`_parse_ep_html` 期望的 `queries[0].state.data.seasonInfo.mediaInfo.episodes` 不复存在 |
| 2 | `get_cate` 恒为空 | 旧接口 `s.search.bilibili.com/cate/search` 匿名/登录均返回 `numResults:0`（接口实质废弃） |
| 3 | `bilix get_up 946974`（裸用户ID）报 "Can't find any handler" | 分发层 `handle` 只认 URL 正则；CLI 帮助文档却承诺"用户id"可用 —— **文档与实现不一致的历史欠账** |
| 4 | `bilix get_favour <fid>`（裸收藏夹ID）同样分发失败 | 同上 |
| 5 | 收藏夹不可见时 TypeError 崩溃 | API 返回 `code:0` 但 `data.medias=null`（私密/无权限），代码未判空直接迭代 |
| 6 | wbi 接口偶发 412 | 缺 `buvid3/buvid4` cookie（可通过 `x/frontend/finger/spi` 匿名获取）；带 cookie 也会概率性 412，req_retry 能兜住 |
| 7 | （范围外）douyin/tiktok/youtube/yhdmp/yinghuacd | 见附录 A |

---

## 4. 方案设计与取舍

### 4.1 番剧 ep/ss 解析 → 改用 pgc season API

- **方案**：URL 含 `/bangumi/play/` 时直接走 `GET /pgc/view/web/season?ep_id=|season_id=`，从返回的 `result` 构造 `VideoInfo`，随后沿用 `_attach_ep_dash`（pgc playurl v2）取流。
- **为何不修 HTML 解析**：实测确认数据已不在页面里，无 HTML 可修。
- **已验证的字段映射**：
  - `stat`：`coins/views/danmakus/share/likes/reply/favorite/favorites` 与 `Status` 模型一一对应；
  - 每集：`id`(ep_id)、`aid`、`cid`、`bvid`、`title`、`long_title`、`show_title`、`link`、`cover`；
  - 预告/PV 等可能不在 `episodes` 而在 `sections[*].episodes`，实现里做了兜底合并。
- **细节决策**：
  - `p_name` 取 `long_title or show_title or title`；多集且全空时用 `P{n}` 防止文件名冲突（同名文件会被 `path_check` 当成"已存在"而跳过，属静默丢任务）；
  - ep URL 找不到对应集 → `APIResourceError("未找到该集数")`；ss URL → 取第一集；
  - 用 `ep_found` 标志而非 cover 判断"是否找到"（有的集没有 cover，会误判）。

### 4.2 get_cate → 双通道替换

旧接口语义：子分区 + 热度排序 + 时间窗 + 分页。实测替代接口行为：

| 接口 | 实测结论 |
|---|---|
| `x/web-interface/wbi/search/type` | **必须有关键词**（空关键词 -400"请求错误"）；`tids` 过滤真实有效但**仅主分区**（tids=1/121 有差异，旧子分区 tid=33 恒空）；`tids_1` 参数被**完全忽略**（4 个不同值返回同一批结果）；时间过滤用 `pubtime_begin_s/pubtime_end_s`（unix 秒）；order 支持 `click/pubdate/dm/stow`，不支持的映射到 `totalrank` |
| `x/web-interface/newlist?main_ver=v3` | 支持旧子分区 tid（cate_id=33 返回 30 条）、支持 pn/ps 分页，按最新发布排序；不支持 order/时间过滤 |
| `x/web-interface/ranking/v2` | 仅主分区 rid（rid=33 → -400；rid=1/4 正常），无分页 → 弃用 |
| `x/web-interface/ranking/region` | 子分区可用但固定只有 10 条、无分页 → 弃用 |
| `x/web-interface/popular` | 可用但 rid 疑似不过滤 → 弃用 |
| `x/web-interface/dynamic/region` | 已 404 → 不可用 |

**最终设计**：
- 有关键词 → `search/type`（`tids=主分区tid`；结果为空则**降级全站搜索**并 `logger.warning`，保证永远有结果、行为透明）；时间窗 `%Y%m%d` → unix 秒。
- 无关键词 → `newlist v3`（文档注明不支持排序与时间过滤）。
- downloader 侧新增 `main_cate_id` 解析：遍历 `cate_meta` 找到包含该子分区 tid 的主分区。

### 4.3 裸 ID 分发（get_up / get_favour）

`handle` 的分发条件追加：

```python
or (method in {'up', 'get_up', 'fav', 'get_favour'} and keys[0].isdigit())
```

安全性论证：`assign.py` 对所有模块按相似度排序逐个尝试；其他站点模块的 `_decide_handle` 对纯数字 key 均不命中（URL pattern 不匹配），base 模块按 method 判断也不命中，因此不会误分发，bilibili 排序靠后也能被正确触达。`get_up_video_info`/`get_favour_page_info` 本身就支持 `url_or_mid`/`url_or_fid` 双态，无需再改。

### 4.4 get_favour 健壮性

- API 层：`medias is None` → `APIResourceError("收藏夹内容不可见（可能为私密收藏夹，或需要登录后访问）")`（此时 `code:0`、`media_count>0`，是权限问题而非空夹）。
- downloader 层：`get_favour` 与 `_get_favor_by_page` 的 API 调用包 `try/except APIError → logger.error`，避免向 CLI 抛裸 traceback（与 `get_series` 的处理风格一致）。

### 4.5 412 风控缓解

新增 `_ensure_buvid(client)`：首次调用 wbi 签名前若无 `buvid3`，从 `x/frontend/finger/spi` 匿名获取 `b_3/b_4` 写入 client cookies；失败仅 debug 日志（best effort，不阻断）。挂载点放在 `_add_sign` 开头，`get_up`/`get_cate(关键词)` 均被覆盖。

---

## 5. 验证矩阵（修复后全绿）

API 层脚本（`/tmp/bilix_test/verify_api2.py`，显式 `sys.path.insert` 指向工作区）+ CLI 端到端，登录态（使用者提供的 SESSDATA，经环境变量注入，未落盘）与匿名双跑：

| 用例 | 结果 |
|---|---|
| `get_video_info` ep URL / ss URL | ✔ 同一 season 正确解析（aid/cid/ep_id/p/pages） |
| `get_video ep831556` | ✔ 下载合并成功"是，大臣 第一季-1.mp4" |
| `get_series ss48267 -p 1 2` | ✔ 两集完成（ep2 无 dash 自动转 durl 路径，为非大会员内容的预期行为） |
| `get_cate 连载动画 -n 1`（无关键词） | ✔ 匿名与登录均下载成功 |
| `get_cate --keyword 原创`（子分区） | ✔ 主分区映射 + 降级告警后下载成功 |
| `get_up 946974`（裸ID）/ space URL | ✔ 下载成功 |
| `get_favour 69370754`（裸fid） | ✔ 多P视频下载成功 |
| `get_favour 22452274`（无权限） | ✔ 友好报错，无 traceback |
| 回归：BV `get_video`（匿名）、多P `get_series -p`、`info` | ✔ 全部正常 |
| `compileall` | ✔ 无语法错误 |

---

## 6. 遇到的问题与坑（按发现顺序）

1. **双份 bilix 安装陷阱**：`-I`/脚本方式运行命中 site-packages 旧拷贝，导致第一轮"修复验证"实际测的是旧代码，结果与修复前完全相同。`sys.path.insert(0, '/opt/workspace/bilix/src')` 或从 `src/` 目录用 `-m` 才可靠。**教训：改代码后第一件事是确认 `bilix.__file__`。**
2. **`python -I` 忽略 `PYTHONPATH`**（蕴含 `-E`），用环境变量注路径无效。
3. **`__NEXT_DATA__` 里只剩 1 个 query 且无 episodes**：数据移除是"彻底的"，sections 里的预告数据也不在页面，必须走 API。
4. **search/type 空关键词 -400、空格关键词 -1200**：分区热榜语义（无关键词）在 search API 上无等价物，才引出 newlist 通道。
5. **tids 只认主分区**：tid=33 恒空曾疑似"接口又坏了"，用同一关键词对比 tids=1/4/121/无 的结果集才定位（tids=1 与无 tids 结果不同 → 过滤真实存在；tids_1 四种取值结果完全相同 → 被忽略）。子分区 tid 在新分区体系下已不在 search 的过滤键里。
6. **ep831556 返回的 season 里找不到该 ep**：请求 `ep_id=1114212` 能返回某个 season 但 episodes 中无此集（预告/PV 类），因此"找不到指定集"必须是显式错误而不是静默取第一集。
7. **收藏夹 `code:0 + medias:null`**：与"空收藏夹（空数组）"语义不同，前者是权限问题，需区分对待。
8. **旧测试视频已失效**：`BV1bW411N7tb` 等仓库测试样例视频返回"视频已失效"，回归时要从**当天实测可达的资源**里取用例。

---

## 7. 经验教训

- **第三方接口必然漂移**：两年前的"正确代码"今天就是错的。排查顺序应当是：先抓原始响应（不要信中间层包装的异常），再对照代码期望的结构，最后才动代码。
- **异常包装会吃掉真相**：`raise_api_error` 把 KeyError/TypeError 包成 `APIParseError`，报错只剩"Caused by KeyError"。定位时要绕过装饰器拿原始 traceback。
- **文档与实现不一致要当 bug 修**：CLI 帮助承诺裸 ID，分发层却不支持——这类"承诺缺口"用户感知就是功能坏了。
- **错误处理分层**：API 层抛语义化异常（APIResourceError），downloader 层捕获转日志，CLI 层不出现裸 traceback；同时区分"权限不可见"与"真的为空"。
- **降级要透明**：search 降级全站、durl 替代 dash 这类行为变化必须 `logger.warning`，不能静默换语义。
- **验证要双态**：登录态通过的接口匿名可能 412/空结果，反之亦然；本轮所有用例均双跑。
- **临时脚本与凭证分离**：探测脚本放 `/tmp/bilix_test/`，cookie 只经环境变量注入，不写入仓库与文档。

---

## 8. 追加修复（2026-11-12）：下载结束后终端无法输入/显示

**现象**：用户报告下载完成后命令行无法输入、无法显示字符。

**排查过程**（pty 测试台：模拟真实终端，对比运行前后 termios 属性 + 光标 hide/show 序列配对 + 尾部字节）：

| 路径 | 结果 |
|---|---|
| 正常单视频/多P完成、Ctrl+C 中断、画质报错、info | ✅ termios 无变化、光标序列配对、退出以 `\r` 收尾（rich 15 的 Live.stop 自带） |
| ffmpeg 合并（继承 tty stdin）失败/被取消 | ✅ 该 ffmpeg 版本不使用 raw mode，termios 不受影响 |
| **bilix 子进程被 SIGKILL 强杀** | ❌ 光标停留隐藏态——bilix 自身代码无法恢复 |

在受控 pty 中未能复现用户的确切损坏（其终端为应用内终端，可能存在渲染差异），但确认了防御缺口。**修复（双层）**：

1. `bilix/cli/main.py`：`finally` 中 `CLIProgress.stop()` 包异常保护，并兜底写入 `\r\x1b[?25h`（光标可见+行首）——覆盖 bilix 自身任何路径的遗漏；
2. `bx.py`：`subprocess.call` 外层 `finally` 调用 `restore_terminal()` 同样写入——覆盖子进程被强杀等父进程仍存活的场景（实测 SIGKILL 子进程后终端被正确恢复）。

**用户侧应急恢复**：终端已损坏时盲打 `reset` 回车（或 `stty sane`）即可。

教训：rich Live 类 TUI 程序的终端恢复不能只依赖正常退出路径，父进程/finally 兜底 `\r\x1b[?25h` 是廉价且可靠的保险。

---

## 9. 遗留事项与建议（原第8节）

1. `pip install -e /opt/workspace/bilix/src`（或清理 site-packages 旧拷贝），否则 `bilix` 命令仍是旧代码。
2. 412 彻底治理需实现 buvid 激活签名（`ExClimbWuzhi`），当前靠重试兜底。
3. `get_up`/`get_favour` 等列表接口对"UP无视频/空结果"仍会 IndexError（`vlist[0]`），未在本次范围内。
4. 仓库自带 `*_test.py` 依赖 pytest + pytest-asyncio（环境未装），且部分样例 URL 已失效；建议补充一组"当天可达"的冒烟用例。
5. 外部站点问题清单见附录 A，确认范围后可按同方法论修复。
6. 本文档属于开发笔记，未接入 VitePress 导航；如需面向用户公开，请在 `docs/.vitepress/config.ts` 与 `docs/en/` 同步。

---

## 附录 A：范围外站点问题（仅记录）

| 站点 | 现象 | 初步根因 |
|---|---|---|
| douyin | JSONDecodeError | 旧 `iesdouyin.com/web/api/v2/aweme/iteminfo` 接口已死（返回非 JSON）；现代方案是分享页 `_ROUTER_DATA` 解析或带签名的 web detail 接口 |
| tiktok | 429 + JSONDecodeError | 旧 `api16-normal-c-useast1a.tiktokv.com/aweme/v1/feed` 端点失效 |
| youtube | KeyError 'url' | 播放地址移入 `signatureCipher`（需 JS 解密）；代码只取 `adaptiveFormats[*].url`。可行方向：innertube android/ios client 直连 |
| yhdmp | ModuleNotFoundError: execjs | `execjs` 未写入 pyproject 依赖（打包 bug），且运行时需要 Node.js |
| yinghuacd | HTTP 400 | 站点本身疑似死亡/更换域名 |

## 附录 B：探测脚本索引（临时，/tmp/bilix_test/，重启即失）

| 脚本 | 用途 |
|---|---|
| `verify_api2.py` | 修复后 API 层全量验证（8 用例） |
| `debug1.py` / `debug_cate*.py` / `debug_tids*.py` | 各故障根因定位（原始响应抓取、tids 行为对比） |
| `probe_sites.py` / `probe_sites2.py` | 外部站点健康度探测 |
| `find_multip.py` | 多P回归用例查找 |

> 复现命令示例：`cd /opt/workspace/bilix/src && python -m bilix get_cate "连载动画" -n 1 -d /tmp/videos`

## 附录 C：提交与版本记录（2026-11-12）

| 提交 | 内容 |
|---|---|
| `82f945f` | fix(bilibili)：番剧/分区/裸ID/收藏夹修复 + buvid 引导 |
| `76d9d40` | fix(cli)：下载结束后兜底恢复终端状态 |
| `ac5cbb8` | feat：bx.py 便捷启动器 |
| （本文档所在提交） | docs：修复记录、接口行为参考、bx 使用指南 |

## 附录 D：经验教训总结（一句话版）

1. **接口必然漂移**：抓原始响应 → 对照代码期望 → 最后才改代码；页面解析类先 dump 完整 JSON key 树。
2. **确认 import 路径再验证**：本仓库曾存在 site-packages 旧拷贝遮蔽工作区代码，导致一轮验证测的是旧代码。改代码后先看 `bilix.__file__`。
3. **异常包装吃掉真相**：定位时绕过 `raise_api_error` 拿原始 traceback。
4. **文档承诺=契约**：CLI 帮助承诺裸 ID 就必须支持，否则用户感知就是"坏了"。
5. **错误分层**：API 层语义化异常 → downloader 层转日志 → CLI 层不裸奔 traceback；区分"无权限"与"真的为空"。
6. **降级要透明**：换接口/换语义必须 `logger.warning`。
7. **双态验证**：登录/匿名都要测。
8. **TUI 终端恢复不能只靠正常退出路径**：父进程/finally 兜底 `\r\x1b[?25h` 是廉价保险；用户终端损坏时 `reset` 盲打恢复。
9. **凭证安全**：cookie 只经环境变量注入测试脚本、保存在 `~/.bilix/`（0600），永不入库、永不写入文档与记忆。
