# 站点支持状态板（防漂移长效机制之一）

> 本文档由 `python -m bilix doctor` 的检查项与人工验证共同维护。
> **约定**：每次修复或验证站点后，更新本表的"最后验证"日期与方法摘要；doctor 的测试 URL 选用长期稳定资源，失效时优先换 URL 而非删检查项。

最后全量巡检：2026-11-12（doctor 全量 + 端到端）

| 站点 | 状态 | 最后验证 | 获取方法摘要 | 已知限制 |
|---|---|---|---|---|
| bilibili | ✅ 全功能 | 2026-11-12 | HTML(`__INITIAL_STATE__`) / API双路；番剧走 pgc season API；搜索走 wbi search/type；详见 [repair-notes](./2026-11-bilibili-repair-notes.md) | 偶发412靠重试；付费内容需登录 |
| cctv | ✅ | 2026-11-12 | 页面取 pid → cntv hls 接口 | — |
| douyin | ✅ | 2026-11-12 | iesdouyin 分享页两步法（ttwid cookie + `_ROUTER_DATA`），playwm→play 去水印 | 接口存在间歇性风控（空数据/挑战页），已做3轮重试；失败可换网络重试 |
| youtube | ✅ | 2026-11-12 | innertube（ANDROID 直链→IOS HLS 高清分轨→muxed itag18 三级fallback），m3u8 管线下载 | clientVersion 有硬校验需跟随yt-dlp更新；数据中心IP可能命中bot风控（需登录cookie） |
| tiktok | ✅ | 2026-11-12 | 网页 `__UNIVERSAL_DATA_FOR_REHYDRATION__` + WAF PoW 解算；失败自动退回 tikwm 第三方 | playAddr 下载需 UA+Referer+会话cookie（同client满足）；tikwm 为第三方兜底有频控 |
| jable | ✅ | 2026-11-12 | HTML 解析 og:meta + 页内 m3u8 | 403多为IP级Cloudflare临时风控，稍后重试或换网络 |
| hanime1 | ⚠️ 受限 | 2026-11-12 | HTML 解析 | Cloudflare 403：需浏览器 cookie（`-fb` 导入 cf_clearance），代码层无法绕过 |
| yhdmp | ❌ 失联 | 2026-11-12 | — | 站点失联（SSL错误）；且依赖 execjs/Node.js（环境无 Node）。若复活：需将 yhdmp.js 移植为纯 Python（`__getplay_rev_data` 算法可读：hex字节+偏移+逆序+URI解码） |
| yinghuacd | ❌ 站点死亡 | 2026-11-12 | — | 原域名 530（源站下线）；镜像域名繁多难辨真伪，不建议盲连 |

## 功能矩阵（2026-11-13 审计）

| 功能 | bilibili | douyin | tiktok | youtube | cctv | jable | hanime1 |
|---|---|---|---|---|---|---|---|
| 单视频下载 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 封面 --image | ✅ | ✅ | ✅ | ➖平台无独立封面接口 | ➖ | ✅ | ✅ |
| **仅音频 -oa**（三格式） | ✅ 分离音轨 | ✅ 提取 | ✅ 提取 | ✅ 只下音频轨 | ✅ 提取 | ✅ 提取 | ✅ 提取 |
| **画质选择 -q** | ✅ | ➖平台单一画质 | ➖ | ✅ HLS分辨率选择 | ✅ | ➖ | ➖ |
| **编码过滤 --codec** | ✅ | ➖ | ➖ | ✅ HLS CODECS过滤 | ➖ | ➖ | ➖ |
| **字幕 --subtitle** | ✅ 多语言+过滤 | ➖平台无CC字幕接口 | ➖平台无CC字幕接口 | ✅ CC字幕(json3/XML双格式转SRT)+语言过滤 | ➖平台无 | ➖平台无 | ➖平台无 |
| 弹幕 --dm | ✅ | ➖平台无 | ➖平台无 | ➖ | ➖ | ➖ | ➖ |
| 切片 --time-range | ✅ | ➖ | ➖ | ➖ | ✅ | ✅ | ✅ |
| 系列/合集 get_series | ✅ | ➖ | ➖ | ✅ 播放列表(新旧结构双兼容+分页) | ✅ | ✅(get_actor) | ➖ |
| 编码选择 --codec | ✅ | ➖ | ➖ | ✅ | ➖ | ➖ | ➖ |
| info 探测 | ✅ | ➖ | ➖ | ✅ 画质档位/字幕/时长/播放数据 | ➖ | ➖ | ➖ |
| 总结报告 | ✅ | ➖单视频为主 | ➖单视频为主 | ✅ 播放列表批量 | ➖ | ➖ | ➖ |

