# B站接口行为参考（2026-11 实测）

> 本文记录本次修复过程中**实测确认**的接口行为，供后续维护与再次排障参考。
> 所有结论均基于 2026-11-12 的真实请求（匿名 + 登录双态），B站接口随时可能再变，使用前建议先复核。
> 注意条目中标注 ⚠️ 的是与直觉/旧文档不一致、容易踩坑的点。

## 1. 风控与请求头

| 项 | 实测行为 |
|---|---|
| `buvid3/buvid4` | 可通过 `GET /x/frontend/finger/spi` **匿名**获取（`data.b_3/b_4`），直接写入 cookies 即可降低 wbi 接口 412 概率；未做 ExClimbWuzhi 激活 |
| wbi 接口 412 | `x/space/wbi/arc/search` 在带 SESSDATA 情况下仍会概率性 412（HTML 响应体），req_retry 的 5 次退避重试可兜住 |
| 匿名可用性 | `get_video_info` HTML 路径、`pgc/view/web/season`、`x/v3/fav/resource/list`（公开夹）、`newlist`、`ranking/v2`、`finger/spi` 匿名均可用；`arc/search` 匿名必 412 |
| UA/Referer | 现有 `dft_client_settings`（PostmanRuntime UA + bilibili referer）在上述接口均未触发拦截 |

## 2. WBI 签名（`_add_sign`）

- 流程：`x/web-interface/nav` 取 `wbi_img.img_url/sub_url` 文件名拼接 → 按 OE 置换表取 32 位 → params 加 `wts` 后字典序拼接 `k=v&...` + token → md5 得 `w_rid`。
- ⚠️ **每次参与签名的参数集合必须与最终请求完全一致**（先加 `wts` 再签；中途改参数需重签）。
- 签名参数中包含 `tids`、`pubtime_begin_s`、`pubtime_end_s`、`keyword`（含中文）均正常工作。

## 3. 搜索相关

### 3.1 `x/web-interface/wbi/search/type`（视频搜索）

| 参数 | 行为 |
|---|---|
| `keyword` | ⚠️ **必填**：空 → `code:-400 请求错误`；纯空格 → `code:-1200`；请求前应 strip |
| `tids` | ⚠️ **仅主分区 tid 有效**。tids=1（动画）、121（鬼畜）对结果有真实过滤；旧子分区 tid=33（连载动画）**恒返回 0 条**且 code=0（无错误提示） |
| `tids_1` | ⚠️ **被完全忽略**：tids_1=1/33/121/21 返回完全相同的结果集 |
| `order` | 支持 `click`（播放）、`pubdate`（最新）、`dm`（弹幕）、`stow`（收藏）；不支持 `scores`/`coin`（旧 cate/search 的取值），未知值建议回落 `totalrank` |
| `pubtime_begin_s` / `pubtime_end_s` | unix 秒级发布时间过滤，配合 wbi 签名正常生效 |
| `page` / `page_size` | 正常分页 |
| 响应 | `data.result[]`，视频项含 `bvid`；`data.numResults` 为命中总数 |

**经验**：区分"参数被忽略"与"过滤真实生效"，要用**同一关键词 + 不同参数值对比结果集**，单看有无结果会误判。

### 3.2 `x/web-interface/newlist`（分区最新投稿）

- `main_ver=v3&search_type=video&cate_id=<旧子分区tid>&pn=&ps=` 可用，**支持旧子分区 tid（如 33）**，按发布时间倒序。
- ⚠️ 不带 `main_ver=v3` 时子分区返回 `code:0` 但 `archives` 为空。
- 不支持 order 与时间过滤。
- 对比：`ranking/v2`（仅主分区 rid，无分页）、`ranking/region`（固定 10 条）、`popular`（rid 疑似不过滤）、`dynamic/region`（已 404）均不适合做 get_cate 替代。

### 3.3 已废弃

- `s.search.bilibili.com/cate/search`：匿名/登录均返回 `numResults:0`（`code:0`），**实质废弃**；无时间参数时返回 `-10 time_from format error`（旧格式 `%Y%m%d`），有参数也查不到东西。

