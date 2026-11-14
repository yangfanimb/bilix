import asyncio
import re
from pathlib import Path
from typing import Union
import httpx
from . import api
from bilix.download.base_downloader_part import BaseDownloaderPart
from bilix.utils import legal_title


class DownloaderDouyin(BaseDownloaderPart):
    pattern = re.compile(r"^https?://([A-Za-z0-9-]+\.)*(douyin\.com)")

    def __init__(
            self,
            *,
            client: httpx.AsyncClient = None,
            browser: str = None,
            speed_limit: Union[float, int, None] = None,
            stream_retry: int = 5,
            progress=None,
            logger=None,
            part_concurrency: int = 10,
            audio_format: str = 'm4a',
    ):
        client = client or httpx.AsyncClient(**api.dft_client_settings)
        super(DownloaderDouyin, self).__init__(
            client=client,
            browser=browser,
            speed_limit=speed_limit,
            stream_retry=stream_retry,
            progress=progress,
            logger=logger,
            part_concurrency=part_concurrency,
            audio_format=audio_format,
        )

    async def get_video(self, url: str, path=Path('.'), image=False, only_audio=False):
        """
        :cli: short: v
        :param url:
        :param path:
        :param image:
        :return:
        """
        video_info = await api.get_video_info(self.client, url)
        title = legal_title(video_info.author_name, video_info.title)
        cors = [self.get_file(video_info.nwm_urls, path=path / f"{title}.mp4")]
        if image:
            cors.append(self.get_static(video_info.cover, path / title))
        await asyncio.gather(*cors)
        if only_audio:  # 仅音频：从成品视频提取音轨
            await self.extract_audio(path / f"{title}.mp4")
