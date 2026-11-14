"""bx.py 全平台全功能遍历测试矩阵（回归工具）

用法: python tools/bx_matrix.py [名称过滤]
说明: 顺序执行28个用例（全部平台×主要功能+预期失败路径），验证下载产物与报错行为。
      结果实时写入 /tmp/bx_matrix/results.json。
注意: ①各用例产物较大（youtube约360MB），注意 /tmp 配额；②douyin/tiktok 接口存在
      间歇性风控，单用例失败请先重试并参考 docs/dev/site-support-status.md 的已知限制。
"""

import json
import subprocess
import sys
import shutil
import time
from pathlib import Path

BX = ['/opt/workspace/english_books/.venv/bin/python', '/opt/workspace/bilix/src/bx.py']
BASE = Path('/tmp/bx_matrix')
BILI_V = 'https://www.bilibili.com/video/BV1GJ411x7h7'
BILI_MULTI = 'https://www.bilibili.com/video/BV1Tdxt6fEQS'
DY_V = 'https://www.douyin.com/video/7132430286415252773'
TT_V = 'https://www.tiktok.com/@lindaselection/video/7171715528124271877'
YT_V = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
CCTV_V = 'https://tv.cctv.com/2012/05/02/VIDE1355968282695723.shtml'

# (name, bx args, timeout_s, validator)
# validator: ('files', glob, min_size) | ('contains', text) | ('graceful_fail', error_hint)
CASES = [
    # ---- A. bilibili（cookie 由 bx 自动注入）----
    ('bili-video-basic',      ['v', BILI_V], 180, ('files', '*.mp4', 500_000)),
    ('bili-video-sub-DM-img', ['v', BILI_V, '--subtitle', '--dm', '--image'], 240, ('files', '*.srt', 100)),
    ('bili-video-only-audio', ['v', BILI_V, '-oa'], 180, ('files', '*.m4a', 500_000)),
    ('bili-video-q1080p',     ['v', BILI_V, '-q', '1080p'], 180, ('files', '*.mp4', 500_000)),
    ('bili-video-codec-hev',  ['v', BILI_V, '--codec', 'hev'], 180, ('files', '*.mp4', 500_000)),
    ('bili-video-anonymous',  ['v', BILI_V, '--cookie', ''], 180, ('files', '*.mp4', 500_000)),
    ('bili-series-multip-P1-2', ['s', BILI_MULTI, '-p', '1', '2'], 280, ('files', '*.mp4', 500_000)),
    ('bili-series-bangumi',   ['s', 'https://www.bilibili.com/bangumi/play/ss48267', '-p', '1', '1'], 280, ('files', '*.mp4', 100_000)),
    ('bili-up-url',           ['up', 'https://space.bilibili.com/946974', '-n', '1'], 240, ('files', '*.mp4', 500_000)),
    ('bili-up-bare-id',       ['up', '946974', '-n', '1'], 240, ('files', '*.mp4', 500_000)),
    ('bili-fav-bare-id',      ['fav', '69370754', '-n', '1'], 240, ('files', '*.mp4', 100_000)),
    ('bili-cate-no-keyword',  ['cate', '连载动画', '-n', '1'], 240, ('files', '*.mp4', 100_000)),
    ('bili-cate-keyword',     ['cate', '连载动画', '--keyword', '原创', '-n', '1'], 240, ('files', '*.mp4', 100_000)),
    ('bili-collect',          ['col', 'https://space.bilibili.com/546195/channel/collectiondetail?sid=223180', '-n', '1'], 240, ('files', '*.mp4', 100_000)),
    ('bili-info',             ['info', BILI_V], 120, ('contains', 'Audio')),
    ('bili-autourl',          [BILI_V], 180, ('files', '*.mp4', 500_000)),
    # ---- B. douyin ----
    ('douyin-video',          ['v', DY_V], 200, ('files', '*.mp4', 500_000)),
    ('douyin-image',          ['v', DY_V, '--image'], 200, ('files', '*.webp', 1000)),
    ('douyin-autourl',        [DY_V], 200, ('files', '*.mp4', 500_000)),
    # ---- C. tiktok ----
    ('tiktok-video',          ['v', TT_V], 200, ('files', '*.mp4', 300_000)),
    ('tiktok-autourl',        [TT_V], 200, ('files', '*.mp4', 300_000)),
    # ---- D. youtube ----
    ('youtube-video',         ['v', YT_V], 280, ('files', '*.mp4', 1_000_000)),
    ('youtube-short-url',     ['https://youtu.be/dQw4w9WgXcQ'], 280, ('files', '*.mp4', 1_000_000)),
    # ---- E. cctv ----
    ('cctv-video',            ['v', CCTV_V], 240, ('files', '*.mp4', 100_000)),
    ('cctv-autourl',          [CCTV_V], 240, ('files', '*.mp4', 100_000)),
    # ---- F. jable（此IP可能遇Cloudflare临时403）----
    ('jable-video',           ['v', 'https://jable.tv/videos/ssis-448/'], 200, ('files', '*.mp4', 1_000_000)),
    # ---- G. 预期失败路径（验证报错优雅不崩溃）----
    ('x-hanime1-cloudflare',  ['v', 'https://hanime1.me/watch?v=13658'], 200, ('graceful_fail', '403')),
    ('x-yhdmp-dead-site',     ['v', 'https://www.yhdmp.cc/vp/22224-1-0.html'], 120, ('graceful_fail', '')),
]


