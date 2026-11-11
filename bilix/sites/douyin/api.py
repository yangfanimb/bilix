"""
Originally From
@Author: https://github.com/Evil0ctal/
https://github.com/Evil0ctal/Douyin_TikTok_Download_API

Modified by
@Author: https://github.com/HFrost0/

2026-11 重写：旧 iteminfo 接口已失效，改用 iesdouyin 分享页 SSR（两步法，无需签名）。
流程：首次请求获取 ttwid cookie → 二次请求同URL获得 _ROUTER_DATA JSON。
详见 docs/dev/site-support-status.md 平台方案记录。
"""
import asyncio
import re
import json
from typing import List
import httpx
from pydantic import BaseModel
from bilix.utils import legal_title
from bilix.download.utils import req_retry, raise_api_error
from bilix.exception import APIUnsupportedError, APIResourceError

# 移动端UA是分享页返回SSR数据的关键（桌面UA会被风控挑战页拦截）
MOBILE_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
             'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1')

dft_client_settings = {
    'headers': {'user-agent': MOBILE_UA, 'Accept-Language': 'zh-CN,zh;q=0.9'},
    'http2': True
}


class VideoInfo(BaseModel):
    title: str
    author_name: str
    wm_urls: List[str]
    nwm_urls: List[str]
    cover: str
    dynamic_cover: str
    origin_cover: str


async def _get_aweme_item(client: httpx.AsyncClient, video_id: str) -> dict:
    """经 iesdouyin 分享页获取视频item数据（两步法：先取ttwid cookie再取SSR数据）

    该接口存在间歇性风控（返回空item_list或挑战页），对整条链路做多轮重试。
    """
    url = f'https://www.iesdouyin.com/share/video/{video_id}'
    kw = {'follow_redirects': True}
    await req_retry(client, url, **kw)  # 第一次：响应 Set-Cookie 下发 ttwid，SSR数据为空属正常

    def parse(text: str):
        m = re.search(r'window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>', text, re.S)
        if not m:
            raise APIUnsupportedError('分享页未含_ROUTER_DATA（可能被风控或页面结构变更）', url)
        data = json.loads(m.group(1))
        items = []  # loaderData 的key形如 "video_(id)/page"，遍历取值避免key模板变化导致解析失效
        for v in data.get('loaderData', {}).values():
            if not isinstance(v, dict):
                continue
            vir = v.get('videoInfoRes') or {}
            items = vir.get('item_list') or [] if isinstance(vir, dict) else []
            if items:
                break
        return items

    last_err = None
    for attempt in range(3):
        try:
            res = await req_retry(client, url, **kw)
            items = parse(res.text)
            if items:
                return items[0]
            last_err = APIResourceError('接口返回空数据（间歇性风控）', video_id)
        except APIUnsupportedError as e:
            last_err = e  # 挑战页/结构临时异常，同样值得重试
        await asyncio.sleep(1.5 * (attempt + 1))
    raise last_err or APIResourceError('未获取到视频数据', video_id)


@raise_api_error
async def get_video_info(client: httpx.AsyncClient, url: str) -> VideoInfo:
    if short_url := re.findall(r'https://v.douyin.com/\w+/', url):
        res = await req_retry(client, short_url[0], follow_redirects=True)
        url = str(res.url)
    if key := re.search(r'/video/(\d+)', url):
        key = key.groups()[0]
    else:
        key = re.search(r"modal_id=(\d+)", url).groups()[0]
    item = await _get_aweme_item(client, key)
    # 视频标题
    title = legal_title(item.get('desc') or item.get('share_info', {}).get('share_title', key))
    # 视频作者昵称
    author_name = item['author']['nickname']
    # 有水印视频链接
    wm_urls = item['video']['play_addr']['url_list']
    # 无水印视频链接 (将'playwm'替换为'play'即可获得无水印地址，302到douyinvod CDN)
    nwm_urls = list(map(lambda x: x.replace('/playwm/', '/play/'), wm_urls))
    # 视频封面
    cover = item['video']['cover']['url_list'][0]
    # 视频动态封面/原始封面（部分帖子缺失时回退到静态封面）
    dynamic_cover = item['video'].get('dynamic_cover', {}).get('url_list', [cover])[0]
    origin_cover = item['video'].get('origin_cover', {}).get('url_list', [cover])[0]
    video_info = VideoInfo(title=title, author_name=author_name, wm_urls=wm_urls, nwm_urls=nwm_urls, cover=cover,
                           dynamic_cover=dynamic_cover, origin_cover=origin_cover)
    return video_info


if __name__ == '__main__':
    async def main():
        client = httpx.AsyncClient(**dft_client_settings)
        data = await get_video_info(client, 'https://www.douyin.com/video/7132430286415252773')
        print(data)


    asyncio.run(main())
