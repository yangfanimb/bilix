import asyncio
import json
from typing import Tuple
from rich.tree import Tree
from .downloader import DownloaderBilibili
from . import api
from bilix.log import logger
from rich import print as rprint
from bilix.utils import convert_size
from bilix.download.utils import req_retry
from bilix.cli.assign import kwargs_filter


class InformerBilibili(DownloaderBilibili):
    """A special downloader with functionality to log info of bilibili resources"""

    @classmethod
    def parse_url(cls, url: str):
        res = super().parse_url(url)
        func_name = res.__name__.replace("get_", "info_")
        return getattr(cls, func_name)

    async def info_key(self, key):
        await self.parse_url(key)(self, key)

    async def info_up(self, url: str):
        up_name, total_size, bvids = await api.get_up_video_info(self.client, url)
        rprint(up_name)

    async def info_favour(self, url: str):
        pass

    async def info_collect_or_list(self, url: str):
        pass

    async def info_video(self, url: str):
        video_info = await api.get_video_info(self.client, url)
        if video_info.dash is None and video_info.other is None:
            return logger.warning(f'{video_info.title} 需要大会员或该地区不支持')
        elif video_info.other and video_info.dash is None:
            return rprint(video_info.other)  # todo: beautify durl info

        async def ensure_size(m: api.Media):
            if m.size is None:
                res = await req_retry(self.client, m.base_url, method='GET', headers={'Range': 'bytes=0-1'})
                m.size = int(res.headers['Content-Range'].split('/')[-1])

        dash = video_info.dash
        cors = [ensure_size(m) for m in dash.videos] + [ensure_size(m) for m in dash.audios]
        await asyncio.gather(*cors)

        # 当前账号状态：决定实际可下载的内容范围（流列表即当前账号可获取的）
        try:
            nav = json.loads((await req_retry(self.client, 'https://api.bilibili.com/x/web-interface/nav')).text)
            d = nav.get('data') or {}
            acc = f"已登录 {d.get('uname', '?')}" + ('（大会员）' if d.get('vipStatus') else '') \
                if d.get('isLogin') else '未登录（会员画质/内容不可下载）'
        except Exception as e:
            acc = f'未知({type(e).__name__})'

        # 字幕轨（当前分P）：标明中英文字幕有无
        sub_desc = '获取失败'
        try:
            subs = await api.get_subtitle_info(self.client, video_info.bvid, video_info.cid)
            names = [n for _, n in subs]
            has_zh = any('中' in n or 'zh' in n.lower() for n in names)
            has_en = any('英' in n or 'english' in n.lower() for n in names)
            sub_desc = (f"中文:{'有' if has_zh else '无'} 英文:{'有' if has_en else '无'}"
                        f" | 共{len(names)}轨: {', '.join(names) if names else '无字幕轨'}")
        except Exception as e:
            sub_desc = f'获取失败({type(e).__name__})'

        tree = Tree(
            f"[bold reverse] {video_info.title}-{video_info.pages[video_info.p].p_name} [/]"
            f" {video_info.status.view:,}👀 {video_info.status.like:,}👍 {video_info.status.coin:,}🪙"
            f"  [dim]账号: {acc}[/]",
            guide_style="bold cyan")
        video_tree = tree.add("[bold]画面 Video")
        audio_tree = tree.add("[bold]声音 Audio")
        leaf_fmt = "codec: {codec:32} size: {size}"
        # for video
        for qi, (quality, codecs) in enumerate(dash.video_formats.items()):
            res_str = ''
            for m in codecs.values():  # 同一画质下分辨率一致，取第一个标注
                if m.width and m.height:
                    res_str = f" ({m.width}×{m.height})"
                break
            p_tree = video_tree.add(f"{quality}{res_str}" + (" [reverse]最高[/]" if qi == 0 else ""))
            for c in codecs:
                m = codecs[c]
                p_tree.add(leaf_fmt.format(codec=m.codec, size=convert_size(m.size)))
            if len(p_tree.children) == 0:
                p_tree.style = "rgb(242,93,142)"
                p_tree.add("需要登录或大会员")
        # for audio
        name_map = {"default": "默认音质", "dolby": "杜比全景声 Dolby", "flac": "Hi-Res无损"}
        for k in dash.audio_formats:
            sub_tree = audio_tree.add(name_map[k])
            if m := dash.audio_formats[k]:
                sub_tree.add(leaf_fmt.format(codec=m.codec, size=convert_size(m.size)))
            else:
                sub_tree.style = "rgb(242,93,142)"
                sub_tree.add("需要登录或大会员")
        # 字幕
        sub_tree = tree.add(f"[bold]字幕 Subtitle（{sub_desc}）")
        if sub_desc.startswith(('无字幕轨', '获取失败')):
            sub_tree.style = "rgb(242,93,142)"
        rprint(tree)

    @classmethod
    def handle(cls, method: str, keys: Tuple[str, ...], options: dict):
        if cls.pattern.match(keys[0]) and 'info' == method:
            informer = InformerBilibili(sess_data=options['cookie'], **kwargs_filter(cls, options))

            # in order to maintain order
            async def temp():
                for key in keys:
                    if len(keys) > 1:
                        logger.info(f"For {key}")
                    await informer.info_key(key)

            return informer, temp()
