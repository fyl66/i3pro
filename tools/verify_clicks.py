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
import glob
import http.client
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
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

# 与 viewer.html 里的 TIME_STEPS 一字不差：断言"刻度落在钟表档位上"必须用同一张表，
# 两边各写一份就是为了**不一致时会红**，而不是为了复用。
TIME_STEPS = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5,
              1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600]


def _clock_ladder(step):
    """步长是不是 1/2/5/10/15/30 秒、1/2/5/10/15/30 分…… 里的那一档。"""
    return any(abs(step - s) <= 1e-9 * max(1.0, s) for s in TIME_STEPS)


def _generic_ladder(step):
    """距离轴 / 数值轴用的是通用档位 1/2/5×10ⁿ。"""
    if not (step > 0) or not math.isfinite(step):
        return False
    exponent = math.floor(math.log10(step))
    mantissa = step / (10 ** exponent)
    return any(abs(mantissa - want) < 1e-6 for want in (1, 2, 5, 10))


def _label_value(text):
    """把画出来的刻度文字读回数值：'2:05' -> 125.0，'231.80' -> 231.8，读不出 None。"""
    text = str(text).strip()
    if not text:
        return None
    if ":" in text:
        sign = -1.0 if text.startswith("-") else 1.0
        minutes, _, seconds = text.lstrip("+-").partition(":")
        if not minutes.isdigit() or not seconds.isdigit():
            return None
        return sign * (int(minutes) * 60 + int(seconds))
    try:
        return float(text)
    except ValueError:
        return None


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

    def open(self, url, wait=0.0, init_script=None):
        """开一个标签页。``init_script`` 在页面自己的脚本**之前**跑（CDP 的
        addScriptToEvaluateOnNewDocument），所以能截住页面第一次绘制。"""
        target = self.call("Target.createTarget", {"url": "about:blank"})
        session = self.call(
            "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}
        )["sessionId"]
        self.call("Page.enable", session=session)
        self.call("Runtime.enable", session=session)
        if init_script:
            self.call("Page.addScriptToEvaluateOnNewDocument",
                      {"source": init_script}, session=session)
        self.call("Page.navigate", {"url": url}, session=session)
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


