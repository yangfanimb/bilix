#!/usr/bin/env python3
"""bx.py —— bilix 便捷启动器

把 bilix CLI 包装得更顺手：cookie 只需配置一次，之后自动注入每次下载；
支持从浏览器一键导入 cookie、登录状态检查、默认下载目录/画质记忆。

用法:
  python bx.py login [--browser chrome | --sessdata <SESSDATA>]
                            保存/更新B站cookie（交互式粘贴，或从浏览器读取），自动校验
  python bx.py whoami       查看当前登录状态与会员信息
  python bx.py logout       清除已保存的cookie
  python bx.py config [-d 下载目录] [-q 画质] [--reset]
                            设置/查看默认下载目录与画质
  python bx.py <方法> <key> [bilix原有选项...]
                            执行下载，自动注入cookie与默认目录

方法可用简写: v=视频, s=系列, up=UP主投稿, fav=收藏夹, cate=分区, col=合集/列表,
              info=资源详情；直接传b站url时自动识别类型。

示例:
  python bx.py login                          # 首次配置cookie
  python bx.py v "https://www.bilibili.com/video/BV1GJ411x7h7"
  python bx.py "https://space.bilibili.com/946974" -n 5   # url自动识别
  python bx.py up 946974 -n 5 --keyword 教程
  python bx.py s "https://www.bilibili.com/bangumi/play/ss48267" -p 1 3
  python bx.py cate 连载动画 -n 10             # 临时覆盖画质: -q 1080p

说明:
  - 配置保存在 ~/.bilix/config.json（含SESSDATA，注意不要泄露该文件）
  - SESSDATA 获取方式: 浏览器登录b站 → F12 → 应用/Storage → Cookies → 复制 SESSDATA 的值
  - 显式传入 --cookie / -fb 时以命令行为准，忽略已保存配置
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

CONFIG_DIR = Path.home() / '.bilix'
CONFIG_FILE = CONFIG_DIR / 'config.json'
NAV_API = 'https://api.bilibili.com/x/web-interface/nav'
DFT_DIR = 'videos'

METHOD_ALIASES = {
    'v': 'get_video', 'video': 'get_video',
    's': 'get_series', 'series': 'get_series',
    'up': 'get_up',
    'fav': 'get_favour', 'favour': 'get_favour',
    'cate': 'get_cate',
    'col': 'get_collect', 'collect': 'get_collect',
    'info': 'info',
    'auto': 'auto', 'a': 'auto',
}
MANAGE_CMDS = {'login', 'logout', 'whoami', 'config', 'help', '-h', '--help'}
BROWSER_CHOICES = ['chrome', 'edge', 'firefox', 'safari', 'opera', 'brave', 'chromium', 'vivaldi']


# ---------- 配置 ----------

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            print(f'⚠️  配置文件损坏，已忽略: {CONFIG_FILE}')
    return {}


def save_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        os.chmod(CONFIG_FILE, 0o600)  # 含cookie，仅本人可读
    except OSError:
        pass


# ---------- cookie 获取与校验 ----------

def sessdata_from_browser(browser: str):
    """从浏览器cookie库中提取b站的SESSDATA"""
    try:
        import browser_cookie3
    except ImportError:
        raise RuntimeError('缺少 browser_cookie3 依赖，请先 pip install browser_cookie3')
    fn = getattr(browser_cookie3, browser.lower(), None)
    if fn is None:
        raise RuntimeError(f'不支持的浏览器 {browser}，可选: {", ".join(BROWSER_CHOICES)}')
    print(f'⏳ 正在从 {browser} 读取cookie（若浏览器正在运行或需要授权，可能较慢或失败）...')
    try:
        jar = fn(domain_name='bilibili.com')
    except Exception as e:
        raise RuntimeError(f'读取 {browser} cookie失败: {e}\n'
                           f'   可改用交互式粘贴：python bx.py login')
    for c in jar:
        if c.name == 'SESSDATA':
            return c.value
    return None


def check_sessdata(sessdata: str) -> dict:
    """调用nav接口校验SESSDATA，返回用户信息；未登录/无效时抛异常"""
    import httpx
    headers = {'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0',
               'referer': 'https://www.bilibili.com'}
    r = httpx.get(NAV_API, headers=headers, cookies={'SESSDATA': sessdata}, timeout=10)
    data = r.json().get('data') or {}
    if not data.get('isLogin'):
        raise RuntimeError('SESSDATA 无效或已过期（未登录）。请重新复制最新的SESSDATA。')
    return data


def fmt_user_info(data: dict) -> str:
    vip = '是' if data.get('vipStatus') == 1 else '否'
    if data.get('vipStatus') == 1 and data.get('vipDueDate'):
        due = datetime.fromtimestamp(data['vipDueDate'] / 1000).strftime('%Y-%m-%d')
        vip = f'是（{data.get("vip_label", "大会员")}，到期 {due}）'
    level = (data.get('level_info') or {}).get('current_level', '?')
    return (f'用户名: {data.get("uname")}\n'
            f'  等级: Lv{level}    大会员: {vip}    硬币: {data.get("money", "?")}')


# ---------- 子命令 ----------

def cmd_login(argv):
    sessdata, browser = None, None
    if '--browser' in argv:
        i = argv.index('--browser')
        browser = argv[i + 1] if i + 1 < len(argv) else None
        if not browser:
            sys.exit('错误: --browser 需要指定浏览器名，可选: ' + ', '.join(BROWSER_CHOICES))
        sessdata = sessdata_from_browser(browser)
        if not sessdata:
            sys.exit(f'错误: 在 {browser} 的cookie中未找到b站SESSDATA（是否登录过b站？）')
    elif '--sessdata' in argv:
        i = argv.index('--sessdata')
        if i + 1 >= len(argv):
            sys.exit('错误: --sessdata 后面要跟SESSDATA的值')
        sessdata = argv[i + 1]
    else:
        print('请粘贴你的SESSDATA（获取方式：浏览器登录b站 → F12 → 应用/Storage → Cookies → 复制SESSDATA的值）')
        print('直接回车可从浏览器导入。')
        try:
            sessdata = input('SESSDATA: ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)
        if not sessdata:
            print('未粘贴，尝试从浏览器导入。支持的浏览器: ' + ', '.join(BROWSER_CHOICES))
            browser = input(f'浏览器名[{BROWSER_CHOICES[0]}]: ').strip() or BROWSER_CHOICES[0]
            sessdata = sessdata_from_browser(browser)
            if not sessdata:
                sys.exit(f'错误: 在 {browser} 中未找到SESSDATA（是否登录过b站？）')

    print('⏳ 校验中...')
    try:
        data = check_sessdata(sessdata)
    except RuntimeError as e:
        sys.exit(f'❌ {e}')
    cfg = load_config()
    cfg.update(sessdata=sessdata, uname=data.get('uname', ''),
               saved_at=datetime.now().strftime('%Y-%m-%d %H:%M'))
    save_config(cfg)
    print('✅ cookie已保存到 ' + str(CONFIG_FILE))
    print(fmt_user_info(data))


def cmd_whoami():
    cfg = load_config()
    sess = cfg.get('sessdata')
    if not sess:
        print('当前未配置cookie。先执行: python bx.py login')
        return
    print(f'配置文件: {CONFIG_FILE}（保存于 {cfg.get("saved_at", "?")}，用户: {cfg.get("uname", "?")}）')
    print('⏳ 校验中...')
    try:
        data = check_sessdata(sess)
    except RuntimeError as e:
        print(f'❌ {e}')
        print('   重新配置: python bx.py login')
        return
    print('✅ 登录状态有效')
    print(fmt_user_info(data))


def cmd_logout():
    cfg = load_config()
    if not cfg:
        print('当前没有已保存的配置。')
        return
    for k in ('sessdata', 'uname', 'saved_at'):
        cfg.pop(k, None)
    save_config(cfg)
    print('✅ 已清除cookie（下载目录/画质等设置保留）。')


def cmd_config(argv):
    cfg = load_config()
    if '--reset' in argv:
        cfg.pop('dir', None)
        cfg.pop('quality', None)
    if '-d' in argv:
        if len(argv) <= argv.index('-d') + 1:
            sys.exit('错误: -d 后面要跟下载目录')
        cfg['dir'] = argv[argv.index('-d') + 1]
    if '-q' in argv:
        if len(argv) <= argv.index('-q') + 1:
            sys.exit('错误: -q 后面要跟画质（如 1080p）')
        cfg['quality'] = argv[argv.index('-q') + 1]
    if '--reset' in argv or '-d' in argv or '-q' in argv:
        save_config(cfg)
    print(f'当前配置（{CONFIG_FILE}）:')
    print(f'  cookie: {"已配置" if cfg.get("sessdata") else "未配置（python bx.py login）"}')
    print(f'  下载目录: {cfg.get("dir", DFT_DIR + "（默认）")}')
    print(f'  画质: {cfg.get("quality", "0（最高，默认）")}')
    if argv and not any(x in argv for x in ('-d', '-q', '--reset')):
        print('提示: 修改用 -d <目录> / -q <画质>，重置用 --reset')


def print_help():
    print(__doc__.strip())


# ---------- 下载转发 ----------

def build_bilix_args(argv: list, cfg: dict) -> list:
    """把 bx.py 的参数翻译成 bilix CLI 的参数，注入cookie与默认配置"""
    args = list(argv)
    # 方法别名归一化；首参为b站url时自动识别
    if args and args[0].startswith('http'):
        args.insert(0, 'auto')
    elif args and args[0] in METHOD_ALIASES:
        args[0] = METHOD_ALIASES[args[0]]
    # cookie注入：用户显式指定时以用户为准
    cookie_given = any(a in ('--cookie',) or a.startswith('--cookie=') for a in args) \
        or any(a in ('-fb', '--from-browser') or a.startswith('--from-browser=') for a in args)
    if cfg.get('sessdata') and not cookie_given:
        args += ['--cookie', cfg['sessdata']]
    elif not cookie_given:
        print('💡 提示: 未配置cookie，部分内容（大会员视频/收藏夹/UP列表）可能无法访问。配置: python bx.py login')
    # 默认下载目录/画质
    if not any(a in ('-d', '--dir') or a.startswith('--dir=') for a in args) and cfg.get('dir'):
        args += ['-d', cfg['dir']]
    if not any(a in ('-q', '--quality') or a.startswith('--quality=') for a in args) and cfg.get('quality'):
        args += ['-q', cfg['quality']]
    return args


def restore_terminal():
    """子进程退出后兜底恢复终端（光标可见+回到行首），保证shell能正常显示提示符与输入。
    覆盖子进程被强杀（SIGKILL）、异常退出等bilix自身无法恢复的场景"""
    if not sys.stdout.isatty():
        return
    try:
        sys.stdout.write('\r\x1b[?25h')
        sys.stdout.flush()
    except OSError:
        pass


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print_help()
        return
    cmd = argv[0]
    if cmd == 'login':
        cmd_login(argv[1:])
    elif cmd == 'whoami':
        cmd_whoami()
    elif cmd == 'logout':
        cmd_logout()
    elif cmd == 'config':
        cmd_config(argv[1:])
    else:
        cfg = load_config()
        args = build_bilix_args(argv, cfg)
        try:
            code = subprocess.call([sys.executable, '-m', 'bilix', *args])
        finally:
            restore_terminal()
        sys.exit(code)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n已取消。')