def validate(case, outdir: Path, rc: int, output: str):
    kind = case[3]
    if kind[0] == 'files':
        hits = [p for p in outdir.rglob(kind[1]) if p.stat().st_size >= kind[2]]
        return ('PASS' if hits else 'FAIL'), (f'{hits[0].stat().st_size}B {hits[0].name[:40]}' if hits else '产物缺失')
    if kind[0] == 'contains':
        return ('PASS' if kind[1] in output else 'FAIL'), ('包含特征' if kind[1] in output else f'缺少 {kind[1]}')
    if kind[0] == 'graceful_fail':
        # 预期失败：报错包含提示 or 非零退出，且不是未处理崩溃段错误
        bad = ('Segmentation' in output)
        hint = kind[1] in output if kind[1] else True
        ok = (not bad) and (hint or ("Can't find any handler" in output) or ('not available' in output))
        tail = output.strip().splitlines()[-1][:60] if output.strip() else 'no output'
        return ('PASS' if ok else 'FAIL'), f'预期失败: rc={rc} {tail}'


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    results = []
    for case in CASES:
        name, args, timeout, _ = case
        if only and only not in name:
            continue
        outdir = BASE / name
        shutil.rmtree(outdir, ignore_errors=True)
        outdir.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        try:
            proc = subprocess.run(BX + args, capture_output=True, text=True,
                                  timeout=timeout, cwd=str(outdir),
                                  env={'PATH': '/home/sc/.pyenv/versions/3.12.0/bin:/usr/bin:/bin',
                                       'HOME': '/home/sc', 'TERM': 'xterm-256color',
                                       'LANG': 'C.UTF-8'})
            rc, output = proc.returncode, (proc.stdout + proc.stderr)
        except subprocess.TimeoutExpired as e:
            rc, output = -9, f'TIMEOUT {e}'
        dur = time.time() - t0
        status, evidence = validate(case, outdir, rc, output)
        results.append({'name': name, 'status': status, 'dur': round(dur), 'evidence': evidence})
        print(f"[{status}] {name:26s} {dur:6.1f}s  {evidence}", flush=True)
        (BASE / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding='utf-8')

    ok = sum(1 for r in results if r['status'] == 'PASS')
    print(f"\n===== 总计 {ok}/{len(results)} PASS =====", flush=True)


if __name__ == '__main__':
    main()
