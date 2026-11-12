# bx.py —— bilix 便捷启动器

`bx.py` 是仓库根目录下的一个单文件脚本，把 `bilix` 命令行包装得更顺手。核心改进：**cookie 只需配置一次，之后每次下载自动注入**，并额外支持从浏览器一键导入、登录状态检查、默认下载目录/画质记忆、站点健康自检。

## 快速开始

```bash
# 1. 配置cookie（二选一，B站下载需要；其他平台无需登录）
python bx.py login                    # 交互式：粘贴SESSDATA，或回车后从浏览器导入
python bx.py login --browser chrome   # 直接从浏览器读取cookie
python bx.py login --sessdata "..."   # 非交互式（适合脚本）

# 2. 下载（url自动识别站点，支持 bilibili/douyin/tiktok/youtube/cctv/jable）
python bx.py v "https://www.bilibili.com/video/BV1GJ411x7h7"      # B站单视频
python bx.py "https://space.bilibili.com/946974" -n 5              # 直接丢url，自动识别
python bx.py s "https://www.bilibili.com/bangumi/play/ss48267" -p 1 3   # 番剧P1~P3
python bx.py up 946974 -n 10 --keyword 教程                        # UP主投稿
python bx.py fav 69370754 -n 10                                    # 收藏夹
python bx.py cate 连载动画 -n 10                                    # 分区
python bx.py v "https://www.douyin.com/video/xxx"                  # 抖音（无水印）
python bx.py "https://www.tiktok.com/@user/video/xxx"              # TikTok
python bx.py v "https://www.youtube.com/watch?v=xxx"               # YouTube（自动HLS高清）

# 字幕：--sub-lang 过滤语言（逗号分隔，对语言名/代码包含匹配，缺省下载全部轨）
python bx.py v "<链接>" --subtitle --sub-lang 中文                  # 只要中文字幕
python bx.py v "<链接> -p 3" --subtitle --sub-lang 中文,en          # 中英文字幕
python bx.py sub "<链接>" --sub-lang 中文                           # 只下载字幕不下载视频

# 仅音频：-oa 配合 --audio-format 选择输出格式
python bx.py v "<链接>" -oa                                         # 原始AAC流（默认）
python bx.py v "<链接>" -oa --audio-format m4a                      # 无损重封装M4A（时长元数据正确，推荐）
python bx.py v "<链接>" -oa --audio-format mp3                      # 转码MP3（自动探测带lame的ffmpeg）

# 站点健康自检（接口漂移排查）
python bx.py doctor                       # 全量巡检
python bx.py doctor --site youtube        # 只查某站点
python bx.py doctor --json                # JSON输出（便于CI）
```

## 方法与别名

| 别名 | 功能 | 对应 bilix 方法 |
|---|---|---|
| `v` / `video` | 单个视频 | get_video |
| `s` / `series` | 整个系列/多P/番剧 | get_series |
| `up` | UP主投稿 | get_up |
| `fav` / `favour` | 收藏夹 | get_favour |
| `cate` | 分区 | get_cate |
| `col` / `collect` | 合集/视频列表 | get_collect |
| `info` | 查看资源详情 | info |
| （直接传 url） | 自动识别站点与类型（bilibili/douyin/tiktok/youtube/cctv/jable） | auto |

`bilix` 原有的全部选项（`-q` 画质、`-n` 数量、`--dm` 弹幕、`--subtitle` 字幕、`-oa` 仅音频、`--dm` 弹幕、`-sl` 限速等）原样可用。

## Cookie 管理

| 命令 | 作用 |
|---|---|
| `python bx.py login` | 保存/更新 cookie：交互粘贴，或回车后从浏览器导入 |
| `python bx.py login --browser chrome` | 从指定浏览器读取（支持 chrome/edge/firefox/safari/opera/brave/chromium/vivaldi） |
| `python bx.py whoami` | 校验并显示登录状态（用户名/等级/大会员/硬币） |
| `python bx.py logout` | 清除 cookie（目录与画质设置保留） |
| `python bx.py doctor [--fast/--site/--json]` | 站点健康自检：L1信息解析 + L2媒体有效分层定位失效原因 |

