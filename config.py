# -*- coding: utf-8 -*-
"""用户配置：存在用户目录下的 ``~/.sharefiles/config.json``。

首次运行会自动建好这个文件，之后每次启动读回来，改设置时写回去。
读配置处处容错 —— 文件被删了、被手改坏了、字段类型不对，都退回默认值继续跑，
不能因为一个配置文件让程序起不来。
"""

from __future__ import annotations

import json
import os

VERSION = 1

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".sharefiles")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULTS = {
    "version": VERSION,
    "ip": "",            # 空 = 启动时按网卡自动挑一个
    "port": 8000,
    "preview": False,    # 是否允许浏览器直接预览
    "password": "",
    "limit": 0,          # 每个文件的下载次数上限，0 = 不限
    "blocked": [],       # IP / 网段黑名单
    "close_action": "",  # 关窗口时干什么："" = 每次问，"tray" = 收进托盘，"quit" = 退出
    "ball_x": None,      # 悬浮球停靠位置（屏幕坐标），None = 未拖动过，用默认位置
    "ball_y": None,
    "ball_enabled": True,   # 收进托盘后是否在桌面显示悬浮球
}


def _clean(raw) -> dict:
    """把读到的内容规整成合法配置，认不出来的字段一律退回默认值。"""
    cfg = dict(DEFAULTS)
    if not isinstance(raw, dict):
        return cfg

    if isinstance(raw.get("ip"), str):
        cfg["ip"] = raw["ip"].strip()

    try:
        cfg["port"] = max(1024, min(65535, int(raw.get("port", DEFAULTS["port"]))))
    except (TypeError, ValueError):
        pass

    cfg["preview"] = bool(raw.get("preview"))

    if isinstance(raw.get("password"), str):
        cfg["password"] = raw["password"]

    try:
        cfg["limit"] = max(0, int(raw.get("limit", 0)))
    except (TypeError, ValueError):
        pass

    blocked = raw.get("blocked")
    if isinstance(blocked, list):
        cfg["blocked"] = [str(b) for b in blocked if str(b).strip()]

    # 只认这两个值，别的一律当"没记住"，下次接着问
    if raw.get("close_action") in ("tray", "quit"):
        cfg["close_action"] = raw["close_action"]

    # 悬浮球坐标：两个都必须是整数才有效，缺一个即视为未记录
    for axis in ("ball_x", "ball_y"):
        value = raw.get(axis)
        # bool 是 int 的子类，需先排除，否则 True 会被当作合法坐标 1
        if isinstance(value, int) and not isinstance(value, bool):
            cfg[axis] = value

    if cfg["ball_x"] is None or cfg["ball_y"] is None:
        cfg["ball_x"] = cfg["ball_y"] = None

    if "ball_enabled" in raw:
        cfg["ball_enabled"] = bool(raw["ball_enabled"])

    return cfg


def load() -> dict:
    """读配置；文件不存在或读不动都返回默认值，不抛异常。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return _clean(json.load(f))
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save(cfg: dict) -> bool:
    """写配置。写失败（比如目录没权限）返回 False，不抛异常。"""
    data = _clean(cfg)
    data["version"] = VERSION
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(tmp, 0o600)      # 里面存着密码，只给自己看
        except OSError:
            pass                       # Windows 上这个基本不起作用，忽略
        os.replace(tmp, CONFIG_PATH)   # 原子替换，免得写一半留下个坏文件
        return True
    except (OSError, ValueError):
        # ValueError 也要接住：路径里有非法字符时 os.makedirs 抛的是它，
        # 漏掉的话这个函数"失败只返回 False"的约定就破了
        return False


def load_or_create() -> dict:
    """首次运行把配置文件建出来，之后按内容读取。"""
    if not os.path.exists(CONFIG_PATH):
        cfg = dict(DEFAULTS)
        save(cfg)
        return cfg
    return load()
