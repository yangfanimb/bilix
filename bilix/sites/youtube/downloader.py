import re
import asyncio
from pathlib import Path
from typing import Union
import httpx
from . import api
from bilix.download.base_downloader_m3u8 import BaseDownloaderM3u8
from bilix.download.base_downloader_part import BaseDownloaderPart
from bilix import ffmpeg


class DownloaderYoutube(BaseDownloaderM3u8, BaseDownloaderPart):
    """多继承：get_file(直链) + get_m3u8_video(HLS分轨) 两种下载管线"""

    pattern = re.compile(r"^https?://([A-Za-z0-9-]+\.)*(youtube\.com|youtu\.be)")

    def __init__(
            self,
            *,
            client: httpx.AsyncClient = None,
            browser: str = None,
            speed_limit: Union[float, int] = None,
            stream_retry: int = 5,
            progress=None,
            logger=None,
            part_concurrency: int = 10,
            # unique params
            video_concurrency: Union[int, asyncio.Semaphore] = 3
    ):
        client = client or httpx.AsyncClient(**api.dft_client_settings)
        super().__init__(
            client=client,
            browser=browser,
            speed_limit=speed_limit,
            stream_retry=stream_retry,
            progress=progress,
            logger=logger,
            part_concurrency=part_concurrency,
            video_concurrency=video_concurrency,
        )

    async def get_video(self, url: str, path=Path('.')):
        """
        :cli: short: v
        :param url
        :param path:
        :return:
        """
        async with self.v_sema:
            video_info = await api.get_video_info(self.client, url)
            video_path = path / (video_info.title + '.mp4')
            if video_path.exists():
                return self.logger.info(f'[green]已存在[/green] {video_path.name}')
            task_id = await self.progress.add_task(description=video_info.title, upper=True)
            if video_info.hls:
                # HLS分轨：分别下载视频/音频清单（m3u8管线自带分段下载与解密）
                # 中间文件必须带.mp4后缀：ffmpeg.concat需按扩展名推断输出容器格式
                path_lst = await asyncio.gather(
                    self.get_m3u8_video(m3u8_url=video_info.video_url, path=path / (video_info.title + '-v.mp4')),
                    self.get_m3u8_video(m3u8_url=video_info.audio_url, path=path / (video_info.title + '-a.mp4')),
                )
            elif video_info.audio_url:
                path_lst = await asyncio.gather(
                    self.get_file(url_or_urls=video_info.video_url, path=path / (video_info.title + '-v'), task_id=task_id),
                    self.get_file(url_or_urls=video_info.audio_url, path=path / (video_info.title + '-a'), task_id=task_id)
                )
            else:
                # muxed 单文件（音视频合一），无需合并
                part = await self.get_file(url_or_urls=video_info.video_url,
                                           path=path / (video_info.title + '-v'), task_id=task_id)
                await self.progress.update(task_id, visible=False)
                part.rename(video_path)
                self.logger.info(f'[cyan]已完成[/cyan] {video_path.name}')
                return video_path
        await ffmpeg.combine(path_lst, output_path=video_path)
        self.logger.info(f'[cyan]已完成[/cyan] {video_path.name}')
        await self.progress.update(task_id=task_id, visible=False)
        return video_path