> **cookie 与站点的关系**：只有 B站下载需要 cookie（会员视频/收藏夹/UP列表/番剧）；
> douyin、tiktok、youtube、cctv 等平台无需登录即可下载。站点健康状态随时 `doctor` 查看，
> 各站点现状与已知限制见 `docs/dev/site-support-status.md`。

### 怎么获取 SESSDATA？

1. 浏览器登录 [bilibili.com](https://www.bilibili.com)；
2. 按 `F12` 打开开发者工具 → **应用/Application**（Firefox 是"存储"）→ 左侧 **Cookies → https://www.bilibili.com**；
3. 找到名为 `SESSDATA` 的条目，复制它的**值**；
4. `python bx.py login` 时粘贴即可（无需自行处理URL编码）。

> SESSDATA 约一个月有效期；过期后 `whoami` 会提示，重新 login 即可。
> 大会员账号可下载会员视频；下载番剧/收藏夹/UP列表等接口需要登录。

## 默认配置

```bash
python bx.py config                 # 查看当前配置
python bx.py config -d ~/videos     # 设置默认下载目录
python bx.py config -q 1080p        # 设置默认画质（数字相对画质如 0/1/2 也可以）
python bx.py config --reset         # 重置目录与画质
```

## info 探测

```bash
python bx.py info "<链接>"
```

输出内容：视频标题与分P、播放/点赞/投币数据、**当前账号状态**（已登录用户名/大会员，未登录则提示部分画质不可下）、
**各画质流清单**（标注最高画质及分辨率，未登录/非会员的画质会标"需要登录或大会员"）、音质清单（默认/杜比/Hi-Res）、
**字幕轨情况**（中文字幕有无、英文字幕有无、全部语言轨列表）。据此可直接填写 `-q`/`--codec`/`--sub-lang`。

配置优先级：**命令行显式选项 > bx.py 保存的配置 > bilix 默认值**。
例如命令行里写了 `-d /other/path`，则忽略配置中的目录；写了 `--cookie`/`-fb`，则忽略已保存的 cookie。

> ⚠️ 画质注意：配置里存 `1080p` 这类**绝对画质名**表示"必须为该画质"，观看权限不够（如非大会员看付费番剧）时会报"清晰度/编码不可用"。没把握时建议不存画质（默认 `0` = 能看的最高画质自动适配），或存相对画质 `0/1/2`。

## 安全说明

- 配置保存在 `~/.bilix/config.json`（脚本会将其权限设为 `0600`，仅本人可读）；
- 该文件内含登录凭证，**不要提交到仓库或分享给他人**；
- 若怀疑泄露，去B站「安全设置」退出所有设备即可使其失效。

## 常见问题

- **下载结束后总结**：全部成功输出一行计数；存在异常时逐项列出（❌视频失效 / ⚠️字幕(语言)失败 等），便于批量下载后核对
- **直接 `./bx.py` 执行报 "Can't find any handler"**：shebang 选到的 `python3` 可能缺少 bilix 依赖，站点模块被静默跳过。bx.py 会自动探测可用解释器（仓库 .venv / python3.12 / pyenv versions）并切换重执行；也可用环境变量 `BX_PYTHON` 显式指定，或改用独立环境的 `./bx` 启动器。
- **提示缺少依赖**：脚本依赖 bilix 本体及其依赖（httpx、browser_cookie3 等），请先 `pip install -e /opt/workspace/bilix/src`；
- **从浏览器读取失败**：Chrome 新版本可能加密 cookie，或浏览器正在运行导致数据库被锁——改用交互式粘贴；
- **下载报 412/风控**：多由匿名高频访问引起，配置 cookie 后可显著缓解，脚本内置重试也会兜底。
- **下载结束后终端显示/输入异常**：极少数情况下（如下载进程被强制杀掉）终端的光标状态可能未被恢复，表现为提示符不显示、键入的字符看不见。此时输入 `reset` 回车（或 `stty sane` 回车，输入不可见也没关系，盲打即可）即可立即恢复。
  bx.py 已内置双层防护：下载子进程无论以何种方式退出（正常/中断/被杀），bx 都会在退出前强制恢复光标可见并回到行首。
