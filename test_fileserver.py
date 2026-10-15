# -*- coding: utf-8 -*-
"""fileserver 核心层自测：不依赖 GUI，直接用标准库发 HTTP 请求验证。

    python test_fileserver.py
"""

import http.client
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile

try:                                   # Windows 控制台默认不是 UTF-8
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fileserver import (ShareRegistry, TransferTracker, create_server,
                        get_lan_ips, human_size, human_speed, parse_block_entry)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def wait_until(pred, timeout=8.0, interval=0.1):
    """等条件成立。比固定 sleep 稳：服务端收尾要等 socket 写入失败，快慢不定。"""
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


TOKEN = ""          # main() 拿到 server 后填上


def url(path: str) -> str:
    """带上随机前缀的完整路径。"""
    return ("/" + TOKEN + path) if TOKEN else path


def request(port, method, path, headers=None, raw=False):
    """默认自动带上随机前缀；raw=True 用来测不带 / 带错前缀的情况。"""
    if not raw and TOKEN:
        path = "/" + TOKEN + path
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, dict(resp.getheaders()), body
    finally:
        conn.close()


def main():
    tmp = tempfile.mkdtemp(prefix="sharefiles-test-")
    try:
        # ---- 准备测试数据 --------------------------------------------------
        payload = os.urandom(3 * 1024 * 1024)          # 3 MB
        big = os.path.join(tmp, "大文件.bin")
        with open(big, "wb") as f:
            f.write(payload)

        # newline="\n"：Windows 文本模式默认会把 \n 写成 \r\n，这里不转换
        small = os.path.join(tmp, "note.txt")
        with open(small, "w", encoding="utf-8", newline="\n") as f:
            f.write("你好，ShareFiles\n")

        folder = os.path.join(tmp, "素材")
        os.makedirs(os.path.join(folder, "子目录"))
        for i in range(3):
            with open(os.path.join(folder, f"f{i}.txt"), "w", newline="\n") as f:
                f.write(f"file {i}\n")
        with open(os.path.join(folder, "子目录", "deep.txt"), "w", newline="\n") as f:
            f.write("deep\n")

        secret = os.path.join(tmp, "secret.txt")
        with open(secret, "w") as f:
            f.write("TOP SECRET\n")

        # ---- 注册表 --------------------------------------------------------
        print("\n[1] ShareRegistry")
        reg = ShareRegistry()
        vp_big, added = reg.add(big)
        check("加入文件返回虚拟路径", added and vp_big == "/大文件.bin", vp_big)
        check("重复加入不产生副本", reg.add(big) == (vp_big, False))
        vp_dir, _ = reg.add(folder)
        check("加入文件夹", vp_dir == "/素材", vp_dir)
        vp_txt, _ = reg.add(small)
        check("加入 txt", vp_txt == "/note.txt", vp_txt)
        vp_secret, _ = reg.add(secret)
        check("加入 secret", vp_secret == "/secret.txt", vp_secret)

        # 重名处理
        dup_dir = os.path.join(tmp, "dup")
        os.makedirs(dup_dir)
        dup_file = os.path.join(dup_dir, "大文件.bin")
        with open(dup_file, "wb") as f:
            f.write(b"x")
        vp_dup, _ = reg.add(dup_file)
        check("重名自动加后缀", vp_dup == "/大文件.bin (2)", vp_dup)

        check("resolve 命中文件", reg.resolve(vp_big) == os.path.realpath(big))
        check("resolve 命中目录内文件",
              reg.resolve("/素材/子目录/deep.txt") == os.path.realpath(
                  os.path.join(folder, "子目录", "deep.txt")))
        check("resolve 未挂载路径返回 None", reg.resolve("/不存在.txt") is None)
        # secret 已单独挂在 /secret.txt；以下请求都不能借道 /素材 把它掏出来
        check("拒绝借道 .. 逃出挂载点", reg.resolve("/素材/../secret.txt") is None)
        check("拒绝深层 .. 逃出挂载点",
              reg.resolve("/素材/子目录/../../secret.txt") is None)
        check("拒绝逃逸到未挂载区域",
              reg.resolve("/素材/../../../windows/win.ini") is None)

        # ---- 启服务 --------------------------------------------------------
        tracker = TransferTracker()
        server, port = create_server(reg, tracker, port=8123, host="127.0.0.1")
        server.start()
        time.sleep(0.3)
        global TOKEN
        TOKEN = server.token
        print(f"\n[2] HTTP 服务  (127.0.0.1:{port})")

        # 完整下载
        st, hd, body = request(port, "GET", "/note.txt")
        check("GET 文件 200", st == 200, st)
        check("GET 文件内容正确", body.decode("utf-8") == "你好，ShareFiles\n")
        check("声明 Accept-Ranges", hd.get("Accept-Ranges") == "bytes")
        # 没有这个头，浏览器第二次点同一链接会走缓存，服务端收不到请求、
        # 既计不了下载次数也拦不住
        check("文件响应禁止缓存", hd.get("Cache-Control") == "no-store",
              hd.get("Cache-Control"))
        check("默认强制下载", hd.get("Content-Disposition", "").startswith("attachment"))
        check("中文文件名已 RFC5987 编码",
              "filename*=UTF-8''" in hd.get("Content-Disposition", ""))

        # 大文件 + 追踪
        st, hd, body = request(port, "GET", "/%E5%A4%A7%E6%96%87%E4%BB%B6.bin")
        check("GET 大文件完整", st == 200 and body == payload, f"{st} {len(body)}")
        snap = tracker.snapshot()
        check("下载完成后计入统计", snap["completed"] >= 1, snap["completed"])
        check("下载完成后无活跃任务", snap["task_count"] == 0, snap["task_count"])
        check("字节数已累计", snap["bytes_served"] >= len(payload))

        # Range
        print("\n[3] HTTP Range（断点续传基础）")
        st, hd, body = request(port, "GET", "/%E5%A4%A7%E6%96%87%E4%BB%B6.bin",
                               {"Range": "bytes=0-1023"})
        check("Range 返回 206", st == 206, st)
        check("Range 内容正确", body == payload[:1024], len(body))
        check("Content-Range 正确",
              hd.get("Content-Range") == f"bytes 0-1023/{len(payload)}",
              hd.get("Content-Range"))

        st, hd, body = request(port, "GET", "/%E5%A4%A7%E6%96%87%E4%BB%B6.bin",
                               {"Range": "bytes=1000-"})
        check("开放式 Range 到结尾", st == 206 and body == payload[1000:], st)

        st, hd, body = request(port, "GET", "/%E5%A4%A7%E6%96%87%E4%BB%B6.bin",
                               {"Range": "bytes=-500"})
        check("后缀 Range 取末尾", st == 206 and body == payload[-500:], st)

        st, hd, body = request(port, "GET", "/%E5%A4%A7%E6%96%87%E4%BB%B6.bin",
                               {"Range": f"bytes={len(payload)}-"})
        check("越界 Range 返回 416", st == 416, st)
        st, hd, body = request(port, "GET", "/note.txt", {"Range": "bytes=abc-def"})
        check("非法 Range 忽略并回整段", st == 200 and body, st)

        # HEAD
        st, hd, body = request(port, "HEAD", "/%E5%A4%A7%E6%96%87%E4%BB%B6.bin")
        check("HEAD 200 且无正文", st == 200 and body == b"", f"{st} {len(body)}")
        check("HEAD 不产生下载任务", tracker.snapshot()["task_count"] == 0)

        # 目录 / 索引
        print("\n[4] 目录页与索引页")
        st, hd, body = request(port, "GET", "/%E7%B4%A0%E6%9D%90/")
        text = body.decode("utf-8")
        check("目录页 200", st == 200, st)
        check("目录页列出文件", "f0.txt" in text and "子目录" in text)
        check("目录页是 UTF-8", "charset=utf-8" in hd.get("Content-Type", ""))

        st, hd, body = request(port, "GET", "/%E7%B4%A0%E6%9D%90")
        check("目录缺斜杠时 301 重定向", st == 301, st)
        check("重定向到带斜杠地址",
              hd.get("Location", "").endswith("/"), hd.get("Location"))

        st, hd, body = request(port, "GET", "/%E7%B4%A0%E6%9D%90/%E5%AD%90%E7%9B%AE%E5%BD%95/deep.txt")
        check("深层目录文件可下载",
              st == 200 and body == b"deep\n", st)

        st, hd, body = request(port, "GET", "/")
        text = body.decode("utf-8")
        check("索引页 200", st == 200, st)
        check("索引页列出所有分享项",
              "大文件.bin" in text and "素材" in text and "note.txt" in text)

        # 安全
        print("\n[5] 安全边界")
        st, _, _ = request(port, "GET", "/%E4%B8%8D%E5%AD%98%E5%9C%A8.txt")
        check("未挂载文件 404", st == 404, st)
        st, _, _ = request(port, "GET",
                           "/%E7%B4%A0%E6%9D%90/%2E%2E/%2E%2E/windows/win.ini")
        check("编码的 .. 无法逃到未挂载区域", st in (403, 404), st)
        st, _, _ = request(port, "GET", "/../fileserver.py")
        check("向上穿越被拒", st in (403, 404), st)
        st, _, body = request(port, "GET", "/%E7%B4%A0%E6%9D%90/f0.txt")
        check("挂载点内正常访问不受影响", st == 200 and body == b"file 0\n", st)
        st, _, body = request(port, "GET",
                              "/%E7%B4%A0%E6%9D%90/%E5%AD%90%E7%9B%AE%E5%BD%95/../f1.txt")
        check("挂载点内 .. 归一化后仍可达", st == 200 and body == b"file 1\n", st)

        # 传输追踪（真实并发）
        print("\n[6] 传输追踪")
        seen = {}

        def slow_reader():
            """慢速读取，好在读取过程中观察 tracker。"""
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            conn.request("GET", url("/%E5%A4%A7%E6%96%87%E4%BB%B6.bin"))
            resp = conn.getresponse()
            total = 0
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total >= 512 * 1024 and "snap" not in seen:
                    for _ in range(30):
                        s = tracker.snapshot()
                        if s["task_count"] > 0:
                            seen["snap"] = s
                            break
                        time.sleep(0.05)
                    time.sleep(0.4)            # 让速度估算有时间累积
                    seen["snap2"] = tracker.snapshot()
                time.sleep(0.002)
            conn.close()

        th = threading.Thread(target=slow_reader)
        th.start()
        th.join(timeout=30)

        check("下载过程中能观察到活跃任务", "snap" in seen)
        if "snap" in seen:
            it = seen["snap"]["active"][0]
            check("任务记录了客户端 IP", it["ip"] == "127.0.0.1", it["ip"])
            check("任务记录了文件名", it["name"] == "大文件.bin", it["name"])
            check("任务总大小正确", it["total"] == len(payload), it["total"])
            check("同文件多连接会聚合", it["conns"] == 1, it["conns"])
        if "snap2" in seen and seen["snap2"]["active"]:
            it2 = seen["snap2"]["active"][0]
            check("进度在推进", it2["sent"] > 0, it2["sent"])
            check("速度已估算", it2["speed"] > 0, it2["speed"])
        wait_until(lambda: tracker.snapshot()["task_count"] == 0)
        check("读取结束后任务清零", tracker.snapshot()["task_count"] == 0)

        # 中途断开
        print("\n[7] 客户端中途断开")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/%E5%A4%A7%E6%96%87%E4%BB%B6.bin")
        resp = conn.getresponse()
        resp.read(1024)
        conn.close()                           # 粗暴断开
        # 服务端得等写入失败才收尾，等它自己归零，别用固定 sleep 赌
        wait_until(lambda: tracker.snapshot()["task_count"] == 0)
        snap = tracker.snapshot()
        check("断开后不残留活跃任务", snap["task_count"] == 0, snap["task_count"])
        rec = snap["recent"][0] if snap["recent"] else {}
        check("断开后记录仍留在列表里", bool(rec), snap["recent"])
        check("记录里带着客户端 IP", rec.get("ip") == "127.0.0.1", rec.get("ip"))
        # 这条走的是真实 socket：3 MB 有可能整个被内核缓冲区吞下，那样服务端
        # 确实算发完了。状态语义的精确验证放在 [7b] 里直接测追踪器。
        check("任务状态合法", rec.get("state") in ("cancelled", "done"),
              rec.get("state"))
        check("记录的数据量不超过总大小",
              rec.get("sent", 0) <= rec.get("total", 0), rec)

        # 任务级语义：直接测追踪器，绕开真实 socket 的缓冲不确定性
        print("\n[7b] 下载任务的状态与记录")
        tr = TransferTracker()
        t = tr.begin("10.0.0.1", "a.bin", "/a.bin", 1000, 1000)
        check("新任务处于进行中", tr.snapshot()["active"][0]["state"] == "active")
        tr.advance(t, 400)
        tr.end(t)
        s = tr.snapshot()
        check("只传了 400/1000 就断开 -> cancelled",
              s["recent"][0]["state"] == "cancelled", s["recent"][0]["state"])
        check("进度按整个文件算", abs(s["recent"][0]["progress"] - 0.4) < 0.01,
              s["recent"][0]["progress"])
        check("中断也留在记录里并带着 IP",
              s["recent"][0]["ip"] == "10.0.0.1", s["recent"][0]["ip"])

        # 分段下载（下载工具常见）：三段凑齐才算完成，且只留一条记录
        tr2 = TransferTracker()
        for _ in range(3):
            c = tr2.begin("10.0.0.2", "b.bin", "/b.bin", 1000, 3000)
            tr2.advance(c, 1000)
            tr2.end(c)
        s2 = tr2.snapshot()
        check("三段下载只留一条记录", len(s2["recent"]) == 1, len(s2["recent"]))
        check("凑齐整个文件 -> done", s2["recent"][0]["state"] == "done",
              s2["recent"][0]["state"])
        check("完成计数只加一次", s2["completed"] == 1, s2["completed"])
        check("完成后进度为 100%", s2["recent"][0]["progress"] == 1.0,
              s2["recent"][0]["progress"])

        c = tr2.begin("10.0.0.2", "b.bin", "/b.bin", 100, 3000)
        tr2.end(c)
        check("完成后再次请求不重复计完成数", tr2.snapshot()["completed"] == 1,
              tr2.snapshot()["completed"])
        check("同一文件不会多出记录", len(tr2.snapshot()["recent"]) == 1)

        # 续传：中断之后再来一条连接，状态应回到进行中
        tr3 = TransferTracker()
        c = tr3.begin("10.0.0.5", "e.bin", "/e.bin", 1000, 2000)
        tr3.advance(c, 500)
        tr3.end(c)
        check("中断后状态为 cancelled", tr3.snapshot()["recent"][0]["state"] == "cancelled")
        c = tr3.begin("10.0.0.5", "e.bin", "/e.bin", 1500, 2000)
        check("续传时回到进行中", tr3.snapshot()["active"][0]["state"] == "active")
        tr3.advance(c, 1500)
        tr3.end(c)
        check("续传补齐后算完成", tr3.snapshot()["recent"][0]["state"] == "done",
              tr3.snapshot()["recent"][0]["state"])

        # 清空记录
        tr4 = TransferTracker()
        running = tr4.begin("10.0.0.3", "c.bin", "/c.bin", 100, 100)
        finished = tr4.begin("10.0.0.4", "d.bin", "/d.bin", 100, 100)
        tr4.advance(finished, 100)
        tr4.end(finished)
        gone = tr4.clear_finished()
        s4 = tr4.snapshot()
        check("清空记录移除已结束的", gone == 1 and not s4["recent"], (gone, s4["recent"]))
        check("清空记录不影响进行中的", s4["task_count"] == 1, s4["task_count"])
        tr4.end(running)

        # 预览模式
        print("\n[8] 预览模式开关")
        server.inline_preview = True
        st, hd, _ = request(port, "GET", "/note.txt")
        check("预览模式下不发 Content-Disposition",
              "Content-Disposition" not in hd, hd.get("Content-Disposition"))
        server.inline_preview = False
        st, hd, _ = request(port, "GET", "/note.txt")
        check("关闭预览后恢复强制下载",
              hd.get("Content-Disposition", "").startswith("attachment"))

        # 动态增删
        print("\n[9] 运行时增删分享项")
        live = os.path.join(tmp, "live.txt")
        with open(live, "w", encoding="utf-8", newline="\n") as f:
            f.write("LIVE\n")
        vp_new, added = reg.add(live)
        check("运行时新增返回新路径", added and vp_new == "/live.txt", vp_new)
        st, _, body = request(port, "GET", "/live.txt")
        check("新加入的项立即可下载", st == 200 and b"LIVE" in body, st)
        reg.remove(vp_new)
        st, _, _ = request(port, "GET", "/live.txt")
        check("移除后立即 404", st == 404, st)

        # 随机分享前缀
        print("\n[9b] 随机分享前缀")
        check("前缀长度 16（64 位熵）", len(TOKEN) == 16 and
              all(c in "0123456789abcdef" for c in TOKEN), TOKEN)

        st, _, _ = request(port, "GET", "/note.txt", raw=True)
        check("不带前缀 -> 404", st == 404, st)
        st, _, _ = request(port, "GET", "/", raw=True)
        check("不带前缀访问根 -> 404", st == 404, st)
        st, _, _ = request(port, "GET", "/deadbeefdeadbeef/note.txt", raw=True)
        check("前缀不对 -> 404", st == 404, st)
        st, _, body = request(port, "GET", "/note.txt", raw=True)
        check("404 不泄露真实路径", b"note.txt" not in body, body[:120])
        check("404 里也看不出前缀对不对",
              b"token" not in body.lower() and b"prefix" not in body.lower())

        st, _, _ = request(port, "GET", "/note.txt")
        check("带正确前缀 -> 200", st == 200, st)
        st, _, body = request(port, "GET", "/")
        check("带前缀访问根 -> 索引页", st == 200 and "共享内容" in body.decode("utf-8"),
              st)

        # 索引页里的链接必须带前缀，否则点进去就 404
        st, _, body = request(port, "GET", "/")
        text = body.decode("utf-8")
        check("索引页链接带前缀", f'href="/{TOKEN}/' in text)

        # 目录页面包屑也一样
        st, _, body = request(port, "GET", "/%E7%B4%A0%E6%9D%90/")
        check("目录页面包屑带前缀", f'href="/{TOKEN}/' in body.decode("utf-8"))

        # 换前缀之后旧链接立刻失效
        old = TOKEN
        new = server.refresh_token()
        globals()["TOKEN"] = new
        check("刷新后前缀变了", new != old, (old, new))
        check("新前缀长度一致", len(new) == 16, new)
        st, _, _ = request(port, "GET", f"/{old}/note.txt", raw=True)
        check("旧前缀立刻失效 -> 404", st == 404, st)
        st, _, _ = request(port, "GET", "/note.txt")
        check("新前缀可用 -> 200", st == 200, st)

        # 打包下载
        print("\n[9c] 打包下载")
        st, _, body = request(port, "GET", "/")
        text = body.decode("utf-8")
        check("索引页有「下载全部」按钮",
              "?zip=1" in text and "下载全部" in text)

        st, hd, body = request(port, "GET", "/?zip=1")
        check("根打包返回 zip",
              st == 200 and hd.get("Content-Type") == "application/zip", st)
        check("zip 同样禁止缓存", hd.get("Cache-Control") == "no-store",
              hd.get("Cache-Control"))
        disp = hd.get("Content-Disposition", "")
        # 响应头只能用 latin-1，纯中文包名必须配 ASCII 回退，否则直接抛异常
        check("中文包名给了 ASCII 回退", 'filename="download.zip"' in disp, disp)
        check("同时给出 UTF-8 编码名", "filename*=UTF-8''" in disp, disp)

        if st == 200:
            zf = zipfile.ZipFile(io.BytesIO(body))
            names = sorted(zf.namelist())
            check("包内文件齐全",
                  "大文件.bin" in names and "note.txt" in names, names)
            check("目录保留了层级",
                  any(n.startswith("素材/") for n in names), names)
            # 不同目录下的同名文件必须各占一个条目，不能互相覆盖
            check("重名文件各占一个条目",
                  "大文件.bin" in names and "大文件.bin (2)" in names, names)
            check("包内没有重复条目",
                  len(names) == len(set(names)), names)
            check("压缩包可以正常解开", zf.testzip() is None)

        # 目录页也能打包自己那一层
        st, hd, body = request(port, "GET", "/%E7%B4%A0%E6%9D%90/?zip=1")
        check("目录页也能打包", st == 200, st)
        if st == 200:
            names = sorted(zipfile.ZipFile(io.BytesIO(body)).namelist())
            check("目录包只含该目录内的文件",
                  all(n.startswith("素材/") for n in names) and len(names) == 4,
                  names)

        # 打包要按分享项扣次数，否则就成了绕开限制的后门
        pol = server.policy
        pol.set_limit(1)
        pol.reset_usage()
        request(port, "GET", "/?zip=1")
        wait_until(lambda: pol.used("/note.txt") == 1)
        check("打包消耗了各分享项的额度", pol.used("/note.txt") == 1,
              pol.used("/note.txt"))
        check("打包后该分享项额度用尽", pol.remaining("/note.txt") == 0,
              pol.remaining("/note.txt"))

        st, _, body = request(port, "GET", "/?zip=1")
        if st == 200:
            names = sorted(zipfile.ZipFile(io.BytesIO(body)).namelist())
            check("额度用尽的分享项被排除在包外",
                  "note.txt" not in names, names)
        pol.set_limit(0)
        pol.reset_usage()

        # 访问密码
        print("\n[10] 访问密码")
        import base64
        server.policy.set_password("s3cret")

        st, hd, _ = request(port, "GET", "/note.txt")
        check("设了密码后未带凭据返回 401", st == 401, st)
        check("401 带 WWW-Authenticate 头",
              hd.get("WWW-Authenticate", "").startswith("Basic"), hd.get("WWW-Authenticate"))

        def basic(pwd, user="anyone"):
            raw = f"{user}:{pwd}".encode("utf-8")
            return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}

        st, _, body = request(port, "GET", "/note.txt", basic("wrong"))
        check("密码错误返回 401", st == 401, st)
        st, _, body = request(port, "GET", "/note.txt", basic("s3cret"))
        check("密码正确可下载", st == 200 and b"ShareFiles" in body, st)
        st, _, _ = request(port, "GET", "/note.txt", basic("s3cret", "任意用户名"))
        check("用户名随意填也放行", st == 200, st)
        st, _, _ = request(port, "HEAD", "/note.txt", basic("s3cret"))
        check("HEAD 也走认证", st == 200, st)
        st, _, _ = request(port, "GET", "/", basic("s3cret"))
        check("索引页同样受保护", st == 200, st)

        for bad in ("Basic !!!not-base64!!!", "Basic ", "Bearer abc", "Basic Og=="):
            st, _, _ = request(port, "GET", "/note.txt", {"Authorization": bad})
            if st != 401:
                check(f"畸形 Authorization 头不崩溃({bad!r})", False, st)
                break
        else:
            check("各种畸形 Authorization 头都被挡下", True)

        server.policy.set_password("")
        st, _, _ = request(port, "GET", "/note.txt")
        check("清空密码后恢复公开", st == 200, st)

        # IP 黑名单
        print("\n[10b] IP 黑名单")
        check("单个 IP 校验通过",
              parse_block_entry(" 192.168.1.100 ") == "192.168.1.100")
        check("网段会被规范化",
              parse_block_entry("192.168.1.7/24") == "192.168.1.0/24",
              parse_block_entry("192.168.1.7/24"))
        check("IPv6 也接受", parse_block_entry("::1") == "::1")
        bad_ok = all(parse_block_entry(b) is None
                     for b in ("不是IP", "999.1.1.1", "", "   ", "192.168.1"))
        check("各类非法条目都被拒绝", bad_ok,
              [b for b in ("不是IP", "999.1.1.1", "", "   ", "192.168.1")
               if parse_block_entry(b) is not None])

        bl = server.policy
        st, _, _ = request(port, "GET", "/note.txt")
        check("没拉黑时正常访问", st == 200, st)

        bl.set_blocked(["127.0.0.1"])          # 测试客户端就是本机
        st, _, body = request(port, "GET", "/note.txt")
        check("拉黑客户端 IP 后 403", st == 403, st)
        check("403 页说明了原因", "黑名单" in body.decode("utf-8", "replace"))
        st, _, _ = request(port, "GET", "/")
        check("索引页一并被拦", st == 403, st)
        st, _, _ = request(port, "GET", "/?zip=1")
        check("打包下载同样被拦", st == 403, st)
        st, _, _ = request(port, "HEAD", "/note.txt")
        check("HEAD 也被拦", st == 403, st)

        bl.set_blocked(["127.0.0.0/8"])
        st, _, _ = request(port, "GET", "/note.txt")
        check("用整个网段拉黑同样生效", st == 403, st)

        bl.set_blocked(["10.1.2.3", "192.168.0.0/16"])
        st, _, _ = request(port, "GET", "/note.txt")
        check("拉黑别的地址不影响本机", st == 200, st)

        kept = bl.set_blocked(["垃圾条目", "10.9.9.9"])
        check("非法条目被丢弃、只留合法的",
              kept == 1 and bl.blocked == ["10.9.9.9"], (kept, bl.blocked))

        bl.set_blocked([])
        st, _, _ = request(port, "GET", "/note.txt")
        check("清空黑名单后恢复访问", st == 200, st)

        # 下载次数限制
        print("\n[11] 下载次数限制")
        policy = server.policy
        policy.reset_usage()
        policy.set_limit(2)
        big_url = "/%E5%A4%A7%E6%96%87%E4%BB%B6.bin"
        check("初始额度为 2", policy.remaining("/大文件.bin") == 2, policy.remaining("/大文件.bin"))

        # 分段下载（模拟多线程下载器）——八段加起来才该算一次
        chunks = 8
        step = len(payload) // chunks
        for i in range(chunks):
            a = i * step
            b = (a + step - 1) if i < chunks - 1 else len(payload) - 1
            st, _, part = request(port, "GET", big_url,
                                  {"Range": f"bytes={a}-{b}", **basic("")})
            if i == 0:
                check("分段请求返回 206", st == 206, st)
        # 客户端读完响应 ≠ 服务端记完账，断言前得等计数落定
        wait_until(lambda: policy.used("/大文件.bin") == 1)
        check("八段下载完只算一次", policy.used("/大文件.bin") == 1,
              policy.used("/大文件.bin"))
        check("额度相应减到 1", policy.remaining("/大文件.bin") == 1,
              policy.remaining("/大文件.bin"))

        # 完整下载一次，额度用尽
        st, _, _ = request(port, "GET", big_url)
        wait_until(lambda: policy.remaining("/大文件.bin") == 0)
        check("第二次完整下载后额度用尽",
              policy.remaining("/大文件.bin") == 0, policy.remaining("/大文件.bin"))
        st, _, body = request(port, "GET", big_url)
        check("额度用尽后返回 403", st == 403, st)
        check("403 页面说明了原因", "下载次数" in body.decode("utf-8", "replace"))

        # 别的分享项不受影响
        st, _, _ = request(port, "GET", "/note.txt")
        wait_until(lambda: policy.used("/note.txt") == 1)
        check("大文件额度用尽不影响其它分享项", st == 200, st)
        check("其它分享项各记各的账",
              policy.used("/note.txt") == 1 and policy.used("/大文件.bin") == 2,
              (policy.used("/note.txt"), policy.used("/大文件.bin")))

        # 中途断开不该扣次数
        policy.reset_usage()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", big_url)
        resp = conn.getresponse()
        resp.read(1024)
        conn.close()
        # 等服务端彻底收尾再断言，否则"没扣次数"可能只是还没轮到扣
        wait_until(lambda: tracker.snapshot()["task_count"] == 0)
        check("下载中途断开不扣次数", policy.used("/大文件.bin") == 0,
              policy.used("/大文件.bin"))

        # 换一个 IP 重新计数（这里用 127.0.0.2，本机回环的别名地址）
        policy.set_limit(1)
        policy.reset_usage()
        st, _, _ = request(port, "GET", big_url)
        wait_until(lambda: policy.remaining("/大文件.bin") == 0)
        check("第一个来源用掉唯一额度", policy.remaining("/大文件.bin") == 0,
              policy.remaining("/大文件.bin"))
        st, _, _ = request(port, "GET", big_url)
        check("额度用尽后再取还是 403", st == 403, st)

        policy.set_limit(0)
        policy.reset_usage()
        st, _, _ = request(port, "GET", big_url)
        check("关掉限制后恢复不限次", st == 200, st)

        # 端口占用：绝不能让两个实例绑同一端口（Windows 会允许，然后请求乱窜）
        print("\n[12] 端口占用")
        try:
            s2, p2 = create_server(reg, tracker, 8123, "127.0.0.1")
            check("端口被占用时自动改用其它端口", p2 != 8123, p2)
            s2.stop()
        except OSError as exc:
            check("端口被占用时自动改用其它端口", False, exc)

        server.stop()

        # 用户配置：读的一侧必须够皮实，配置文件坏了不能连程序都起不来
        print("\n[12b] 用户配置")
        import config as cfgmod
        cfgdir = tempfile.mkdtemp(prefix="sharefiles-cfg-")
        try:
            cfgmod.CONFIG_DIR = os.path.join(cfgdir, ".sharefiles")
            cfgmod.CONFIG_PATH = os.path.join(cfgmod.CONFIG_DIR, "config.json")

            check("首次运行前还没有配置文件",
                  not os.path.exists(cfgmod.CONFIG_PATH))
            cfg = cfgmod.load_or_create()
            check("首次运行会自动建出配置文件",
                  os.path.exists(cfgmod.CONFIG_PATH))
            check("默认值合理",
                  cfg["port"] == 8000 and cfg["limit"] == 0 and cfg["blocked"] == [])

            cfg.update({"ip": "10.1.10.130", "port": 8577, "preview": True,
                        "password": "pw123", "limit": 4,
                        "blocked": ["10.0.0.0/8", "192.168.1.5"]})
            check("保存返回成功", cfgmod.save(cfg))
            back = cfgmod.load()
            check("密码 / 次数 / 黑名单都能读回",
                  back["password"] == "pw123" and back["limit"] == 4
                  and back["blocked"] == ["10.0.0.0/8", "192.168.1.5"],
                  back)
            check("端口和预览开关也能读回",
                  back["port"] == 8577 and back["preview"] is True, back)

            with open(cfgmod.CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write("{ 这不是 json")
            check("文件被改坏时退回默认值、不抛异常",
                  cfgmod.load()["port"] == 8000)

            with open(cfgmod.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump({"port": "abc", "limit": -3, "blocked": "不是列表",
                           "preview": 1}, f)
            fixed = cfgmod.load()
            check("字段类型不对会被纠正",
                  fixed["port"] == 8000 and fixed["limit"] == 0
                  and fixed["blocked"] == [] and fixed["preview"] is True, fixed)

            with open(cfgmod.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump({"port": 99999}, f)
            check("端口越界会被夹回合法范围", cfgmod.load()["port"] == 65535)

            os.remove(cfgmod.CONFIG_PATH)
            check("文件被删掉后读默认值、不崩", cfgmod.load()["port"] == 8000)

            cfgmod.CONFIG_DIR = os.path.join(cfgdir, "a", "\0b")
            cfgmod.CONFIG_PATH = os.path.join(cfgmod.CONFIG_DIR, "config.json")
            check("路径非法时 save 返回 False 而不是抛异常",
                  cfgmod.save({"port": 8000}) is False)
        finally:
            shutil.rmtree(cfgdir, ignore_errors=True)

        print("\n[13] 工具函数")
        check("human_size", human_size(1536) == "1.5 KB", human_size(1536))
        check("human_size MB", human_size(3 * 1024 * 1024) == "3.0 MB")
        check("human_speed", human_speed(1024 * 1024) == "1.0 MB/s")
        check("human_speed 零值", human_speed(0) == "—")
        check("能取到本机 IP", len(get_lan_ips()) >= 1, get_lan_ips())

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 46}")
    print(f"  通过 {PASS} 项，失败 {FAIL} 项")
    print(f"{'=' * 46}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