➖ = 平台侧无此数据/功能，或待实现（见下）。单文件平台的 -oa 为下载成品视频后提取音轨（`BaseDownloader.extract_audio` 通用后处理），youtube 为只下载音频分轨（更高效）。

### 待实现清单（按价值排序，2026-11-13 已完成1-3）

1. ~~youtube playlist/系列下载~~ ✅ 已实现（VL browse + lockupViewModel 新结构兼容 + 分页 + UU频道上传清单）
2. ~~youtube info 探测~~ ✅ 已实现（画质档位/编码/字幕语言/时长/播放数据）
3. ~~youtube codec 选择~~ ✅ 已实现（HLS CODECS 过滤）
4. douyin 用户主页/合集批量（需a_bogus签名或第三方接口，风控风险高，暂缓）
5. 总结报告推广到 douyin/tiktok 单视频（价值有限，视频单发时日志已足够）

## 平台方案记录（防"重复调研"）

### youtube（2026-11-12 实测）
- 旧方法（网页 `ytInitialPlayerResponse.streamingData.adaptiveFormats[].url`）已失效：2025年中起全面 SABR，`adaptiveFormats` 只有元数据无 url 也无 signatureCipher（需 PO token）。
- 现方案：`POST https://www.youtube.com/youtubei/v1/player?prettyPrint=false`。
  1. ANDROID client（clientVersion 21.26.364，UA `com.google.android.youtube/...`）：`formats` 含 muxed itag18 直链；`adaptiveFormats` 有直链时优先（少数内容）
  2. IOS client（21.26.4, iPhone16,2）：`streamingData.hlsManifestUrl` → master 清单解析最高 BANDWIDTH 视频分轨 + TYPE=AUDIO 音频分轨 → m3u8 管线下载 → ffmpeg 合并（**免 PO token 的高清路径**）
  3. 兜底：muxed itag 18 单文件（video_url 带 audio_url=None，downloader 直存不合并）
- **坑**：旧版本号（19.09.37/19.45.4）被硬校验直接 400；HLS 中间文件必须带扩展名（ffmpeg.concat 按扩展名推断容器，无后缀报 exit 234）；IOS HLS 音频分轨常无 EXT-X-MAP、分段为 ADTS AAC。

### douyin（2026-11-12 实测）
- 旧方法（`iesdouyin.com/web/api/v2/aweme/iteminfo`）已失效。
- 现方案：`GET https://www.iesdouyin.com/share/video/{id}` 两步法——第一次拿 `ttwid` cookie，第二次同 URL 的 HTML 含 `window._ROUTER_DATA`；`loaderData[*].videoInfoRes.item_list[0]`；`play_addr.url_list[0]` 把 `/playwm/` 换 `/play/` 302 到 douyinvod CDN（206 可下，带时效）。移动端 UA 关键。
- **坑**：接口间歇性风控（空 item_list/挑战页），必须多轮重试；桌面端 douyin.com 需 a_bogus 签名勿碰。

### tiktok（2026-11-12 实测）
- 旧方法（`api16-normal-c-useast1a.tiktokv.com/aweme/v1/feed`）429 失效；api22/POST multi/detail 亦不可用（需 App 签名）。
- 现方案：`GET https://www.tiktok.com/@{user}/video/{id}`（完整浏览器头）→ `<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">` → `__DEFAULT_SCOPE__.webapp.video-detail.itemInfo.itemStruct`；playAddr 下载需 UA+`Referer: https://www.tiktok.com/`+会话cookie 三者齐全。
- WAF 挑战页（`<p id="cs">`）用 yt-dlp 同款 PoW 解算（sha256 穷举）后重试；二级兜底 `tikwm.com/api/?url=&hd=1`（第三方，显式标注）。

### yinghuacd / yhdmp
- 见状态板；复活前无法修复，doctor 会持续报告。

## 修复一个"漂移站点"的标准流程（playbook）

1. `python -m bilix doctor --site <站点>` 确认失败层级（网络/L1解析/L2媒体）；
2. 抓**原始响应**（curl 或 httpx，绕过异常包装），与代码期望的 JSON key 树/HTML 锚点对比；
3. WebSearch 该平台当前的官方数据流（优先无签名、无需登录的路径；其次 innertube/移动端 API；第三方解析 API 仅作兜底并显式标注）；
4. 改 `sites/<站点>/api.py`（保持 VideoInfo 模型字段与 downloader 兼容），多来源 fallback 用 try 链；
5. 该站点 `*_test.py` 更新用例 → doctor 注册表核对 L1/L2 → 端到端 `python -m bilix get_video <url>`；
6. 更新本状态板日期与方法摘要，提交。