# 真画布上没有文字节点：把每次 fillText 截下来，才能知道**画出来的**横轴刻度是什么。
# 钩子只活在"这一条验收"开的浏览器里，不进口到产品代码里（页面也没被改）。
# WeakMap 给每块 canvas 一个号，免得把散点图、直方图那几行的标签混进主图的横轴。
AXIS_HOOK = """
window.__fills = [];
(function () {
  var raw = CanvasRenderingContext2D.prototype.fillText, ids = new WeakMap(), next = 1;
  CanvasRenderingContext2D.prototype.fillText = function (text, x, y) {
    if (window.__fills.length < 40000) {
      var id = ids.get(this);
      if (!id) { id = next++; ids.set(this, id); }
      window.__fills.push({t: String(text), x: x, y: y, align: this.textAlign, c: id});
    }
    return raw.apply(this, arguments);
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

    def gps(self, work_dir, session):
        """#14：真鼠标勾「启用校正」、真键盘打时间偏移、真点「应用」。

        这条只有真浏览器给得了答案的地方在两处：勾选与按钮**点得到**（假 DOM 里
        disabled 的控件照样派发 click），以及"应用之后页面会重新载入"——重载是
        刻意的（校正会改切圈边界、每条圈的里程与距离轴，下游太多），所以要真的
        等到新页面起来，再读**新页面**里的轨迹，看它有没有按偏移量平移。
        """
        path = os.path.join(work_dir, session + ".gps.json")
        if os.path.exists(path):
            os.remove(path)

        def on_disk():
            if not os.path.exists(path):
                return None
            try:
                with open(path, encoding="utf-8") as handle:
                    return json.load(handle)
            except (OSError, ValueError):
                return None

        def rect(element_id):
            raw = self.js("JSON.stringify(__rectOf('%s'))" % element_id)
            return json.loads(raw) if raw and raw != "null" else None

        def note_text():
            return self.js("(document.getElementById('gpsNote')||{}).textContent||''")

        def first_time():
            return self.js("(i3pro.data.track&&i3pro.data.track.time||[null])[0]")

        def wait_ready(timeout=25.0):
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    if self.js("!!(window.i3pro && i3pro.state && i3pro.data"
                               " && i3pro.data.track && i3pro.data.gps)"):
                        return True
                except Exception:
                    pass
                time.sleep(0.25)
            return False

        self.check("#14 面板给出了这一场的坏定位计数",
                   "标记：" in note_text(), note_text()[:80])
        self.check("#14 缺省不校正（侧车还没写过）", on_disk() is None)
        self.check("#14 「应用」在 serve 模式下点得到", not rect("gpsApply")["disabled"])
        before_time = first_time()

        # 1) 真鼠标勾上、真点应用 -> 侧车出现，页面重载
        box = rect("gpsEnabled")
        self.browser.click(box["x"], box["y"], self.session)
        time.sleep(0.2)
        self.check("#14 真鼠标点得动「启用校正」",
                   self.js("document.getElementById('gpsEnabled').checked") is True)
        apply_box = rect("gpsApply")
        self.browser.click(apply_box["x"], apply_box["y"], self.session)
        time.sleep(0.6)
        disk = on_disk()
        self.check("#14 点「应用」把配置写进侧车", bool(disk) and disk.get("enabled") is True,
                   disk)
        reloaded = wait_ready()
        self.check("#14 应用之后页面重新载入并带上新配置", reloaded
                   and self.js("i3pro.data.gps.config.enabled") is True)
        if not reloaded:
            return
        # 重载会把注入的 HELPERS 一起冲掉，__rectOf / __center 得重新注入
        self.browser.js(HELPERS, self.session)
        self.check("#14 重载后面板说得出改了什么", "已应用" in note_text(), note_text()[:90])

        # 2) 真键盘打 5 秒偏移，再应用 -> 新页面里轨迹整体平移 5 秒
        offset_box = rect("gpsOffset")
        self.browser.click(offset_box["x"], offset_box["y"], self.session)
        self.browser.key_named("a", "KeyA", 65, self.session, modifiers=2)
        self.browser.key("5", self.session)
        self.check("#14 真键盘打得进时间偏移",
                   str(self.js("document.getElementById('gpsOffset').value")) == "5")
        apply_box = rect("gpsApply")
        self.browser.click(apply_box["x"], apply_box["y"], self.session)
        time.sleep(0.6)
        disk = on_disk() or {}
        self.check("#14 时间偏移存进了侧车", abs(float(disk.get("offset_s", 0)) - 5.0) < 1e-9,
                   disk)
        if not wait_ready():
            self.check("#14 改偏移后页面重新载入", False)
            return
        self.browser.js(HELPERS, self.session)
        after_time = first_time()
        self.check("#14 轨迹真的平移了 5 秒（效果看得见，不只是存了个数）",
                   after_time is not None and before_time is not None
                   and abs((after_time - before_time) - 5.0) < 0.05,
                   "%s -> %s" % (before_time, after_time))
        # 页头说了"断开 N 处"，载荷里就得真有那么多断点；反过来，一场一个跳点、
        # 一段空档都没有时（高避5圈）页头也不许凭空说有。两边一起判，是因为
        # 单看一边都对得出来："总有东西可报"和"永远不报"都能骗过一半的断言。
        head = self.js("(function(){var b=i3pro.bundleOf(i3pro.state.components"
                       ".filter(function(c){return c.type==='track';})[0]);"
                       "return b&&b.head?b.head.textContent:'';})()")
        breaks = json.loads(self.js(
            "JSON.stringify((i3pro.data.track&&i3pro.data.track.breaks)||[])") or "[]")
        jumped = self.js("(i3pro.data.gps.summary||{}).jumps") or 0
        self.check("#14 页头与载荷一致：真有断点才写「断开」，没有就不写",
                   ("断开" in (head or "")) == (len(breaks) > 0),
                   "%s | breaks=%s | 跳点=%s" % (head, breaks, jumped))
        if jumped:
            self.check("#14 这一场真有跳点，答案里就得有那处断开",
                       len(breaks) > 0 and "断开 1 处" in (head or ""),
                       "%s | breaks=%s" % (head, breaks))
            # 断的必须是那 214 m 的幽灵线，而不是它前面那段正常线：抽稀一旦把断点
            # 错算到桶首，图上就会画出一条通向跳点的直线（这一条正是真机截图抓出来的）
            phantom = self.js(
                "(function(){var t=i3pro.data.track,x=breaks=t.breaks||[];"
                "if(!x.length)return 0;var i=x[0];"
                "return Math.hypot(t.x[i]-t.x[i-1],t.y[i]-t.y[i-1]);})()")
            self.check("#14 被断开的那一段就是幽灵线本身（不是它前面那段）",
                       float(phantom or 0) > 200.0, "断开的段长 %s m" % phantom)

        # 3) 收尾：删掉侧车，别把这一场的校正状态留给下一次运行
        try:
            os.remove(path)
        except OSError:
            pass

    def notes(self, work_dir, session):
        """#15：真鼠标加一条注释、真键盘改字、真点 ✕ 删掉，每一步都要落到侧车。

        另外两条只有真浏览器给得了的答案：那行字**真的画在画布上**（`AXIS_HOOK`
        把每次 fillText 都记下来了），以及加注释**没有动圈速表**（注释不是信标）。
        """
        path = os.path.join(work_dir, session + ".notes.json")
        if os.path.exists(path):
            os.remove(path)
        self.js("(function(){i3pro.state.notes=[];i3pro.renderNotes();"
                "i3pro.state.view=fullRange();i3pro.renderAll();return true;})()")
        time.sleep(0.4)

        def on_disk():
            if not os.path.exists(path):
                return None
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)

        def button():
            return json.loads(self.js("JSON.stringify(__rectOf('addNote'))"))

        def lap_rows():
            return int(self.js(
                "document.querySelectorAll('#lapTable tr[data-lap]').length") or 0)

        rows_before = lap_rows()
        self.check("#15 「＋ 注释」在 serve 模式下可点", not button()["disabled"])

        # 1) 先把光标清掉：没光标时说清"先把鼠标移到图上"，而不是默默加在 0 s
        self.js("(function(){i3pro.state.cursor=null;i3pro.renderAll();return true;})()")
        self.browser.click(button()["x"], button()["y"], self.session)
        time.sleep(0.4)
        self.check("#15 没设光标就按＋注释 -> 说清楚先移鼠标，并且不落盘",
                   on_disk() is None and "鼠标" in self.toast(), self.toast())

        # 2) 真点在图上 -> 真点＋注释 -> 侧车里多一条，时刻就是光标那儿
        x = self.js("__px((lane()[0]+lane()[1])/2)")
        y = self.js("__py(60)")
        self.browser.click(x, y, self.session)
        cursor = self.js("i3pro.exactCursorTime()")
        self.browser.click(button()["x"], button()["y"], self.session)
        time.sleep(1.0)
        disk = on_disk() or {}
        rows = disk.get("notes") or []
        self.check("#15 真点「＋ 注释」-> 侧车里多一条，时刻是光标处",
                   len(rows) == 1 and abs(rows[0]["time"] - (cursor or 0)) < 0.01,
                   "t=%s，光标 %s，盘上 %s" % (rows[0]["time"] if rows else None,
                                              cursor, rows))

        # 3) 真键盘改那行字：光标已经落在新加的那一行里（省掉再去点一下）
        box = json.loads(self.js(
            "(function(){var b=document.querySelector('#notesList input[data-note-text]');"
            "return b?JSON.stringify({value:b.value,x:__center(b).x,y:__center(b).y,"
            "focused:document.activeElement===b}):null;})()"))
        self.browser.click(box["x"], box["y"], self.session)
        self.browser.key_named("a", "KeyA", 65, self.session, modifiers=2)
        self.browser.insert_text("这里换了刹车点", self.session)
        self.browser.key_named("Enter", "Enter", 13, self.session)
        time.sleep(1.0)
        rows = (on_disk() or {}).get("notes") or []
        self.check("#15 真键盘改字 + 回车 -> 侧车里的文字变了",
                   len(rows) == 1 and rows[0]["text"] == "这里换了刹车点",
                   json.dumps(rows, ensure_ascii=False))

        # 4) 那行字真的画在画布上（读的是真浏览器记下来的 fillText）
        fills = json.loads(self.js("JSON.stringify((window.__fills||[]).slice(-4000))"))
        drawn = [f["t"] for f in fills if "刹车点" in str(f.get("t", ""))]
        self.check("#15 注释真的画在图上（真画布上的那行字）", bool(drawn), drawn[:3])
        self.browser.shot(os.path.join(ROOT, "out", "shots", "verify-notes.png"), self.session)

        # 5) Esc 取消不落盘
        before = open(path, "rb").read()
        self.browser.click(box["x"], box["y"], self.session)
        self.browser.key_named("a", "KeyA", 65, self.session, modifiers=2)
        self.browser.insert_text("改了但不算数", self.session)
        self.browser.key_named("Escape", "Escape", 27, self.session)
        time.sleep(0.5)
        self.check("#15 Esc 取消 -> 侧车一个字节没动",
                   open(path, "rb").read() == before,
                   json.dumps((on_disk() or {}).get("notes"), ensure_ascii=False))

        # 6) 注释不是信标：加了注释，圈速表一行都不许变
        self.check("#15 加了注释之后圈速表一行没变（注释不参与切圈）",
                   lap_rows() == rows_before,
                   "加之前 %d 行，加之后 %d 行" % (rows_before, lap_rows()))

        # 7) 真点 ✕ 删掉
        delete = json.loads(self.js(
            "(function(){var a=document.querySelector('#notesList a[data-note-del]');"
            "return a?JSON.stringify(__center(a)):null;})()"))
        self.browser.click(delete["x"], delete["y"], self.session)
        time.sleep(1.0)
        rows = (on_disk() or {}).get("notes") or []
        self.check("#15 真点 ✕ -> 侧车里的那条没了", rows == [],
                   json.dumps(rows, ensure_ascii=False))

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

    def spectrum(self):
        """#10：频谱——加得出来、真画出曲线、换参数/缩放会重问、纵轴只是显示。

        这里验的是假 DOM 证明不了的：新组件在真浏览器里**真的画出了曲线**（读回画布
        像素）、原生下拉的键盘操作**真的发出去了**（看 __fetchLog）、以及"换纵轴不该
        重新问服务端"（那只是同一份功率谱换个写法）。
        """
        def comp_json():
            raw = self.js(
                "(function(){var a=i3pro.state.components.filter(function(c){"
                "return c.type==='spectrum';});var c=a[a.length-1];"
                "return c?JSON.stringify({id:c.id,channel:c.config.channel,"
                "points:c.config.points,win:c.config.win}):'null';})()"
            )
            return json.loads(raw) if raw and raw != "null" else None

        def spec_calls():
            raw = self.js("JSON.stringify(window.__fetchLog.filter(function(e){"
                          "return e.url.indexOf('/spectrum')>=0;}))")
            return json.loads(raw) if raw else []

        def wait_for_spec(seen, needle, timeout=5.0):
            """有界轮询：等第 seen 条之后出现一条含 needle 的 /spectrum 请求。"""
            deadline = time.time() + timeout
            while time.time() < deadline:
                calls = spec_calls()
                for call in calls[seen:]:
                    if needle in call["url"]:
                        return calls
                time.sleep(0.2)
            return spec_calls()

        before = self.js("i3pro.state.components.length")
        self.js("(function(){document.getElementById('addType').value='spectrum';"
                "return true;})()")
        add = json.loads(self.js("JSON.stringify(__rectOf('addBtn'))"))
        self.browser.click(add["x"], add["y"], self.session)
        self.browser.wait_for("i3pro.state.components.length === %d" % (before + 1),
                              self.session, timeout=10)
        time.sleep(1.2)
        comp = comp_json()
        self.check("#10 真点「＋ 组件」-> 工作表里多了一个频谱，并且自己挑好了一条通道",
                   bool(comp) and bool(comp["channel"]),
                   json.dumps(comp, ensure_ascii=False) if comp else "没有频谱组件")
        if not comp:
            return

        painted = self.js(
            "(function(){var cv=document.querySelector('.comp[data-id=\"%s\"] canvas');"
            "if(!cv)return -1;var d=cv.getContext('2d').getImageData(0,0,cv.width,cv.height).data;"
            "var n=0;for(var i=3;i<d.length;i+=4){if(d[i]>0)n++;}return n;})()" % comp["id"]
        )
        self.check("#10 频谱画布上真的有曲线（不是一张白纸）",
                   isinstance(painted, (int, float)) and painted > 500,
                   "%s 个像素" % painted)
        self.browser.shot(os.path.join(ROOT, "out", "shots", "verify-spectrum.png"),
                          self.session)

        # 换窗函数：原生下拉 + 真键盘（ArrowDown 就会派发 change）
        win_box = self.js(
            "(function(){var el=document.querySelector('.comp[data-id=\"%s\"]"
            " select[data-spec=\"win\"]');if(!el)return 'null';el.focus();return 'ok';})()"
            % comp["id"]
        )
        if win_box == "ok":
            seen = len(spec_calls())
            self.browser.key_named("ArrowDown", "ArrowDown", 40, self.session)
            calls = wait_for_spec(seen, "window=")
            new_comp = comp_json() or {}
            fresh = calls[seen:] if len(calls) > seen else []
            self.check("#10 真键盘换窗函数 -> 用新窗重新问了服务端",
                       bool(fresh) and any("window=" in c["url"] for c in fresh)
                       and new_comp.get("win") != comp["win"],
                       "请求=%s；配置 %s -> %s" % (
                           fresh[-1]["url"].split("?")[-1] if fresh else "(没有请求)",
                           comp["win"], new_comp.get("win")))
        else:
            self.check("#10 真键盘换窗函数 -> 用新窗重新问了服务端", False, "没有窗函数下拉")

        # 缩放：真双击图 -> 频谱必须跟着重问一次，而且请求里带着新窗口
        seen = len(spec_calls())
        x = self.js("__px((i3pro.lane()[0]+i3pro.lane()[1])/2)")
        y = self.js("__py(60)")
        before_view = self.view()
        self.browser.double_click(x, y, self.session)
        time.sleep(1.0)
        calls = spec_calls()
        view = self.view()
        self.check("#10 真双击放大 -> 频谱跟着换了窗口（重新问了一次）",
                   len(calls) > seen and (view[1] - view[0]) < (before_view[1] - before_view[0]),
                   "view %.1f-%.1f -> %.1f-%.1f，请求 %d 次"
                   % (before_view[0], before_view[1], view[0], view[1], len(calls) - seen))

        # 纵轴只是显示：切 dB / 线性不该再问一次服务端（服务端一律回功率谱密度）
        axis_box = self.js(
            "(function(){var el=document.querySelector('.comp[data-id=\"%s\"]"
            " select[data-spec=\"axis\"]');if(!el)return 'null';el.focus();return 'ok';})()"
            % comp["id"]
        )
        if axis_box == "ok":
            seen = len(spec_calls())
            self.browser.key_named("ArrowDown", "ArrowDown", 40, self.session)
            time.sleep(1.2)
            calls = spec_calls()
            self.check("#10 切纵轴（dB/线性）不该重新问服务端：那是同一份功率谱的写法",
                       len(calls) == seen,
                       "又多发了 %d 个 /spectrum 请求" % (len(calls) - seen))
        else:
            self.check("#10 切纵轴（dB/线性）不该重新问服务端：那是同一份功率谱的写法",
                       False, "没有纵轴下拉")

    def axis(self):
        """横轴随缩放换档（A36）：读真画布**画出来的**刻度文字。

        为什么非要这一道：canvas 里没有文字节点，`smoke_viewer.js` 又跑在假 canvas
        上（`drawGrid` 那几行在它那里根本没被调用），所以"整场印 0:00/1:00/…、缩到
        1 s 印 231.80"这句话此前只有截图能证明，而截图下次改坏了不会报红。

        断言的是**画出来的字符串**：按 (canvas, y) 还原出一条条横轴，再读回数值。
        """

        def fills():
            raw = self.js("JSON.stringify(window.__fills)")
            return json.loads(raw) if raw and raw != "null" else []

        def axis_rows():
            """同一个 y 上 >= 3 个居中的、能读成数字的标签 = 一条横轴。"""
            groups = {}
            for fill in fills():
                if fill.get("align") != "center":
                    continue
                if _label_value(fill.get("t")) is None:
                    continue
                key = (fill.get("c"), round(float(fill["y"]), 3))
                groups.setdefault(key, []).append(fill)
            rows = []
            for (canvas, y), items in groups.items():
                if len(items) < 3:
                    continue
                items.sort(key=lambda f: float(f["x"]))
                xs = [float(f["x"]) for f in items]
                values = [_label_value(f["t"]) for f in items]
                steps = [round(values[i + 1] - values[i], 9) for i in range(len(values) - 1)]
                rows.append({
                    "canvas": canvas, "y": y,
                    "labels": [f["t"] for f in items], "values": values,
                    "step": steps[0] if steps else 0.0,
                    "even_steps": len(set(steps)) == 1,
                    "gaps": [round(xs[i + 1] - xs[i], 3) for i in range(len(xs) - 1)],
                    "span": xs[-1] - xs[0],
                })
            return rows

        def shoot(a, b, wait=1.0):
            """换一段视图并重画，再把刚画出来的横轴读回来。

            一定要换一段**不同的**区间：绘图区有离屏缓存，区间没变就不会重画，
            `fillText` 一次都不会被调用，读回来的会是上一轮的残留。
            """
            self.js("window.__fills=[];i3pro.state.view=[%r,%r];i3pro.renderAll();true" % (a, b))
            time.sleep(wait)
            return axis_rows()

        def text_of(row):
            return "%s（步长 %s，%d 条：%s）" % (row["labels"][0], row["step"],
                                                 len(row["labels"]), " ".join(row["labels"]))

        def clean(row, on_clock):
            """一条横轴读不读得出来：步长一致、落在档位上、标签之间留得下字。"""
            ladder_ok = _clock_ladder(row["step"]) if on_clock else _generic_ladder(row["step"])
            return (row["even_steps"] and ladder_ok and min(row["gaps"]) >= 40
                    and max(row["gaps"]) - min(row["gaps"]) <= 1.5 and len(row["labels"]) <= 13)

        def aligned(row):
            """刻度落在整齐的数上（231.8 配 0.2 的档位，不是 231.83）。"""
            return all(abs(v / row["step"] - round(v / row["step"])) < 1e-6
                       for v in row["values"])

        duration = float(self.js("i3pro.data.meta.duration") or 0.0)
        self.js("i3pro.state.mode='time';i3pro.state.view=null;i3pro.renderAll();true")
        time.sleep(0.4)

        # 一路缩下去，每一档都把**画出来的**横轴读回来。第一屏里印钟点的那几块画布
        # 就是"时间/距离图"；后面几屏只认它们，免得把散点图、直方图的行混进来。
        windows = [("整场", 0.0, duration), ("1/4 场", 0.0, duration / 4.0),
                   ("20 s", 100.0, 120.0), ("1 s", 231.7, 232.7)]
        shots, graph_ids = [], set()
        for label, a, b in windows:
            rows = shoot(a, b)
            if not graph_ids:
                graph_ids = {r["canvas"] for r in rows
                             if all(":" in lab for lab in r["labels"])}
            shots.append((label, rows))

        def graph_rows(rows):
            picked = [r for r in rows if r["canvas"] in graph_ids]
            return picked or rows

        # 1) 整场：走钟表档位（高避 464 s -> 60 s），标签是 0:00 / 1:00 / …
        whole = [r for r in graph_rows(shots[0][1]) if all(":" in lab for lab in r["labels"])]
        self.check("#36 整场的时间轴印钟点标签（0:00 / 1:00 / …）",
                   bool(whole) and all(clean(r, True) and aligned(r) for r in whole),
                   " | ".join(text_of(r) for r in whole) or "一条横轴都没读到")

        # 2) 缩得越窄步长只许越小，且每一档都还读得出来（标签不重叠、落在档位上）
        steps, problems, seen = [], [], []
        for label, rows in shots:
            picked = graph_rows(rows)
            clock = [r for r in picked if all(":" in lab for lab in r["labels"])]
            if not picked:
                problems.append("%s：一条横轴都没读到" % label)
                continue
            steps.append(picked[0]["step"])
            seen.append("%s -> %ss" % (label, [r["step"] for r in picked]))
            for row in picked:
                if not (clean(row, True) and aligned(row)):
                    problems.append("%s：%s" % (label, text_of(row)))
            if label != "1 s" and not clock:
                problems.append("%s：时间轴没有印钟点标签，读到的是 %s"
                                % (label, picked[0]["labels"]))
        self.check("#36 缩得越窄，时间轴的档位只降不升（%s）"
                   % " -> ".join(str(s) for s in steps),
                   len(steps) == len(windows) and not problems
                   and all(steps[i + 1] <= steps[i] + 1e-9 for i in range(len(steps) - 1)),
                   "；".join(problems) if problems else "；".join(seen))
        tiny = [r for r in graph_rows(shots[-1][1])
                if all(("." in lab and ":" not in lab) for lab in r["labels"])]
        self.check("#36 缩到 1 s 时改成亚秒档（标签带小数、不再是钟点）",
                   bool(tiny) and all(r["step"] < 1.0 for r in tiny)
                   and steps[0] >= 1.0,
                   "整场 %s s -> 1 s 窗口：%s"
                   % (steps[0] if steps else "?",
                      " | ".join(text_of(r) for r in tiny) or "没读到亚秒标签"))

        # 3) 距离轴不该出现钟点标签（这就是"别从标签里猜是不是时间轴"的理由）
        span = json.loads(self.js(
            "(function(){i3pro.state.mode='distance';i3pro.state.view=null;i3pro.renderAll();"
            "return JSON.stringify(lane());})()"))
        rows = shoot(span[0] + (span[1] - span[0]) * 0.3, span[1])
        clock = [r for r in rows if any(":" in lab for lab in r["labels"])]
        widest = max(rows, key=lambda r: r["span"]) if rows else None
        self.check("#36 距离轴不印钟点标签，且步长落在 1/2/5 档上",
                   not clock and widest is not None
                   and clean(widest, False) and aligned(widest),
                   ("距离轴读到钟点标签：%s" % " ".join(clock[0]["labels"])) if clock
                   else (text_of(widest) if widest else "一条横轴都没读到"))
        self.browser.shot(os.path.join(ROOT, "out", "shots", "verify-axis-zoom.png"), self.session)

        self.js("i3pro.state.mode='time';i3pro.state.view=[0,%r];i3pro.renderAll();true" % duration)
        time.sleep(0.4)

    def export(self, work_dir):
        """#23/#24：真点「导出数据」→ 面板拿到服务端的预估 → 真落一个文件。

        假 DOM 验不了这一段：``fetch`` 在那里永远失败、``URL.createObjectURL`` 也
        不存在。这里用真实的下载行为，连"临时文件不许残留"一起钉住。
        """
        # 开工前先记下机器上已有的导出临时目录：本机的判断只针对"这次跑出来的"那些。
        exports_before = set(glob.glob(os.path.join(tempfile.gettempdir(), "i3pro-export-*")))
        point = json.loads(self.js(
            "(function(){var b=document.getElementById('dataBtn');"
            "b.scrollIntoView({block:'nearest'});"
            "var r=b.getBoundingClientRect();"
            "return JSON.stringify({x:r.left+r.width/2,y:r.top+r.height/2});})()"
        ))
        self.browser.click(point["x"], point["y"], self.session)
        self.check("点工具栏的「导出数据」打开了面板",
                   self.js("!document.getElementById('exportDlg').hidden"))
        # 面板一开就向服务端要一次预估（不是界面自己拍的数）
        got_plan = self.browser.wait_for(
            "document.getElementById('exportPlan').textContent.indexOf('行') > 0",
            self.session, timeout=30,
        )
        self.check("面板显示服务端算出的预估", got_plan,
                   self.js("document.getElementById('exportPlan').textContent"))

        download_dir = os.path.join(work_dir, "_downloads")
        shutil.rmtree(download_dir, ignore_errors=True)
        os.makedirs(download_dir)
        self.browser.call("Browser.setDownloadBehavior",
                          {"behavior": "allow", "downloadPath": download_dir})
        # 只导 2 秒、10 Hz：验的是"这条路通"，不是"能导多少"
        self.js(
            "i3pro.applyExportConfig({range:'time',from:'10',to:'12',channels:'all',"
            "maths:true,rate:'10',custom:'',resample:'linear',meta:false,axis:'time',"
            "format:'csv',layout:'wide'}); i3pro.refreshExportPlan(); true"
        )
        small = self.browser.wait_for(
            "document.getElementById('exportPlan').textContent.indexOf('21') >= 0",
            self.session, timeout=30,
        )
        self.check("把范围改成 10–12 s、10 Hz 之后预估变成 21 行", small,
                   self.js("document.getElementById('exportPlan').textContent"))

        # #27：主索引换成绝对时间戳（同一根时间轴换一种写法），要能拿到服务端的预估，
        # 而且真导出来的首行必须就是 `timestamp,`——这条只有真服务 + 真数据才算数。
        self.js(
            "i3pro.applyExportConfig({range:'time',from:'10',to:'12',channels:'all',"
            "maths:true,rate:'10',custom:'',resample:'linear',meta:false,axis:'timestamp',"
            "format:'csv',layout:'wide'}); i3pro.refreshExportPlan(); true"
        )
        stamped = self.browser.wait_for(
            "document.getElementById('exportPlan').textContent.indexOf('timestamp') >= 0",
            self.session, timeout=30,
        )
        self.check("主索引选绝对时间戳后，预估里写明 timestamp（#27）", stamped,
                   self.js("document.getElementById('exportPlan').textContent"))
        probe = self.js(
            "(async()=>{const u=i3pro.exportURL({range:'time',from:'10',to:'12',"
            "channels:'all',maths:true,rate:'10',custom:'',resample:'linear',meta:false,"
            "axis:'timestamp',format:'csv',layout:'wide'},false).href;"
            "const r=await fetch(u);const t=await r.text();"
            "return r.status+'|'+t.slice(0,40);})()"
        )
        self.check("绝对时间戳导出的首行就是 timestamp,（真服务 + 真数据）",
                   str(probe).startswith("200|") and "timestamp," in str(probe),
                   str(probe)[:120])
        # 换回相对秒：下面要**真下载**一个文件，那几条断言认的表头是 time_s
        self.js(
            "i3pro.applyExportConfig({range:'time',from:'10',to:'12',channels:'all',"
            "maths:true,rate:'10',custom:'',resample:'linear',meta:false,axis:'time',"
            "format:'csv',layout:'wide'}); true"
        )

        go = json.loads(self.js(
            "(function(){var b=document.getElementById('exportGo');"
            "var r=b.getBoundingClientRect();"
            "return JSON.stringify({x:r.left+r.width/2,y:r.top+r.height/2});})()"
        ))
        before_errors = len(self.browser.page_errors())
        self.browser.click(go["x"], go["y"], self.session)
        self.browser.wait_for(
            "document.getElementById('exportPlan').textContent.indexOf('已导出') >= 0",
            self.session, timeout=60,
        )
        plan = self.js("document.getElementById('exportPlan').textContent")
        self.check("点「导出」之后面板说已导出（真的走完了 fetch + Blob）",
                   "已导出" in plan, plan)

        landed = []
        for _ in range(160):
            landed = [f for f in os.listdir(download_dir) if not f.endswith(".crdownload")]
            if landed:
                break
            time.sleep(0.25)
        if landed:
            path = os.path.join(download_dir, landed[0])
            with open(path, "rb") as handle:
                head = handle.read(4096)
            first = head.decode("utf-8-sig", "replace").splitlines()[0]
            self.check("浏览器真的落了一个文件在下载目录", os.path.getsize(path) > 0,
                       "%s (%d B)" % (landed[0], os.path.getsize(path)))
            self.check("文件名来自 Content-Disposition（中文名没被吃掉）",
                       landed[0].endswith(".csv") and "-" in landed[0], landed[0])
            self.check("CSV 带 BOM（Excel 双击不乱码）", head.startswith(b"\xef\xbb\xbf"))
            self.check("首行表头是 time_s + 通道名 [单位]",
                       first.startswith("time_s,") and "[" in first, first[:90])
            self.check("行数与面板报的一致（21 行数据 + 1 行表头）",
                       len(open(path, "rb").read().decode("utf-8-sig").splitlines()) == 22)
        else:
            # 旧版无头偶尔不落盘。退一步：同一台服务、同一条路，在页面里直接取一次，
            # 断言写成"接口那一半过了"，不冒充"真下载过了"。
            probe = self.js(
                "(async()=>{const r=await fetch(i3pro.exportURL(i3pro.exportConfig(),false).href);"
                "const t=await r.text();return r.status+'|'+t.slice(0,90);})()"
            )
            self.check("下载没落盘时改验接口：200 + 首行表头",
                       str(probe).startswith("200|time_s,"), str(probe)[:120])

        # 只对**这次跑出来的**临时目录下结论：这台机器上可能有别的进程（或被杀掉的旧服务）
        # 留下的空目录，拿它们报红等于让一条永远红着的断言教人忽略它。
        leftovers = [p for p in glob.glob(os.path.join(tempfile.gettempdir(), "i3pro-export-*"))
                     if p not in exports_before]
        self.check("导出临时目录没残留（成功路径也要删）", not leftovers, leftovers[:3])
        self.check("导出过程没有页面级报错",
                   len(self.browser.page_errors()) == before_errors,
                   self.browser.page_errors()[:3])

        # 「再点一次就是取消」（需求 §5：支持取消、取消后不许留半个文件）。
        # 挑一个够大的导出（整场 + 原始采样 + 全通道），点下去之后立刻看到按钮变成
        # 「取消」，再点一次，面板要自己说"已取消"，而且**没有文件落地、没有临时目录**。
        self.js(
            "i3pro.applyExportConfig({range:'all',from:'',to:'',channels:'all',maths:true,"
            "rate:'auto',custom:'',resample:'linear',meta:false,axis:'time',format:'csv',"
            "layout:'wide'}); true"
        )
        big = json.loads(self.js(
            "(function(){var b=document.getElementById('exportGo');"
            "var r=b.getBoundingClientRect();"
            "return JSON.stringify({x:r.left+r.width/2,y:r.top+r.height/2});})()"
        ))
        before_files = set(os.listdir(download_dir))
        self.browser.click(big["x"], big["y"], self.session)
        running = self.browser.wait_for(
            "document.getElementById('exportGo').textContent.indexOf('取消') >= 0",
            self.session, timeout=30,
        )
        self.check("导出中「导出」按钮自己变成「取消」", running,
                   self.js("document.getElementById('exportGo').textContent"))
        self.browser.click(big["x"], big["y"], self.session)      # 再点一次 = 取消
        stopped = self.browser.wait_for(
            "document.getElementById('exportPlan').textContent.indexOf('已取消') >= 0",
            self.session, timeout=30,
        )
        self.check("再点一次就取消，面板说已取消", stopped,
                   self.js("document.getElementById('exportPlan').textContent"))
        time.sleep(1.0)
        self.check("取消之后没有文件落地",
                   set(os.listdir(download_dir)) == before_files,
                   sorted(os.listdir(download_dir)))
        for _ in range(40):            # 服务端清理临时目录是异步的，等它一下
            leftovers = [p for p in glob.glob(
                os.path.join(tempfile.gettempdir(), "i3pro-export-*")) if p not in exports_before]
            if not leftovers:
                break
            time.sleep(0.25)
        self.check("取消之后服务端临时目录也不残留", not leftovers, leftovers[:3])
        # 收尾：换回小配置，免得影响后面别的用例读面板状态
        self.js(
            "i3pro.applyExportConfig({range:'time',from:'10',to:'12',channels:'all',"
            "maths:true,rate:'10',custom:'',resample:'linear',meta:false,axis:'time',"
            "format:'csv',layout:'wide'}); true"
        )
        # 收尾：把面板关掉，后面的用例在干净状态下跑
        self.js("i3pro.closeExportDialog(); true")

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
        # 验收从**干净的浏览器状态**开始：上一次跑留下的工作表存在 localStorage 里
        # （开着几个组件、什么通道），不清掉的话这一跑就不是在验同一件事。
        profile = os.path.join(ROOT, "out", "_edge_profile_%d" % args.cdp_port)
        shutil.rmtree(profile, ignore_errors=True)
        browser = Browser(
            edge, args.cdp_port, profile
        )
        url = "http://127.0.0.1:%d/session/%s" % (args.port, urllib.parse.quote(args.session))
        session = browser.open(url, wait=1.0, init_script=AXIS_HOOK)
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
            checker.export(work_dir)
            checker.histogram()
            checker.axis()
            checker.notes(work_dir, args.session)
            checker.gps(work_dir, args.session)
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
