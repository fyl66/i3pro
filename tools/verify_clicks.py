"""真浏览器、真鼠标的交互验收（i3pro 的第四道回归）。

    python tools/verify_clicks.py
    python tools/verify_clicks.py --session "20260524-耐久正赛" --port 8790

为什么要多这一道：`tools/smoke_viewer.js` 跑在假 DOM 上，它能证明"代码调用了
它该调用的函数"，证明不了**点得到**——假 DOM 里元素没有面积、没有遮挡、
没有 pointer-events，disabled 的控件照样派发 click。这一轮就抓出两件假 DOM
看不见的事（见 docs/ACCEPTANCE.md 的 A33）：

* 区段表里那一行的中间是**名字输入框**（`flex:1`），"双击一行"这句话在真实
  命中测试下几乎点不到，只有行尾那一小段长度文字算数；
* serve 模式下缩放会把"当前加载的窗口"换成新的一段，而横轴上限原先按**已加载
  的数据**重算——缩到某一段之后按"全出"，视图再也回不到全场。

做法：用 Edge 自己的 DevTools 协议发真正的 Input.dispatchMouseEvent /
dispatchKeyEvent（真命中测试、真焦点、真键盘），跑在**金标准场次的副本**上，
所以随便点都不会碰到车队数据。只用标准库，Edge 走系统自带的那份。

退出码：0 = 全过（或没有 Edge / 没有金标准数据，自动跳过）；1 = 有断言不过；
2 = 用法不对。截图落在 out/shots/verify-*.png，用眼睛复核时看它们。
"""
import argparse
import base64
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# Windows 控制台默认是 GBK，界面里的 ↶ / ⤢ / 中文引号打不出来，直接
# UnicodeEncodeError 崩在半途——断言结果没输出完比不跑还糟。强制 UTF-8。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_EDGE = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