## 4. 番剧（pgc）

| 接口 | 行为 |
|---|---|
| `pgc/view/web/season?ep_id=` / `?season_id=` | 匿名可用；`result` 含 `title/evaluate/stat/episodes/sections/cover/season_id` |
| `result.stat` | 字段：`coins/views/danmakus/share/likes/reply/favorite/favorites`（与旧 HTML `mediaInfo.stat` 同构） |
| `result.episodes[]` | 每项含 `id`(ep_id)、`aid`、`cid`、`bvid`、`title`、`long_title`、`show_title`、`link`、`cover`、`duration` 等 |
| ⚠️ ep 与 season 的对应 | 请求某 ep_id 返回的 season 的 `episodes` 里**不一定包含该 ep**（预告/PV/花絮类可能在 `sections` 中，或另有归属）；`sections[*].episodes` 结构与 episodes 一致 |
| ⚠️ 番剧播放页 HTML | `__NEXT_DATA__` 仍存在，但 `dehydratedState.queries` 只有 1 个 query，**已无 seasonInfo/mediaInfo/episodes**（`positive` 仅 `{id,title}`）；页面解析路线不可修复，必须走 season API |
| `pgc/player/web/v2/playurl` | `support_multi_audio/avid/cid/fnver/fnval=4048/fourk/ep_id` 参数组合仍有效；返回 `result.video_info.dash/durl` |
| 非大会员下载付费番剧 | dash 不可用时回退 durl（预览画质），属内容权限而非代码问题 |

## 5. 收藏夹（`x/v3/fav/resource/list`）

- 参数 `media_id/pn/ps/keyword/order=mtime`。
- ⚠️ **三种返回状态语义不同**：
  - `data.medias` 为数组：正常（条目 `title == '已失效视频'` 的要过滤）；
  - `data.medias` 为**空数组**：收藏夹真的为空；
  - `data.medias` 为 **`null` 且 `code:0`**：无权限/私密收藏夹（`media_count` 可能仍 >0），应报错而不是当空处理。
- 空数组夹与 null 夹的 `data.info.title/upper` 均正常返回。

## 6. UP 主投稿

- `x/space/wbi/arc/search`：需 wbi 签名 + SESSDATA（匿名 412）；`mid/order/ps/pn/keyword`；响应 `data.list.vlist[]`（含 `bvid/author`）、`data.page.count`。
- ⚠️ UP 无投稿时 `vlist` 可能为空数组，`vlist[0]['author']` 取名会 IndexError（遗留问题，未在本次修复范围）。
- `x/space/wbi/acc/info`：wbi 签名后可用，取 UP 昵称。
- 裸 mid 直传（不经 space URL）时注意 `keyword` 要用空串而非 `quote("")`（原实现 quote 后签名仍有效，保持原样即可）。

## 7. 普通 BV（投稿视频）

- HTML 路径（`window.__INITIAL_STATE__` + `cidMap` + `window.__playinfo__`）**仍有效**；`window._riskdata_` 出现时表示被风控，应降级到 `x/web-interface/view` API 路径。
- `x/web-interface/view`：`bvid/aid` 参数，`follow_redirects=True`。
- `x/player/playurl`：`cid/bvid/qn=120/fnval=4048/fourk=1`，`code != 0` 时 message 可直接呈现给用户（如"视频已失效"——注意部分是权限/地区问题，文案未必准确）。
- 字幕 `x/player/v2`、弹幕 `x/v2/dm/web/view` + `seg.so`：未变更，正常。

## 8. 快速复核清单

下次怀疑"B站接口又变了"时按此顺序排查：

1. 抓**原始响应**（绕过 `raise_api_error` 包装），看 code/message/content-type；
2. 对比匿名/登录双态；
3. 同一请求手工 curl/httpx 重放，排除签名与中间层；
4. 关键结构变更（页面解析类）先 dump 页面内嵌 JSON 的完整 key 树再对照代码；
5. 搜索类接口用**同关键词换参数值**的对比实验区分"过滤失效"与"参数被忽略"；
6. 修改后确认 `bilix.__file__` 指向工作区代码再验证。
