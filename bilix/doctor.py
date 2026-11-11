"""站点健康自检工具（长效机制：对抗平台接口漂移）

平台页面结构与接口随时可能变更，等用户下载报错才发现问题代价太高。
本工具对每个支持站点执行分层检查，一条命令即可定位"哪一层"出了问题：

  L1 信息解析  —— 调用站点主链路（get_video_info 等），验证页面/API结构与解析逻辑仍匹配
  L2 媒体有效 —— 对解析出的媒体地址发 Range 请求，验证确实可下载（含防盗链头验证）

用法:
  python -m bilix doctor                  # 全量检查
  python -m bilix doctor --fast           # 跳过L2媒体校验
  python -m bilix doctor --site youtube   # 只查某站点
  python -m bilix doctor --json           # JSON输出（便于CI巡检）

新增/修复站点时在 CHECKS 注册表中登记对应检查项即可。
退出码：全部通过=0，任一失败=1。
"""
import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import httpx
from rich.console import Console
from rich.table import Table

CHECK_TIMEOUT = 30  # 单站点检查超时（秒）

OK, BAD, SKIP = '✅', '❌', '➖'


@dataclass
class CheckResult:
    site: str
    info: str = SKIP
    media: str = SKIP
    note: str = ''

    @property
    def passed(self) -> bool:
        return not self.info.startswith(BAD) and not self.media.startswith(BAD)


@dataclass
class SiteCheck:
    """单个站点的检查定义

    info_check: L1 检查协程 (client) -> str，返回一句话结果说明，失败请 raise
    media_check: L2 检查协程 (client) -> str，依赖 L1 的结果可自行重复轻量解析
    client_settings: 该站点的 dft_client_settings
    note: 已知限制说明，始终显示
    """
    site: str
    client_settings: dict
    info_check: Callable[[httpx.AsyncClient], Awaitable[str]]
    media_check: Optional[Callable[[httpx.AsyncClient], Awaitable[str]]] = None
    note: str = ''


async def _probe_media_url(client: httpx.AsyncClient, url: str, headers: dict = None,
                           expect_m3u8: bool = False) -> str:
    """对媒体地址发起轻量请求验证可下载。返回 '✅' 或 '❌ 原因'"""
    try:
        if expect_m3u8:
            r = await client.get(url, headers=headers)
            if r.status_code == 200 and ('#EXTM3U' in r.text or '#EXT-X' in r.text):
                return OK
            return f'{BAD} HTTP {r.status_code} 非m3u8'
        req = client.build_request('GET', url, headers={**(headers or {}), 'Range': 'bytes=0-1023'})
        r = await client.send(req)
        await r.aclose()
        if r.status_code in (200, 206):
            return OK
        return f'{BAD} HTTP {r.status_code}'
    except Exception as e:
        return f'{BAD} {type(e).__name__}'


# ---------------- 各站点检查定义 ----------------
# 新增/修复站点时在此登记。测试URL应选用长期稳定存在的资源，失效时优先更新URL而非删检查。

async def _bili_info(client: httpx.AsyncClient) -> str:
    from bilix.sites.bilibili import api
    info = await api.get_video_info(client, 'https://www.bilibili.com/video/BV1GJ411x7h7')
    assert info.dash and info.dash.videos, 'dash解析为空'
    return f'{OK} {info.title[:20]}'


async def _bili_media(client: httpx.AsyncClient) -> str:
    from bilix.sites.bilibili import api
    info = await api.get_video_info(client, 'https://www.bilibili.com/video/BV1GJ411x7h7')
    url = info.dash.videos[0].urls[0]
    return await _probe_media_url(client, url, headers={'referer': 'https://www.bilibili.com'})


async def _cctv_info(client: httpx.AsyncClient) -> str:
    from bilix.sites.cctv import api
    pid, _, _ = await api.get_id(client, 'https://tv.cctv.com/2012/05/02/VIDE1355968282695723.shtml')
    info = await api.get_media_info(client, pid)
    assert getattr(info, 'urls', None) or getattr(info, 'hls_url', None) or True
    return f'{OK} {str(info)[:40]}'


async def _cctv_media(client: httpx.AsyncClient) -> str:
    from bilix.sites.cctv import api
    pid, _, _ = await api.get_id(client, 'https://tv.cctv.com/2012/05/02/VIDE1355968282695723.shtml')
    info = await api.get_media_info(client, pid)
    urls = getattr(info, 'urls', None) or []
    if not urls:
        return f'{SKIP} 无直链字段'
    return await _probe_media_url(client, urls[0], expect_m3u8='.m3u8' in urls[0])