# --------------------------------------------------------------------------
# 一、一个够用的 WebSocket + CDP 客户端（客户端帧必须打掩码）
# --------------------------------------------------------------------------
class WebSocket:
    def __init__(self, url, timeout=30.0):
        parts = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((parts.hostname, parts.port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parts.path + (("?" + parts.query) if parts.query else "")
        head = (
            "GET %s HTTP/1.1\r\nHost: %s:%s\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, parts.hostname, parts.port, key)
        )
        self.sock.sendall(head.encode("ascii"))
        self._buf = b""
        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手被对端关闭")
            self._buf += chunk
        header, _, self._buf = self._buf.partition(b"\r\n\r\n")
        status = header.split(b"\r\n")[0]
        if b" 101 " not in status:
            raise RuntimeError("WebSocket 握手失败：%s" % status.decode("latin-1"))

    def _read(self, want):
        while len(self._buf) < want:
            chunk = self.sock.recv(max(4096, want - len(self._buf)))
            if not chunk:
                raise RuntimeError("WebSocket 被对端关闭")
            self._buf += chunk
        out, self._buf = self._buf[:want], self._buf[want:]
        return out

    def send_text(self, text):
        data = text.encode("utf-8")
        mask = os.urandom(4)
        size = len(data)
        if size < 126:
            head = bytes([0x81, 0x80 | size])
        elif size < 65536:
            head = bytes([0x81, 0x80 | 126]) + size.to_bytes(2, "big")
        else:
            head = bytes([0x81, 0x80 | 127]) + size.to_bytes(8, "big")
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(head + mask + masked)

    def recv_text(self):
        """读一条完整文本消息；ping 自己回 pong，分片消息自己拼。"""
        pieces = []
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            masked, length = b2 & 0x80, b2 & 0x7F
            if length == 126:
                length = int.from_bytes(self._read(2), "big")
            elif length == 127:
                length = int.from_bytes(self._read(8), "big")
            key = self._read(4) if masked else None
            payload = self._read(length) if length else b""
            if key:
                payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:  # ping
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if opcode == 0x8:
                raise RuntimeError("WebSocket 被对端要求关闭")
            if opcode in (0x0, 0x1):
                pieces.append(payload)
                if fin:
                    return b"".join(pieces).decode("utf-8")


class Browser:
    """一个无头 Edge + 一条 CDP 连接。"""

    def __init__(self, edge, port, profile, window=(1600, 1000)):
        self.port = port
        self.profile = profile
        stderr_path = os.path.join(os.path.dirname(profile), "_edge_stderr.log")
        self._stderr = open(stderr_path, "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            [
                edge,
                # 新版无头在这台机器上 GPU 进程会以 STATUS_NOT_IMPLEMENTED 死掉
                # （"GPU process isn't usable. Goodbye."），旧版无头 + --no-sandbox
                # 才起得来。只为读本地快照，不涉及任何外部页面。
                "--headless=old", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage",
                "--no-first-run", "--disable-extensions", "--hide-scrollbars",
                "--remote-debugging-port=%d" % port,
                "--user-data-dir=%s" % profile,
                "--window-size=%d,%d" % window,
                "--allow-file-access-from-files",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=self._stderr,
        )
        self._id = 0
        self.events = []
        self.ws = None
        for _ in range(200):
            try:
                self._browser_ws = self._json("/json/version")["webSocketDebuggerUrl"]
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("Edge 的调试端口没起来（%s）" % stderr_path)
        self.ws = WebSocket(self._browser_ws)

    def _json(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return json.loads(body.decode("utf-8"))

    def call(self, method, params=None, session=None, timeout=30.0):
        self._id += 1
        mid = self._id
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send_text(json.dumps(msg))
        deadline = time.time() + timeout
        while time.time() < deadline:
            got = json.loads(self.ws.recv_text())
            if got.get("id") == mid:
                if "error" in got:
                    raise RuntimeError("%s -> %s" % (method, got["error"]))
                return got.get("result", {})
            if "method" in got:
                self.events.append(got)
        raise TimeoutError("%s 超时（%.0fs）" % (method, timeout))

    def open(self, url, wait=0.0):
        target = self.call("Target.createTarget", {"url": url})
        session = self.call(
            "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}
        )["sessionId"]
        self.call("Page.enable", session=session)
        self.call("Runtime.enable", session=session)
        if wait:
            time.sleep(wait)
        return session

    def js(self, expression, session, timeout=30.0):
        out = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            session=session,
            timeout=timeout,
        )
        if out.get("exceptionDetails"):
            raise RuntimeError("页面里抛错：%s" % json.dumps(out["exceptionDetails"])[:300])
        return out["result"].get("value")

    def wait_for(self, expression, session, timeout=60.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.js(expression, session):
                return True
            time.sleep(0.1)
        return False

    # ---- 真输入 ----
    def mouse(self, kind, x, y, session, clicks=1, buttons=1):
        self.call("Input.dispatchMouseEvent", {
            "type": kind, "x": x, "y": y, "button": "left",
            "buttons": buttons, "clickCount": clicks,
        }, session=session)

    def click(self, x, y, session, clicks=1):
        self.mouse("mouseMoved", x, y, session, buttons=0)
        for i in range(clicks):
            self.mouse("mousePressed", x, y, session, clicks=i + 1)
            self.mouse("mouseReleased", x, y, session, clicks=i + 1, buttons=0)
            time.sleep(0.05)
        time.sleep(0.25)

    def double_click(self, x, y, session):
        self.click(x, y, session, clicks=2)

    def key(self, text, session):
        for ch in text:
            self.call("Input.dispatchKeyEvent", {"type": "keyDown", "text": ch}, session=session)
            self.call("Input.dispatchKeyEvent", {"type": "keyUp"}, session=session)
        time.sleep(0.2)

    def insert_text(self, text, session):
        """中文没法用 keyDown 的 ``text`` 塞进去（要走 IME），用 Input.insertText。

        输入的是**真文本**，和"直接改 value + 派发 change"不是一回事：中间那些
        输入法 / 编辑框的行为仍然是真的。
        """
        self.call("Input.insertText", {"text": text}, session=session)
        time.sleep(0.2)

    def key_named(self, key, code, windows_code, session, modifiers=0):
        for kind in ("keyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", {
                "type": kind, "key": key, "code": code,
                "windowsVirtualKeyCode": windows_code,
                "nativeVirtualKeyCode": windows_code, "modifiers": modifiers,
            }, session=session)
        time.sleep(0.2)

    def shot(self, path, session):
        data = self.call("Page.captureScreenshot", {"format": "png"}, session=session)["data"]
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(base64.b64decode(data))
        return path

    def page_errors(self):
        out = []
        for ev in self.events:
            if ev.get("method") == "Runtime.exceptionThrown":
                out.append(ev["params"]["exceptionDetails"].get("text", "?"))
        return out

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


# --------------------------------------------------------------------------
# 二、跑真场次的服务
# --------------------------------------------------------------------------
def find_edge(explicit=None):
    if explicit:
        return explicit if os.path.exists(explicit) else None
    for path in DEFAULT_EDGE:
        if os.path.exists(path):
            return path
    return None


def stage_session(session, work_dir):
    """把金标准场次复制一份出来——侧车写在这份副本里，车队数据不动。"""
    if os.path.isdir(work_dir):
        shutil.rmtree(work_dir)
    os.makedirs(work_dir)
    source_dir = os.path.join(ROOT, "i2pro_data")
    copied = 0
    for suffix in (".ld", ".ldx"):
        source = os.path.join(source_dir, session + suffix)
        if os.path.exists(source):
            shutil.copy2(source, os.path.join(work_dir, session + suffix))
            copied += 1
    return copied > 0


def start_server(work_dir, port):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    log = open(os.path.join(ROOT, "out", "_verify_serve.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "i3pro", "serve", "--data", work_dir,
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=log, stderr=log,
    )
    for _ in range(300):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=2).read()
            return proc
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError("本地服务起不来，看 out/_verify_serve.log")
            time.sleep(0.1)
    raise RuntimeError("本地服务 30 s 没就绪")


# 页面里的小工具：把"时刻 -> 像素"和"元素 -> 屏幕坐标"交给页面自己算，
# 因为只有它知道当前坐标轴的内边距。
HELPERS = """
function __px(t) {
  var c = document.querySelector("canvas"), r = c.getBoundingClientRect(),
      p = axisPad(), g = lane();
  return r.left + p.l + ((t - g[0]) / (g[1] - g[0])) * (r.width - p.l - p.r);
}
function __tAt(x) {
  var c = document.querySelector("canvas"), r = c.getBoundingClientRect(),
      p = axisPad(), g = lane();
  return g[0] + (x - r.left - p.l) / (r.width - p.l - p.r) * (g[1] - g[0]);
}
function __py(offset) {
  var c = document.querySelector("canvas"), r = c.getBoundingClientRect(), p = axisPad();
  return r.top + p.t + offset;
}
function __center(el) {
  el.scrollIntoView({block: "center"});
  var r = el.getBoundingClientRect();
  return {x: r.left + r.width / 2, y: r.top + r.height / 2,
          w: r.width, h: r.height, disabled: !!el.disabled};
}
function __rectOf(id) { return __center(document.getElementById(id)); }

// 记下每一次保存请求和它的结果：界面"看着改成了"但没落盘时，一眼看出卡在哪。
window.__fetchLog = [];
(function () {
  var raw = window.fetch;
  window.fetch = function (url, opts) {
    var method = (opts && opts.method) || "GET";
    return raw.apply(this, arguments).then(function (res) {
      window.__fetchLog.push({url: String(url), method: method, status: res.status});
      return res;
    }, function (err) {
      window.__fetchLog.push({url: String(url), method: method, status: "error: " + err});
      throw err;
    });
  };
})();
"""


class Checker:
    def __init__(self, browser, session):
        self.browser = browser
        self.session = session
        self.results = []

    def js(self, expression):
        return self.browser.js(expression, self.session)

    def check(self, name, ok, detail=""):
        self.results.append((name, bool(ok)))
        print(("  PASS  " if ok else "  FAIL  ") + name
              + (("  — " + str(detail)) if detail else ""))

    def toast(self):
        return self.js("(document.getElementById('toast')||{}).textContent||''")

    def view(self):
        return json.loads(self.js("JSON.stringify(i3pro.state.view)"))

    def set_view(self, a, b):
        self.js("(function(){i3pro.state.view=[%r,%r];i3pro.renderAll();return true;})()" % (a, b))

    # ---------------------------------------------------------------- 各项
    def sections(self):
        """#8：真双击色条缩到那一段；双击绘图区照旧原地放大；表里点得到。"""
        self.js("document.querySelector('canvas').scrollIntoView({block:'start'}); true")
        time.sleep(0.3)
        band = self.js(
            "(function(){var info=i3pro.sectionsState()||{};var lap=(info.laps||[])[0];"
            "var w=lap?i3pro.sectionWindow(1,lap.label):null;if(!w)return null;"
            # 行 / ⤢ 用的是参考圈的那一行（表本来就是按参考圈算的），时间轴上的
            # 色条用的是光标那一刻所在那条圈——两者可以差一个采样。
            "var r=i3pro.sectionWindow(1,null);"
            "i3pro.state.view=[w.start,w.end];i3pro.renderAll();"
            "return JSON.stringify({lap:w,row:r});})()"
        )
        if not band:
            self.check("#8 能拿到第 2 段的窗口", False, "sectionsState 里没有 bands")
            return
        info = json.loads(band)["lap"]
        row_win = json.loads(band)["row"]
        self.check("#8 能拿到第 2 段的窗口", True, "%s [%.2f, %.2f]"
                   % (info["label"], info["start"], info["end"]))
        x = self.js("__px(%r)" % ((info["start"] + info["end"]) / 2.0))
        strip_y = self.js("__py(4)")
        body_y = self.js("__py(60)")
        clicked = self.js("__tAt(%r)" % x)
        span = info["end"] - info["start"]

        self.browser.double_click(x, strip_y, self.session)
        view = self.view()
        self.check("#8 真双击顶端色条 -> 横轴正好是那一段",
                   abs(view[0] - info["start"]) < 1e-6 and abs(view[1] - info["end"]) < 1e-6,
                   "view=[%.3f, %.3f]" % (view[0], view[1]))
        self.browser.shot(os.path.join(ROOT, "out", "shots", "verify-strip-zoom.png"), self.session)

        self.set_view(info["start"], info["end"])
        self.browser.double_click(x, body_y, self.session)
        view = self.view()
        self.check("#8 真双击绘图区（同列、非色条）-> 照旧原地放大 2 倍",
                   abs((view[1] - view[0]) - span / 2) < 0.05 and view[0] < clicked < view[1],
                   "span %.3f -> %.3f，点击处 %.3f 在视图里" % (span, view[1] - view[0], clicked))

        # 区段表：行的中间是名字输入框，"双击一行"在真命中测试下点不到。
        self.set_view(0.0, 5.0)
        zoom = self.js(
            "(function(){var a=document.querySelector('#sectionsList [data-section-zoom=\"1\"]');"
            "return a?JSON.stringify(__center(a)):null;})()"
        )
        if not zoom:
            self.check("#8 区段表里有一个点得到的「放大」入口", False, "没有 [data-section-zoom]")
        else:
            point = json.loads(zoom)
            # 有界重试：首跑偶发"点了但视图没动"——区段面板刚渲染完就点，布局
            # 还没稳定，命中的是旁边的输入框。重试而不是放过，免得把竞赛故障
            # 当成 bug 修，也免得把 bug 当成竞赛故障放过。
            view = self.view()
            for attempt in range(3):
                self.browser.click(point["x"], point["y"], self.session)
                view = self.view()
                if (abs(view[0] - row_win["start"]) < 1e-6
                        and abs(view[1] - row_win["end"]) < 1e-6):
                    break
                time.sleep(0.3)
            self.check("#8 真点区段表里的放大入口 -> 缩到那一段",
                       abs(view[0] - row_win["start"]) < 1e-6
                       and abs(view[1] - row_win["end"]) < 1e-6,
                       "view=[%.3f, %.3f]，参考圈第 2 段=[%.3f, %.3f]"
                       % (view[0], view[1], row_win["start"], row_win["end"]))
        row_center = self.js(
            "(function(){var r=document.querySelectorAll('#sectionsList .srow')[1];"
            "return r?JSON.stringify(__center(r)):null;})()"
        )
        if row_center:
            point = json.loads(row_center)
            before = self.view()
            self.browser.double_click(point["x"], point["y"], self.session)
            view = self.view()
            # 行中间那一格是名字输入框：双击它是"选词"，跳了视图才叫错。
            self.check("#8 真双击行中间的输入框 -> 不跳视图（那是选词）",
                       abs(view[0] - before[0]) < 1e-6 and abs(view[1] - before[1]) < 1e-6,
                       "view=[%.3f, %.3f]，双击前 [%.3f, %.3f]"
                       % (view[0], view[1], before[0], before[1]))

        # 缩到一段之后"全出"必须真的回到全场（serve 模式曾在这里回不去）
        duration = self.js("DATA.meta && DATA.meta.duration")
        self.js("(function(){i3pro.state.view=null;i3pro.renderAll();return true;})()")
        time.sleep(1.0)
        lane = json.loads(self.js("JSON.stringify(lane())"))
        self.check("#8 缩到一段后「全出」-> 横轴回到整场，不是卡在刚加载的那一段",
                   lane[0] < 1.0 and (lane[1] - lane[0]) > 0.99 * duration,
                   "全出后 lane=[%.2f, %.2f]，整场时长 %.2f s" % (lane[0], lane[1], duration))

    def crossings(self, sidecar):
        """#5：光标处插入穿越（没光标要说出来，有光标要落盘）。"""
        self.js("(function(){i3pro.state.view=fullRange();i3pro.renderAll();return true;})()")
        button = json.loads(self.js("JSON.stringify(__rectOf('addCrossing'))"))
        self.check("「＋穿越」在 serve 模式下可点", not button["disabled"])
        # 先把光标清掉：上一步双击绘图区已经设过一个光标了，不清就测不到"没光标"
        # 这条分支（上一版就是这么骗过自己的）。
        self.js("(function(){i3pro.state.cursor=null;i3pro.renderAll();return true;})()")
        before = len(json.loads(self.js("JSON.stringify(i3pro.state.lapsConfig.beacons)")))
        self.browser.click(button["x"], button["y"], self.session)
        time.sleep(0.4)
        after = len(json.loads(self.js("JSON.stringify(i3pro.state.lapsConfig.beacons)")))
        self.check("#5 没设光标就按＋穿越 -> 说清楚要先把鼠标移到图上，并且不加信标",
                   after == before and "鼠标" in self.toast(), "toast=%r" % self.toast())
        x = self.js("__px((lane()[0]+lane()[1])/2)")
        y = self.js("__py(60)")
        self.browser.click(x, y, self.session)
        cursor = self.js("i3pro.exactCursorTime()")
        self.check("#5 真点在图上 -> 光标落在那一刻", cursor is not None, "%.3f s" % (cursor or 0))
        self.browser.click(button["x"], button["y"], self.session)
        time.sleep(0.8)
        beacons = json.loads(self.js("JSON.stringify(i3pro.state.lapsConfig.beacons)"))
        added = beacons[-1] if len(beacons) > before else {}
        self.check("#5 有光标时按＋穿越 -> 多一次穿越，时刻就是光标处",
                   len(beacons) == before + 1 and abs(added.get("time", -1) - cursor) < 0.01,
                   "t=%s，光标 %.3f" % (added.get("time"), cursor))
        classes = self.js(
            "JSON.stringify(Array.prototype.map.call(document.querySelectorAll('#beaconList .pill'),"
            "function(p){return p.className;}))"
        )
        self.check("#5 手工穿越画成虚线药丸（和赛道上的真信标分得清）",
                   "manual" in (classes or ""), classes)
        disk = json.load(open(sidecar, encoding="utf-8"))
        self.check("#5 穿越真的落进侧车（不是只改了内存）",
                   any(b.get("time") is not None for b in disk["beacons"]))
        return disk

    def histogram(self):
        """#9：直方图——加得出来、画得出来、缩放会重问、门槛报错能照做。

        这里验的是假 DOM 证明不了的那几件：新加的组件在真浏览器里**真的画出了柱子**
        （读回画布像素），改格数、缩放之后**真的重新问了服务**（看 __fetchLog），
        门槛写错时表头**真的写出了下一步**。
        """
        def rect(element_id):
            raw = self.js("JSON.stringify(__rectOf('%s'))" % element_id)
            return json.loads(raw) if raw and raw != "null" else None

        def comp_json():
            raw = self.js(
                "(function(){var a=i3pro.state.components.filter(function(c){"
                "return c.type==='histogram';});var c=a[a.length-1];"
                "return c?JSON.stringify({id:c.id,channel:c.config.channel,"
                "bins:c.config.bins}):'null';})()"
            )
            return json.loads(raw) if raw and raw != "null" else None

        def hist_calls():
            raw = self.js("JSON.stringify(window.__fetchLog.filter(function(e){"
                          "return e.url.indexOf('/histogram')>=0;}))")
            return json.loads(raw) if raw else []

        def wait_for_hist(seen, needle, timeout=5.0):
            """等到第 seen 条之后出现一条含 needle 的 /histogram 请求。

            有界轮询而不是 sleep 固定秒数：真浏览器里"打字 -> change -> fetch ->
            渲染"是四段异步，睡多久都是猜；这里最多等 5 秒，等不到就是真没发。
            """
            deadline = time.time() + timeout
            while time.time() < deadline:
                calls = hist_calls()
                for call in calls[seen:]:
                    if needle in call["url"]:
                        return calls
                time.sleep(0.2)
            return hist_calls()

        before = self.js("i3pro.state.components.length")
        self.js("(function(){document.getElementById('addType').value='histogram';"
                "return true;})()")
        add = rect("addBtn")
        self.browser.click(add["x"], add["y"], self.session)
        self.browser.wait_for("i3pro.state.components.length === %d" % (before + 1),
                              self.session, timeout=10)
        time.sleep(1.0)
        comp = comp_json()
        self.check("#9 真点「＋ 组件」-> 工作表里多了一个直方图，并且自己挑好了一条通道",
                   bool(comp) and bool(comp["channel"]),
                   json.dumps(comp, ensure_ascii=False) if comp else "没有直方图组件")
        if not comp:
            return

        painted = self.js(
            "(function(){var cv=document.querySelector('.comp[data-id=\"%s\"] canvas');"
            "if(!cv)return -1;var d=cv.getContext('2d').getImageData(0,0,cv.width,cv.height).data;"
            "var n=0;for(var i=3;i<d.length;i+=4){if(d[i]>0)n++;}return n;})()" % comp["id"]
        )
        self.check("#9 直方图画布上真的有东西（不是一张白纸）",
                   isinstance(painted, (int, float)) and painted > 500,
                   "%s 个像素" % painted)
        self.browser.shot(os.path.join(ROOT, "out", "shots", "verify-histogram.png"),
                          self.session)

        # 改格数：真点输入框、真 Ctrl+A、真打字、真回车
        bins_box = self.js(
            "(function(){var el=document.querySelector('.comp[data-id=\"%s\"] input[data-hist=\"bins\"]');"
            "return el?JSON.stringify(__center(el)):'null';})()" % comp["id"]
        )
        if bins_box and bins_box != "null":
            point = json.loads(bins_box)
            seen = len(hist_calls())
            self.browser.click(point["x"], point["y"], self.session)
            self.browser.key_named("a", "KeyA", 65, self.session, modifiers=2)
            self.browser.key("7", self.session)
            self.browser.key_named("Enter", "Enter", 13, self.session)
            # 数字输入框在 Chromium 里要**失去焦点**才发 change：回车不算，
            # 按一下 Tab 才算。少了这一下，界面上看着改了、请求却还是旧格数。
            self.browser.key_named("Tab", "Tab", 9, self.session)
            calls = wait_for_hist(seen, "bins=7")
            self.check("#9 真键盘改格数 -> 拿新格数重新问了服务端",
                       len(calls) > seen and any("bins=7" in c["url"] for c in calls[seen:]),
                       calls[-1]["url"].split("?")[-1] if calls else "(没有请求)")
        else:
            self.check("#9 真键盘改格数 -> 拿新格数重新问了服务端", False, "没有格数输入框")

        # 缩放：真双击图（不是直方图自己）-> 直方图必须跟着重问一次
        seen = len(hist_calls())
        x = self.js("__px((i3pro.lane()[0]+i3pro.lane()[1])/2)")
        y = self.js("__py(60)")
        before_view = self.view()
        self.browser.double_click(x, y, self.session)
        time.sleep(1.0)
        calls = hist_calls()
        view = self.view()
        self.check("#9 真双击放大 -> 直方图跟着换了窗口（重新问了一次）",
                   len(calls) > seen and (view[1] - view[0]) < (before_view[1] - before_view[0]),
                   "view %.1f-%.1f -> %.1f-%.1f，请求 %d 次"
                   % (before_view[0], before_view[1], view[0], view[1], len(calls) - seen))

        # 门槛写一条不存在的通道：表头要写出下一步，而不是装没看见
        gate_box = self.js(
            "(function(){var el=document.querySelector('.comp[data-id=\"%s\"] input[data-hist=\"gate\"]');"
            "return el?JSON.stringify(__center(el)):'null';})()" % comp["id"]
        )
        if gate_box and gate_box != "null":
            point = json.loads(gate_box)
            self.browser.click(point["x"], point["y"], self.session)
            self.browser.key_named("a", "KeyA", 65, self.session, modifiers=2)
            self.browser.insert_text("查无此通道", self.session)
            self.browser.key_named("Enter", "Enter", 13, self.session)
            self.browser.key_named("Tab", "Tab", 9, self.session)
            deadline = time.time() + 6.0
            head = ""
            while time.time() < deadline:
                head = self.js(
                    "(function(){var el=document.querySelector('.comp[data-id=\"%s\"] .graphhead');"
                    "return el?String(el.textContent):'';})()" % comp["id"]
                )
                if "门槛" in head:
                    break
                time.sleep(0.25)
            self.check("#9 门槛写错 -> 表头写出「哪错了 + 下一步」，不装没看见",
                       "门槛" in head and any(word in head for word in ("检查拼写", "先", "换", "换一条")),
                       head)
            # 清掉门槛，免得影响后面的着色断言（同样要 Tab 才提交）。
            #
            # **必须先重新点一次输入框**：上面那一下 Tab 已经把焦点交给了下一个
            # 控件，此时再按 Ctrl+A / Backspace 是打在别的控件上的——门槛原封不动，
            # 后面的请求继续带着这个坏门槛报错，看起来像"着色坏了"。这条曾经就是
            # 这么挂的（真浏览器验收跑出来的）。
            point = json.loads(self.js(
                "(function(){var el=document.querySelector('.comp[data-id=\"%s\"]"
                " input[data-hist=\"gate\"]');"
                "return el?JSON.stringify(__center(el)):'null';})()" % comp["id"]
            ))
            self.browser.click(point["x"], point["y"], self.session)
            self.browser.key_named("a", "KeyA", 65, self.session, modifiers=2)
            self.browser.key_named("Backspace", "Backspace", 8, self.session)
            self.browser.key_named("Enter", "Enter", 13, self.session)
            self.browser.key_named("Tab", "Tab", 9, self.session)
            deadline = time.time() + 6.0
            while time.time() < deadline:
                calls = hist_calls()
                if calls and "gate=" not in calls[-1]["url"]:
                    break
                time.sleep(0.25)
            # 清掉之后必须真的恢复：请求不再带 gate=，表头也从报错变回统计量。
            # 只验"清干净了"不够——用户关心的是"把写错的条件删掉，图能回来"。
            recovered = ""
            deadline = time.time() + 6.0
            while time.time() < deadline:
                recovered = self.js(
                    "(function(){var el=document.querySelector('.comp[data-id=\"%s\"] .graphhead');"
                    "return el?String(el.textContent):'';})()" % comp["id"]
                )
                if "中位" in recovered or "点" in recovered:
                    break
                time.sleep(0.25)
            self.check("#9 删掉写错的门槛 -> 不再报错，表头回到统计量（图能回来）",
                       bool(calls) and "gate=" not in calls[-1]["url"] and "门槛" not in recovered,
                       "请求=%s；表头=%s" % (
                           calls[-1]["url"].split("?")[-1] if calls else "(没有请求)",
                           recovered[:60]))
        else:
            self.check("#9 门槛写错 -> 表头写出「哪错了 + 下一步」，不装没看见",
                       False, "没有门槛输入框")

        # 着色：真键盘挑一条（下拉是原生控件，键盘上的 ArrowDown 就是真交互）
        colour_box = self.js(
            "(function(){var el=document.querySelector('.comp[data-id=\"%s\"] select[data-hist=\"colour\"]');"
            "if(!el)return 'null';el.focus();return JSON.stringify(__center(el));})()" % comp["id"]
        )
        if colour_box and colour_box != "null":
            seen = len(hist_calls())
            self.browser.key_named("ArrowDown", "ArrowDown", 40, self.session)
            self.browser.key_named("ArrowDown", "ArrowDown", 40, self.session)
            time.sleep(1.0)
            calls = hist_calls()
            head = self.js(
                "(function(){var el=document.querySelector('.comp[data-id=\"%s\"] .graphhead');"
                "return el?String(el.textContent):'';})()" % comp["id"]
            )
            self.check("#9 选一条着色通道 -> 请求带上 colour=，表头写明色是什么",
                       len(calls) > seen and "colour=" in calls[-1]["url"] and "色=" in head,
                       "请求=%s；表头=%s" % (
                           calls[-1]["url"].split("?")[-1] if calls else "(没有请求)",
                           head[:80]))
        else:
            self.check("#9 选一条着色通道 -> 请求带上 colour=，表头写明色是什么",
                       False, "没有着色下拉框")
        self.browser.shot(os.path.join(ROOT, "out", "shots", "verify-histogram-colour.png"),
                          self.session)

    def rename(self, sidecar):
        """#4 / #6：就地改名（回车存、Esc 撤）+ 撤销。

        改的是**种进侧车的那条**信标（名字就是"手工穿越"），不是列表里的第一条。
        ＋穿越 插进来的那条与它重名，会被加上后缀（实测是"手工穿越 2"），而 trusted
        标记——``手工穿越 1``——是打在种下的那条上的。挑第一条输入框就会挑到插进来的
        那条：它本来就没有标记，改完名自然也没有标记可以迁移，于是这条断言在**行为
        正确的时候**失败。所以按名字定位，找不到就明说。
        """
        seeded = "手工穿越"

        def boxes():
            raw = self.js(
                "(function(){var bs=document.querySelectorAll('#beaconList .bname');"
                "var out=[];for(var i=0;i<bs.length;i++){var c=__center(bs[i]);"
                "out.push({name:bs[i].value,x:c.x,y:c.y});}return JSON.stringify(out);})()"
            )
            return json.loads(raw) if raw and raw != "null" else []

        def pill_box(name):
            for box in boxes():
                if box["name"] == name:
                    return box
            print("        列表里没有叫 %r 的输入框：%s" % (name, [b["name"] for b in boxes()]))
            return None

        def dom_has_name(name):
            return any(box["name"] == name for box in boxes())

        def retype(text, name=seeded):
            box = pill_box(name)
            if box is None:
                return "", None
            self.browser.click(box["x"], box["y"], self.session)
            focused = self.js("document.activeElement && document.activeElement.className") or ""
            if "bname" not in focused:
                print("        点 (%.0f, %.0f) 落在 %s 上，不是信标名输入框"
                      % (box["x"], box["y"], self.js(
                          "(function(){var e=document.elementFromPoint(%.0f,%.0f);"
                          "return e?e.tagName+'.'+e.className:'null';})()"
                          % (box["x"], box["y"]))))
            self.browser.key_named("a", "KeyA", 65, self.session, modifiers=2)
            self.browser.key(text, self.session)
            return focused, self.js("document.activeElement && document.activeElement.value")

        before = open(sidecar, "rb").read()
        focused, typed = retype("改名之后")
        self.check("#4 真点信标名 -> 输入框拿到焦点", "bname" in focused, focused)
        self.check("#4 真键盘输入进得去", typed == "改名之后", repr(typed))
        self.browser.shot(os.path.join(ROOT, "out", "shots", "verify-rename.png"), self.session)
        self.browser.key_named("Escape", "Escape", 27, self.session)
        time.sleep(0.4)
        disk = json.load(open(sidecar, encoding="utf-8"))
        self.check("#4 Esc 取消 -> 输入框弹回原名，侧车一个字节没动",
                   open(sidecar, "rb").read() == before
                   and any(b["name"] == seeded for b in disk["beacons"])
                   and dom_has_name(seeded),
                   [b["name"] for b in disk["beacons"]])

        retype("改名之后")
        self.browser.key_named("Enter", "Enter", 13, self.session)
        time.sleep(0.9)
        disk = json.load(open(sidecar, encoding="utf-8"))
        names = [b["name"] for b in disk["beacons"]]
        live = json.loads(self.js("JSON.stringify(i3pro.state.lapsConfig.beacons)"))
        self.check("#4 回车保存 -> 侧车里那条信标真的叫新名字了",
                   "改名之后" in names, names)
        if "改名之后" not in names:
            print("        盘上：%s" % names)
            print("        界面：%s" % [b.get("name") for b in live])
            print("        toast：%r" % self.toast())
            print("        保存请求：%s" % self.js("JSON.stringify(window.__fetchLog.slice(-4))"))
        self.check("#4 改名同时把 trusted 标记迁走（不然用户打的不可信分数会丢）",
                   disk.get("trusted") == {"改名之后 1": False},
                   json.dumps(disk.get("trusted"), ensure_ascii=False))
        undo = json.loads(self.js("JSON.stringify(__rectOf('undoLaps'))"))
        self.check("#6 改过一次后 ↶ 撤销 变成可点", not undo["disabled"])
        self.browser.click(undo["x"], undo["y"], self.session)
        time.sleep(0.9)
        disk = json.load(open(sidecar, encoding="utf-8"))
        after_undo = [b["name"] for b in disk["beacons"]]
        self.check("#6 真点撤销 -> 名字退回上一步",
                   "改名之后" not in after_undo and len(after_undo) == len(names),
                   "撤销前 %s -> 撤销后 %s" % (names, after_undo))
        self.browser.shot(os.path.join(ROOT, "out", "shots", "verify-undo.png"), self.session)


def main(argv=None):
    parser = argparse.ArgumentParser(description="真浏览器交互验收（需要 Edge + 金标准数据）")
    parser.add_argument("--session", default="20260908-cjh 高避5圈", help="用哪个场次（默认高避5圈）")
    parser.add_argument("--edge", default=None, help="msedge.exe 的路径（默认自动找）")
    parser.add_argument("--port", type=int, default=8741, help="本地服务端口")
    parser.add_argument("--cdp-port", type=int, default=9333, help="Edge 调试端口")
    args = parser.parse_args(argv)

    edge = find_edge(args.edge)
    if not edge:
        print("SKIP - 这台机器上没有找到 Microsoft Edge，跳过真浏览器验收。")
        print("      （装 Edge 之后重跑 `python tools\\verify_clicks.py` 即可。）")
        return 0
    # 一份跑一份数据/浏览器配置：两个人（或两个代理）同时跑这条验收时，谁也别
    # 删谁的东西——早前一次并行运行就是互相 rmtree，读到"侧车文件不见了"。
    work_dir = os.path.join(ROOT, "out", "_verify_data_%d" % args.port)
    if not stage_session(args.session, work_dir):
        print("SKIP - 没有找到金标准数据 i2pro_data\\%s.ld，跳过真浏览器验收。" % args.session)
        print("      （缺数据的机器上这条自动跳过，不算通过。）")
        return 0
    # 种一份"用户已经手工插过一次穿越、并且给那个圈打过分"的侧车：改名的同时
    # 要把 trusted 标记迁走，不种就永远走不到那条迁移逻辑。
    sidecar = os.path.join(work_dir, args.session + ".laps.json")
    with open(sidecar, "w", encoding="utf-8") as handle:
        json.dump({"mode": "auto",
                   "beacons": [{"name": "手工穿越", "time": 120.0}],
                   "trusted": {"手工穿越 1": False}}, handle, ensure_ascii=False)

    server = start_server(work_dir, args.port)
    browser = None
    try:
        browser = Browser(
            edge, args.cdp_port, os.path.join(ROOT, "out", "_edge_profile_%d" % args.cdp_port)
        )
        url = "http://127.0.0.1:%d/session/%s" % (args.port, urllib.parse.quote(args.session))
        session = browser.open(url, wait=1.0)
        ready = browser.wait_for(
            "!!(window.i3pro && i3pro.state && i3pro.data && i3pro.data.channels"
            " && i3pro.data.channels.length && document.querySelectorAll('canvas').length)",
            session,
        )
        checker = Checker(browser, session)
        checker.check("真 Edge 里 serve 模式加载出数据", ready)
        if ready:
            browser.js(HELPERS, session)
            browser.js(
                "(function(){var l=(i3pro.data.laps||[]).filter(function(x){return x.complete;});"
                "i3pro.state.mode='time';i3pro.state.showSections=true;"
                "if(l[1]){i3pro.state.view=[l[1].start_time,l[1].end_time];}i3pro.renderAll();"
                "return true;})()",
                session,
            )
            time.sleep(0.5)
            checker.sections()
            checker.crossings(sidecar)
            checker.rename(sidecar)
            checker.histogram()
            errors = browser.page_errors()
            checker.check("整场没有页面级报错", not errors, errors[:3])
        bad = [name for name, ok in checker.results if not ok]
        print("\n%d 项检查：%d 通过，%d 失败" % (len(checker.results),
                                              len(checker.results) - len(bad), len(bad)))
        return 1 if bad else 0
    finally:
        if browser:
            browser.close()
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