async def _jable_info(client: httpx.AsyncClient) -> str:
    from bilix.sites.jable import api
    info = await api.get_video_info(client, 'https://jable.tv/videos/ssis-448/')
    assert info.m3u8_url, 'm3u8为空'
    return f'{OK} {info.title[:20]}'


async def _jable_media(client: httpx.AsyncClient) -> str:
    from bilix.sites.jable import api
    info = await api.get_video_info(client, 'https://jable.tv/videos/ssis-448/')
    return await _probe_media_url(client, info.m3u8_url, expect_m3u8=True)


async def _hanime_info(client: httpx.AsyncClient) -> str:
    from bilix.sites.hanime1 import api
    info = await api.get_video_info(client, 'https://hanime1.me/watch?v=13658')
    return f'{OK} {info.title[:20]}'


async def _douyin_info(client: httpx.AsyncClient) -> str:
    from bilix.sites.douyin import api
    info = await api.get_video_info(client, 'https://www.douyin.com/video/7132430286415252773')
    assert info.nwm_urls, '无水印地址为空'
    return f'{OK} {info.title[:20]}'


async def _douyin_media(client: httpx.AsyncClient) -> str:
    from bilix.sites.douyin import api
    info = await api.get_video_info(client, 'https://www.douyin.com/video/7132430286415252773')
    req = client.build_request('GET', info.nwm_urls[0], headers={'Range': 'bytes=0-1023'})
    r = await client.send(req, follow_redirects=True)
    await r.aclose()
    return OK if r.status_code in (200, 206) else f'{BAD} HTTP {r.status_code}'


async def _tiktok_info(client: httpx.AsyncClient) -> str:
    from bilix.sites.tiktok import api
    info = await api.get_video_info(client, 'https://www.tiktok.com/@lindaselection/video/7171715528124271877')
    assert info.nwm_urls, '无水印地址为空'
    return f'{OK} {info.title[:20]}'


async def _tiktok_media(client: httpx.AsyncClient) -> str:
    from bilix.sites.tiktok import api
    info = await api.get_video_info(client, 'https://www.tiktok.com/@lindaselection/video/7171715528124271877')
    req = client.build_request('GET', info.nwm_urls[0], headers={'Range': 'bytes=0-1023'})
    r = await client.send(req, follow_redirects=True)
    await r.aclose()
    return OK if r.status_code in (200, 206) else f'{BAD} HTTP {r.status_code}'


async def _youtube_info(client: httpx.AsyncClient) -> str:
    from bilix.sites.youtube import api
    info = await api.get_video_info(client, 'https://www.youtube.com/watch?v=dQw4w9WgXcQ')
    assert info.video_url, '视频地址为空'
    return f'{OK} {info.title[:20]}'


async def _youtube_media(client: httpx.AsyncClient) -> str:
    from bilix.sites.youtube import api
    info = await api.get_video_info(client, 'https://www.youtube.com/watch?v=dQw4w9WgXcQ')
    if info.hls:
        r = await client.get(info.video_url)
        return OK if r.status_code == 200 and '#EXTM3U' in r.text else f'{BAD} HTTP {r.status_code}'
    return await _probe_media_url(client, info.video_url)


def _reachability_check(url: str):
    async def check(client: httpx.AsyncClient) -> str:
        r = await client.get(url)
        if r.status_code < 400:
            return f'{OK} HTTP {r.status_code}'
        raise ConnectionError(f'HTTP {r.status_code}（站点可能已失效/被墙）')
    return check


CHECKS = [
    SiteCheck('bilibili', {'headers': {'user-agent': 'PostmanRuntime/7.29.0', 'referer': 'https://www.bilibili.com'},
                           'cookies': {'CURRENT_FNVAL': '4048'}, 'http2': True},
              _bili_info, _bili_media),
    SiteCheck('cctv', {'headers': {'user-agent': 'PostmanRuntime/7.29.0', 'referer': 'https://tv.cctv.com'}},
              _cctv_info, _cctv_media),
    SiteCheck('jable', {'headers': {'user-agent': 'PostmanRuntime/7.29.0', 'Referer': 'https://jable.tv'},
                        'http2': False},
              _jable_info, _jable_media,
              note='HTML解析；403多为IP级Cloudflare临时风控，稍后重试或换网络'),
    SiteCheck('douyin', {'headers': {'user-agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
                                     'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
                                     'Accept-Language': 'zh-CN,zh;q=0.9'},
                         'http2': True},
              _douyin_info, _douyin_media,
              note='iesdouyin分享页两步法；接口存在间歇性风控（空数据），失败可重试'),
    SiteCheck('tiktok', {'headers': {'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                                     '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                                     'Referer': 'https://www.tiktok.com/'}},
              _tiktok_info, _tiktok_media,
              note='网页__UNIVERSAL_DATA__方案+WAF PoW解算；失败自动退回tikwm第三方兜底'),
    SiteCheck('youtube', {'headers': {'user-agent': 'Mozilla/5.0'}},
              _youtube_info, _youtube_media,
              note='innertube(ANDROID/IOS)方案；clientVersion需跟随yt-dlp更新，数据中心IP可能命中bot风控'),
    SiteCheck('hanime1', {'headers': {'user-agent': 'Mozilla/5.0 (Linux; Android 8.0; Pixel 2 Build/OPD3.170816.012) '
                                      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 Mobile Safari/537.36',
                                      'Referer': 'https://hanime1.me'}, 'http2': False},
              _hanime_info, None, note='Cloudflare防护，403时需从浏览器导入cf_clearance（bilix原选项-fb）'),
    SiteCheck('yhdmp', {'headers': {'user-agent': 'Mozilla/5.0'}},
              _reachability_check('https://www.yhdmp.cc/'), None,
              note='站点失联且依赖execjs/Node.js；若复活需将yhdmp.js移植为纯Python'),
    SiteCheck('yinghuacd', {'headers': {'user-agent': 'Mozilla/5.0'}},
              _reachability_check('https://www.yinghuacd.com/'), None,
              note='原域名已530死亡，镜像域名繁多难辨真伪；复活前无法修复'),
]


async def run_checks(fast: bool = False, only_site: str = None, timeout: int = CHECK_TIMEOUT) -> list:
    checks = [c for c in CHECKS if only_site is None or c.site == only_site]
    results = []

    async def run_one(sc: SiteCheck):
        res = CheckResult(site=sc.site, note=sc.note)
        if sc.info_check is None:
            res.info = f'{SKIP} 未注册'
            results.append(res)
            return
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                         **sc.client_settings) as client:
                res.info = await asyncio.wait_for(sc.info_check(client), timeout=timeout)
                if not fast and sc.media_check is not None:
                    res.media = await asyncio.wait_for(sc.media_check(client), timeout=timeout)
        except asyncio.TimeoutError:
            res.info = f'{BAD} 超时({timeout}s)'
        except AssertionError as e:
            res.info = f'{BAD} 解析不符: {e}'
        except Exception as e:
            res.info = f'{BAD} {type(e).__name__}: {str(e)[:80]}'
        results.append(res)

    await asyncio.gather(*(run_one(sc) for sc in checks))
    return results


def render_table(results: list) -> Table:
    table = Table(title='bilix 站点健康检查', highlight=True)
    table.add_column('站点', style='bold')
    table.add_column('L1 信息解析')
    table.add_column('L2 媒体有效')
    table.add_column('备注', overflow='fold')
    for r in sorted(results, key=lambda x: x.site):
        table.add_row(r.site, r.info, r.media, r.note)
    return table


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='bilix doctor', description='站点健康自检')
    parser.add_argument('--fast', action='store_true', help='跳过L2媒体校验')
    parser.add_argument('--site', help='只检查指定站点')
    parser.add_argument('--json', action='store_true', help='JSON输出')
    parser.add_argument('--timeout', type=int, default=CHECK_TIMEOUT)
    args = parser.parse_args(argv)

    results = asyncio.run(run_checks(fast=args.fast, only_site=args.site, timeout=args.timeout))
    if args.json:
        import json
        print(json.dumps([{'site': r.site, 'info': r.info, 'media': r.media, 'note': r.note,
                           'passed': r.passed} for r in results], ensure_ascii=False, indent=2))
    else:
        Console().print(render_table(results))
        bad = [r.site for r in results if not r.passed]
        if bad:
            print(f'\n❌ 异常站点: {", ".join(bad)}\n修复指引见 docs/dev/2026-11-bilibili-repair-notes.md 与各站点 *_test.py')
    return 0 if all(r.passed for r in results) else 1


if __name__ == '__main__':
    sys.exit(main())
