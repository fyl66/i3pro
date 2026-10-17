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
import hashlib
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
import zipfile

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

    def key_named(self, key, code, windows_code, session, modifiers=0, text=None):
        """发一次真按键。``text`` 只有需要"按键默认动作"时才给（回车要它才会
        真的激活聚焦的那个按钮；不带 text 时页面只收到一个 keydown，什么也不发
        生——A78 那条"回车跳得动"就是这么空转了半个版本）。"""
        for kind in ("keyDown", "keyUp"):
            event = {
                "type": kind, "key": key, "code": code,
                "windowsVirtualKeyCode": windows_code,
                "nativeVirtualKeyCode": windows_code, "modifiers": modifiers,
            }
            if text is not None and kind == "keyDown":
                event["text"] = text
            self.call("Input.dispatchKeyEvent", event, session=session)
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


def stage_worksheets(work_dir):
    """把仓库里的 worksheets/ 复制一份当"仓库根"，返回那个根。

    ticket #33 的验收会真的新建 / 改名 / 删除工作表文件，所以服务必须指向副本——
    和 `.ld` 那条理由一样：随便点都不该碰到车队仓库里的文件。
    """
    root = os.path.join(work_dir, "_worksheets_root")
    target = os.path.join(root, "worksheets")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(target)
    source = os.path.join(ROOT, "worksheets")
    for name in sorted(os.listdir(source)):
        if name.endswith(".json"):
            shutil.copy2(os.path.join(source, name), os.path.join(target, name))
    return root


def _tree(directory):
    """目录里每个文件的内容哈希；用来判"这一趟有没有动到不该动的文件"。"""
    out = {}
    if not os.path.isdir(directory):
        return out
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            with open(path, "rb") as handle:
                out[name] = hashlib.sha1(handle.read()).hexdigest()
    return out


def start_server(work_dir, port, worksheets_root=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    log = open(os.path.join(ROOT, "out", "_verify_serve.log"), "w", encoding="utf-8")
    argv = [sys.executable, "-m", "i3pro", "serve", "--data", work_dir,
            "--host", "127.0.0.1", "--port", str(port)]
    if worksheets_root:
        argv += ["--worksheets", worksheets_root]
    proc = subprocess.Popen(
        argv,
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

    def worksheets(self):
        """ticket #30：顶上那排按钮真的点得到、真的换布局，而且按场次记。

        为什么非要真鼠标：那些按钮的宽度由文字撑开、间距由 flex 给，假 DOM 里没有
        面积也没有遮挡——"点了没反应"和"点到了旁边那个"只有真命中测试说得清。
        顺带把"工作表那一栏没有报错"这条也读出来（仓库里那几份文件本来就不该有问题）。
        """
        names = json.loads(self.js(
            "JSON.stringify(Array.prototype.map.call("
            "document.querySelectorAll('#presetRow button'), function(b){return b.textContent;}))"))
        self.check("#30 工作表按钮 = 仓库里的 worksheets/*.json",
                   len(names) >= 5 and "分析" in names, names)
        note = self.js("(document.getElementById('sheetNote')||{}).textContent||''")
        self.check("#30 仓库里那几份工作表没有问题（这一栏不该有字）", not note.strip(), note)

        def click_sheet(index):
            box = json.loads(self.js(
                "(function(){var b=document.querySelectorAll('#presetRow button')[%d];"
                "return b?JSON.stringify(__center(b)):null;})()" % index))
            self.browser.click(box["x"], box["y"], self.session)
            time.sleep(0.8)
            return json.loads(self.js(
                "JSON.stringify({preset:i3pro.state.preset,"
                "comps:i3pro.state.components.length,"
                "types:i3pro.state.components.map(function(c){return c.type;})})"))

        first = click_sheet(0)
        last = click_sheet(len(names) - 1)
        self.check("#30 真点最后一套 -> 换成那一套的组件",
                   last["preset"] == names[-1] and last["comps"] > 0
                   and last["comps"] != first["comps"],
                   "%s -> %s" % (first, last))
        stored = json.loads(self.js(
            "(function(){var s=i3pro.data.session||'session';var k='i3pro.worksheet.v2:'+s;"
            "return JSON.stringify({session:s,key:k,raw:localStorage.getItem(k),"
            "legacy:localStorage.getItem('i3pro.components.v1')});})()"))
        self.check("#30 布局记忆按场次存下来（键 = 前缀 + 场次名）",
                   bool(stored["session"]) and stored["session"] != "session"
                   and stored["key"] == "i3pro.worksheet.v2:" + stored["session"],
                   stored["key"])
        self.check("#30 记下来的是刚切过去的那一套",
                   bool(stored["raw"]) and json.loads(stored["raw"])["preset"] == names[-1],
                   "raw=%r" % (stored["raw"],))
        self.check("#30 不再往那一格「全场共用」的旧键里写（换场次才不串味）",
                   stored["legacy"] is None, stored["legacy"])

        # 切回第一套：后面的检查按缺省布局来（别把页面留在最后一套上）
        back = click_sheet(0)
        self.check("#30 切回第一套还是它自己的组件",
                   back["preset"] == names[0] and back["comps"] == first["comps"],
                   "%s vs %s" % (back, first))

    def worksheet_editing(self, work_dir):
        """ticket #33：工作表的增删改与进出——**真鼠标点、真键盘打字**。

        为什么非要这一关：这一票的判据全在"仓库里那个文件变没变"上，而假 DOM 里
        既没有服务端也没有文件系统。所以每一步都回到磁盘核对：新建多了一个文件、
        保存把改动落进那份文件、改名换了文件名、同名那次旧文件一个字节没动、
        导入多出一套、删除之后文件不见了、导出的 JSON 落在下载目录里。
        跑完再确认**车队仓库里的 worksheets/ 一个字节没动**（服务跑的是副本）。
        """
        root = os.path.join(work_dir, "_worksheets_root", "worksheets")
        repo_dir = os.path.join(ROOT, "worksheets")
        repo_before = _tree(repo_dir)

        def files():
            return sorted(n for n in os.listdir(root) if n.endswith(".json"))

        def read(name):
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                return json.load(handle)

        def buttons():
            return json.loads(self.js(
                "JSON.stringify(Array.prototype.map.call("
                "document.querySelectorAll('#presetRow button'),"
                "function(b){return b.textContent;}))"))

        def center(element_id):
            raw = self.js(
                "(function(){var el=document.getElementById(%r);"
                "return el?JSON.stringify(__center(el)):null;})()" % element_id)
            return json.loads(raw) if raw and raw != "null" else None

        def click(element_id, label):
            box = center(element_id)
            self.check("#33 「%s」在页面上点得到" % label,
                       bool(box) and box["w"] > 0, box)
            if not box:
                return False
            self.browser.click(box["x"], box["y"], self.session)
            time.sleep(0.7)
            return True

        def type_name(text):
            """真键盘打一个名字：先聚焦、全选，再敲字、回车。"""
            self.js("(function(){var i=document.getElementById('wsAskInput');"
                    "i.focus();i.select();return true;})()")
            self.browser.key(text, self.session)
            self.browser.key_named("Enter", "Enter", 13, self.session)

        def wait_file(name, want=True, timeout=20):
            for _ in range(int(timeout / 0.2)):
                if (name in files()) == want:
                    return True
                time.sleep(0.2)
            return False

        self.check("#33 服务读的是副本目录（车队仓库里的 worksheets/ 不能是它）",
                   os.path.isdir(root) and sorted(os.listdir(root)) != [], root)
        # 只认**随版本发布的那几套**：车队自己存的（比如 fyl-sensor.json）也会在这
        # 个目录里，那是功能本身，不该让这条变红。
        shipped = {"analysis.json", "chassis.json", "compare.json", "dash.json",
                   "driver.json", "powertrain.json", "report.json"}
        self.check("#33 副本里起始就带着仓库里那几套工作表",
                   shipped <= set(files()), files())
        self.check("#33 工作表那一栏多了一排管理按钮",
                   self.js("document.querySelectorAll('#sheetManage button').length") == 7,
                   self.js("document.querySelectorAll('#sheetManage button').length"))

        # ---------------------------------------------------------- 另存为
        if not click("wsSaveAs", "另存为"):
            return
        self.check("#33 点「另存为」弹出名字输入框",
                   self.js("document.getElementById('wsAsk').style.display !== 'none'"))
        type_name("codex-probe")
        landed = wait_file("codex-probe.json")
        self.check("#33 另存为之后 worksheets/ 里真的多了一个文件", landed, files())
        self.check("#33 新那一套立刻上了按钮", "codex-probe" in buttons(), buttons())
        if not landed:
            return
        saved = read("codex-probe.json")
        graphs = [c for c in saved.get("components", []) if c.get("type") == "graph"]
        self.check("#33 没动过的图不把本场次的通道名写死（pick 还在）",
                   bool(graphs) and all(not (c.get("config") or {}).get("channels")
                                        for c in graphs)
                   and all("pick" in c for c in graphs),
                   json.dumps(graphs, ensure_ascii=False)[:200])

        # ------------------------------------------------------------ 保存
        page_before = self.js("i3pro.state.components.length")
        raw = self.js(
            "(function(){var cs=document.querySelectorAll('#worksheet .comp');"
            "if(!cs.length)return null;var el=cs[cs.length-1];"
            "var bs=el.querySelectorAll('.compbar button');"
            "for(var i=0;i<bs.length;i++){if(bs[i].textContent==='✕')"
            "return JSON.stringify(__center(bs[i]));}return null;})()")
        box = json.loads(raw) if raw and raw != "null" else None
        self.check("#33 页面上找得到组件的 ✕", bool(box) and box.get("w", 0) > 0, box)
        if not box:
            return
        self.browser.click(box["x"], box["y"], self.session)
        time.sleep(0.8)
        self.check("#33 真点 ✕ 之后页面上少了一个组件",
                   self.js("i3pro.state.components.length") == page_before - 1,
                   "%s -> %s" % (page_before, self.js("i3pro.state.components.length")))
        click("wsSave", "保存")
        time.sleep(0.5)
        after = len(read("codex-probe.json")["components"])
        self.check("#33 点「保存」之后文件里的组件数跟着变了（真的落盘）",
                   after == len(saved["components"]) - 1,
                   "%s -> %s" % (len(saved["components"]), after))
        self.check("#33 保存之后那一套还在按钮上（没有变成没名字的「自定义」）",
                   self.js("i3pro.state.preset") == "codex-probe",
                   self.js("i3pro.state.preset"))

        # ------------------------------------------ A74：勾的通道保存之后不许丢
        # 修之前这里是单向闩：规则（pick）填过的键再也不写回文件，于是新建的工作表
        # 勾完通道保存一次就回到默认通道。判据落在**磁盘上那份文件**里有没有这条
        # 通道名——那条通道是规则永远填不出来的东西。
        def click_sheet(name):
            raw = self.js(
                "(function(){var n=%s;var bs=document.querySelectorAll('#presetRow button');"
                "for(var i=0;i<bs.length;i++){if(bs[i].textContent===n){"
                "return JSON.stringify(__center(bs[i]));}}return null;})()"
                % json.dumps(name))
            if not raw or raw == "null":
                return False
            box = json.loads(raw)
            self.browser.click(box["x"], box["y"], self.session)
            time.sleep(0.9)
            return True

        picked = json.loads(self.js(
            "(function(){var rows=document.querySelectorAll("
            "'#channelList label.ch:not(.missing)');"
            "for(var i=0;i<rows.length;i++){var inp=rows[i].querySelector('input');"
            "if(inp&&!inp.checked){var c=__center(inp);"
            "return JSON.stringify({name:rows[i].querySelector('.nm').textContent,"
            "x:c.x,y:c.y});}}return null;})()"))
        self.check("#74 侧边栏里找得到一条还没勾的通道（真鼠标要点的）", bool(picked), picked)
        if picked:
            self.browser.click(picked["x"], picked["y"], self.session)
            time.sleep(0.7)
            live = json.loads(self.js("JSON.stringify(i3pro.selectedChannels())"))
            self.check("#74 真点勾选框 -> 这一条进了图上", picked["name"] in live, live)
            click("wsSave", "保存")
            time.sleep(0.9)
            landed = read("codex-probe.json")
            hit = [c for c in landed["components"]
                   if picked["name"] in ((c.get("config") or {}).get("channels") or [])]
            self.check("#74 勾的通道真的落进 worksheets/codex-probe.json", bool(hit),
                       json.dumps(landed["components"], ensure_ascii=False)[:300])
            if click_sheet("分析") and click_sheet("codex-probe"):
                again = json.loads(self.js(
                    "JSON.stringify(i3pro.state.components.map(function(c){"
                    "return (c.config&&c.config.channels)||[];}))"))
                self.check("#74 切走再切回，勾的通道还在（没有变回默认通道）",
                           any(picked["name"] in one for one in again),
                           json.dumps(again, ensure_ascii=False)[:300])

        # ------------------------------------------- 同名另存为：加后缀不覆盖
        before_bytes = open(os.path.join(root, "codex-probe.json"), "rb").read()
        if not click("wsSaveAs", "另存为（第二次）"):
            return
        type_name("codex-probe")
        second = wait_file("codex-probe-2.json")
        self.check("#33 同名另存为自动加后缀，两份都在", second, files())
        self.check("#33 同名另存为没有动旧的那一份",
                   open(os.path.join(root, "codex-probe.json"), "rb").read() == before_bytes)
        self.check("#33 按钮上两份分得清（后缀也加在显示名上）",
                   "codex-probe 2" in buttons(), buttons())

        # ---------------------------------------------------------- 重命名
        if not click("wsRename", "重命名"):
            return
        type_name("codex-renamed")
        renamed = wait_file("codex-renamed.json") and wait_file("codex-probe-2.json", False)
        self.check("#33 重命名之后文件名跟着变了、旧文件删掉了", renamed, files())
        if not renamed:
            return
        self.check("#33 改名之后按钮上也是新名字",
                   "codex-renamed" in buttons() and "codex-probe 2" not in buttons(),
                   buttons())

        # ------------------------------------------------------------ 导出
        download_dir = os.path.join(work_dir, "_downloads")
        shutil.rmtree(download_dir, ignore_errors=True)
        os.makedirs(download_dir)
        self.browser.call("Browser.setDownloadBehavior",
                          {"behavior": "allow", "downloadPath": download_dir})
        if not click("wsExport", "导出"):
            return
        landed_files = []
        for _ in range(50):
            landed_files = [n for n in os.listdir(download_dir)
                            if n.endswith(".json") and not n.endswith(".crdownload")]
            if landed_files:
                break
            time.sleep(0.2)
        self.check("#33 导出的工作表真的落在下载目录里", bool(landed_files), landed_files)
        if landed_files:
            path = os.path.join(download_dir, landed_files[0])
            with open(path, encoding="utf-8") as handle:
                exported = json.load(handle)
            self.check("#33 导出的那份就是文件里那一份（名字与组件都在）",
                       exported.get("name") == "codex-renamed" and exported.get("components"),
                       json.dumps(exported, ensure_ascii=False)[:120])
            with open(path, "rb") as handle:
                blob = base64.b64encode(handle.read()).decode("ascii")
        else:
            blob = ""

        # ------------------------------------------------------------ 导入
        if blob:
            self.js(
                "(function(){var b=atob('%s'),a=new Uint8Array(b.length);"
                "for(var i=0;i<b.length;i++)a[i]=b.charCodeAt(i);"
                "var f=new File([a],'codex-renamed.json',{type:'application/json'});"
                "var d=new DataTransfer();d.items.add(f);"
                "var el=document.getElementById('wsFile');el.files=d.files;"
                "el.dispatchEvent(new Event('change'));return true;})()" % blob)
            imported = False
            for _ in range(100):
                imported = any(n.startswith("codex-renamed-") for n in files())
                if imported:
                    break
                time.sleep(0.2)
            self.check("#33 导入队友给的那份之后多出一套（同名加后缀）", imported, files())
            self.check("#33 导入的那套立刻上了按钮",
                       any(b.startswith("codex-renamed") for b in buttons()), buttons())

        # ------------------------------------------------------------ 删除
        current = self.js("i3pro.state.preset")
        if not click("wsDelete", "删除"):
            return
        self.check("#33 第一次点「删除」只是变成「确定删除？」（不会一下就没了）",
                   self.js("document.getElementById('wsDelete').textContent") == "确定删除？",
                   self.js("document.getElementById('wsDelete').textContent"))
        click("wsDelete", "确定删除？")
        self.check("#33 第二次点才真的删掉，而且说清切回哪一套",
                   "已删除" in self.toast()
                   and self.js("i3pro.state.preset") != current,
                   "%s / preset=%s" % (self.toast(), self.js("i3pro.state.preset")))
        self.check("#33 删掉之后按钮上也没有它了",
                   current not in buttons(), buttons())
        self.browser.shot(os.path.join(ROOT, "out", "shots", "verify-worksheets.png"),
                          self.session)

        # 这一趟跑完，车队仓库里的工作表文件必须一个字节都没动
        self.check("#33 整条流程没有碰仓库里的 worksheets/", _tree(repo_dir) == repo_before,
                   sorted(set(_tree(repo_dir)) ^ set(repo_before)))
        # 收尾：切回第一套、把主图滚回视口里。__center 会 scrollIntoView，而后面
        # 按坐标点画布的用例（注释 / GPS）用的是 getBoundingClientRect——页面停在
        # 半中间的话它们会点到别处（第一次跑就是这么红的）。所以这里不光恢复，
        # 还要**验一下**主图真的又能点：那条红过的用例靠这条才有意义。
        self.js("(function(){i3pro.state.cursor=null;i3pro.state.view=null;"
                "i3pro.applyPreset(i3pro.worksheetCatalogue().sheets[0].name);"
                "document.querySelector('#worksheet .comp')"
                ".scrollIntoView({block:'start'});return true;})()")
        time.sleep(1.2)
        x = self.js("__px((lane()[0]+lane()[1])/2)")
        y = self.js("__py(60)")
        self.browser.click(x, y, self.session)
        self.check("#33 收尾之后主图还行点得到（后面的用例按坐标点画布）",
                   self.js("i3pro.cursorTime()") is not None, "x=%s y=%s" % (x, y))
        self.js("(function(){i3pro.state.cursor=null;i3pro.renderAll();return true;})()")

    def missing_channels(self, work_dir):
        """#34：本场次没有的通道要**看得见**，而且不准被偷偷剔掉。

        这一关只有真浏览器算数：灰不灰是**算出来的样式**（`getComputedStyle` 的
        opacity / 划线），假 DOM 里既没有样式也没有布局；"这一行在屏幕上"更是
        `getBoundingClientRect` 说了算。

        造法走的是真路：把一套"引用了一条本场次没有的通道"的工作表从**文件选择器
        导进去**（和队友把表发给我、我导入它是同一件事），切到那一套，再看那张
        灰名单。最后真跑一次导出，把那几条写进元数据的 `excluded_missing` 也要
        在**真文件**里读到——断言到"接口收到了参数"不够。
        """
        bogus = "本场没有的通道（验收样例）"
        sheet = {
            "schema": 1, "name": "缺通道样例", "order": 99, "hints": [],
            "components": [
                {"type": "graph", "x": 0, "y": 0, "w": 12, "h": 10,
                 "config": {"mode": "tiled", "channels": ["Vx KF", bogus]}},
            ],
        }
        blob = base64.b64encode(
            json.dumps(sheet, ensure_ascii=False).encode("utf-8")).decode("ascii")
        self.js(
            "(function(){var b=atob('%s'),a=new Uint8Array(b.length);"
            "for(var i=0;i<b.length;i++)a[i]=b.charCodeAt(i);"
            "var f=new File([a],'codex-missing.json',{type:'application/json'});"
            "var d=new DataTransfer();d.items.add(f);"
            "var el=document.getElementById('wsFile');el.files=d.files;"
            "el.dispatchEvent(new Event('change'));return true;})()" % blob)
        appeared = self.browser.wait_for(
            "(function(){var bs=document.querySelectorAll('#presetRow button');"
            "for(var i=0;i<bs.length;i++){if(bs[i].textContent==='缺通道样例')return true;}"
            "return false;})()",
            self.session, timeout=20,
        )
        self.check("#34 导入那一套引用了本场没有通道的工作表之后，按钮上有了它",
                   appeared,
                   self.js("i3pro.state.preset"))
        if not appeared:
            return

        # 真的点那个按钮切过去（不是 applyPreset 直接调函数）。
        raw = self.js(
            "(function(){var bs=document.querySelectorAll('#presetRow button');"
            "for(var i=0;i<bs.length;i++){if(bs[i].textContent==='缺通道样例')"
            "return JSON.stringify(__center(bs[i]));}return null;})()")
        point = json.loads(raw) if raw and raw != "null" else None
        self.check("#34 那一套在按钮栏里真的点得到", bool(point) and point.get("w", 0) > 0,
                   point)
        if not point:
            return
        self.browser.click(point["x"], point["y"], self.session)
        time.sleep(1.0)
        self.check("#34 切过去之后，本场没有的那条被判成 missing",
                   self.js("i3pro.channelState(%r)" % bogus) == "missing",
                   self.js("i3pro.channelState(%r)" % bogus))
        self.check("#34 本场有的那条还是 present",
                   self.js("i3pro.channelState('Vx KF')") == "present")
        self.check("#34 缺的那条还在组件配置里（没有被自动剔除）",
                   self.js("i3pro.selectedChannels().indexOf(%r) >= 0" % bogus),
                   self.js("JSON.stringify(i3pro.selectedChannels())")[:200])
        self.check("#34 组件标题写着缺了几条",
                   "缺 1 条" in str(self.js(
                       "(function(){var c=i3pro.state.components[0];"
                       "return i3pro.componentTitle(c);})()")),
                   self.js("i3pro.componentTitle(i3pro.state.components[0])"))

        # 通道列表里那一行：真的在屏幕上、真的灰着、真的写着原因。
        row = self.js(
            "(function(){var rs=document.querySelectorAll('#channelList .ch.missing');"
            "for(var i=0;i<rs.length;i++){if(rs[i].textContent.indexOf(%r)>=0){"
            "var r=rs[i].getBoundingClientRect();var s=getComputedStyle(rs[i]);"
            "return JSON.stringify({w:r.width,h:r.height,top:r.top,"
            "opacity:parseFloat(s.opacity),text:rs[i].textContent,"
            "box:rs[i].querySelector('input')?rs[i].querySelector('input').checked:null});}}"
            "return null;})()" % bogus)
        got = json.loads(row) if row and row != "null" else None
        self.check("#34 通道列表里真有那条灰掉的缺通道", bool(got), got)
        if got:
            self.check("#34 它真的灰着（算出来的 opacity < 1）", got["opacity"] < 1, got)
            self.check("#34 它真的占着屏幕上的位置（不是没渲染）",
                       got["w"] > 0 and got["h"] > 0, got)
            self.check("#34 它写明了「本场次没有」", "本场次没有" in got["text"], got["text"])
            self.check("#34 它的勾还在（换回原场次时勾选不会丢）", got["box"] is True, got)

        # 图上那一条"本场次没有（不画）"。
        head = self.js(
            "(function(){var e=document.querySelector('.lrow.lmissing');"
            "if(!e)return null;var r=e.getBoundingClientRect();"
            "return JSON.stringify({w:r.width,h:r.height,text:e.textContent});})()")
        head = json.loads(head) if head and head != "null" else None
        self.check("#34 图上写着「本场次没有（不画）」那一行", bool(head), head)
        if head:
            self.check("#34 那一行真的画在图上（有宽高）", head["w"] > 0 and head["h"] > 0, head)
            self.check("#34 那一行点名了缺哪条", bogus in head["text"], head["text"])

        self.check("#34 页面抬头也写着缺了几条",
                   bogus in str(self.js(
                       "(document.getElementById('missingNotice')||{}).textContent")),
                   self.js("(document.getElementById('missingNotice')||{}).textContent"))
        # 再把通道列表那一行滚到屏幕中间留一张图：验收说明里那句"灰着且带文案"
        # 只有截图能一眼复核（数字是给回归用的）。
        self.js(
            "(function(){var rs=document.querySelectorAll('#channelList .ch.missing');"
            "for(var i=0;i<rs.length;i++){if(rs[i].textContent.indexOf(%r)>=0){"
            "rs[i].scrollIntoView({block:'center'});return true;}}return false;})()" % bogus)
        time.sleep(0.6)
        self.browser.shot(
            os.path.join(ROOT, "out", "shots", "verify-missing-channel.png"), self.session)

        # 导出：缺的那条**跳过并写进元数据**，真文件里读得到。
        download_dir = os.path.join(work_dir, "_downloads")
        shutil.rmtree(download_dir, ignore_errors=True)
        os.makedirs(download_dir)
        self.browser.call("Browser.setDownloadBehavior",
                          {"behavior": "allow", "downloadPath": download_dir})
        self.js(
            "i3pro.applyExportConfig({range:'time',from:'10',to:'12',channels:'selected',"
            "maths:true,rate:'10',custom:'',resample:'linear',meta:true,axis:'time',"
            "format:'csv',layout:'wide'}); i3pro.refreshExportPlan(); true")
        time.sleep(0.6)
        self.check("#34 导出面板把要跳过的那几条列了出来",
                   bogus in str(self.js("document.getElementById('exportMissing').textContent")),
                   self.js("document.getElementById('exportMissing').textContent"))
        self.check("#34 面板上那一行真的显示出来了",
                   not self.js("document.getElementById('exportMissingRow').hidden"))
        url = str(self.js(
            "(function(){var u=i3pro.exportURL(i3pro.exportConfig());"
            "return u.href||u.error||'';})()"))
        self.check("#34 导出请求带着 skip_missing=1", "skip_missing=1" in url, url)
        self.js("i3pro.runExport()")
        landed = []
        for _ in range(60):
            landed = [n for n in os.listdir(download_dir)
                      if n.endswith(".zip") and not n.endswith(".crdownload")]
            if landed:
                break
            time.sleep(0.2)
        self.check("#34 缺着一条也真的导出来了（不是整单打回）", bool(landed), landed)
        if landed:
            with zipfile.ZipFile(os.path.join(download_dir, landed[0])) as zf:
                names = zf.namelist()
                meta = json.loads(zf.read("metadata.json").decode("utf-8")) \
                    if "metadata.json" in names else {}
            self.check("#34 元数据里写着 excluded_missing（真文件里读出来的）",
                       meta.get("excluded_missing") == [bogus],
                       "names=%s meta=%s" % (names, meta.get("excluded_missing")))
            self.check("#34 缺的那条没有变成一列数据",
                       bogus not in str(meta.get("channels")))

        # 收尾：切回第一套，把主图滚回视口（后面几个用例按坐标点画布）。
        self.js("(function(){i3pro.state.cursor=null;i3pro.state.view=null;"
                "i3pro.applyPreset(i3pro.worksheetCatalogue().sheets[0].name);"
                "document.querySelector('#worksheet .comp')"
                ".scrollIntoView({block:'start'});return true;})()")
        time.sleep(1.0)

    def channel_processing(self, work_dir, session_name):
        """A75：通道处理（i2 Pro 的 Filter / Scale-Offset）——真点 ⚙、真敲参数、真落盘。

        为什么非要在真浏览器里跑：这一票的入口是通道列表里那个 40 px 不到的小齿轮，
        "点击能派发到那个函数"和"手指点得到那个像素"是两件事；判据又要一路落到
        **磁盘上的侧车**上（同名定义、四栏参数、归零那个常数），假 DOM 里既没有
        文件系统也没有布局。
        """
        sidecar = os.path.join(work_dir, session_name + ".maths.json")
        if os.path.exists(sidecar):
            os.remove(sidecar)
        preset_before = self.js("i3pro.state.preset")
        name = self.js(
            "(function(){var cs=(i3pro.state.components.filter(function(c){"
            "return c.type==='graph';})[0]||{config:{}}).config.channels||[];"
            "for(var i=0;i<cs.length;i++){if(i3pro.channelState(cs[i])==='present'"
            "&&cs[i]!==(i3pro.data.meta||{}).speed_channel)return cs[i];}"
            "return null;})()")
        self.check("#75 找到一条能处理的通道（避开在算距离轴的那条速度）", bool(name), name)
        if not name:
            return

        def read_sidecar():
            if not os.path.exists(sidecar):
                return {}
            with open(sidecar, encoding="utf-8") as handle:
                return json.load(handle)

        def definitions():
            return {d.get("name"): d for d in (read_sidecar().get("definitions") or [])}

        def rect_of(element_id):
            raw = self.js("(function(){var el=document.getElementById(%r);"
                          "return el?JSON.stringify(__center(el)):null;})()" % element_id)
            return json.loads(raw) if raw and raw != "null" else None

        # ---------------------------------------------------- 真点 ⚙
        raw = self.js(
            "(function(){var rows=document.querySelectorAll('#channelList label.ch');"
            "for(var i=0;i<rows.length;i++){var nm=rows[i].querySelector('.nm');"
            "if(nm&&nm.textContent===%s){var b=rows[i].querySelector('.cog');"
            "return b?JSON.stringify(__center(b)):null;}}return null;})()" % json.dumps(name))
        cog = json.loads(raw) if raw and raw != "null" else None
        self.check("#75 这一行右边的 ⚙ 在屏幕上真的点得到", bool(cog) and cog.get("w", 0) > 0, cog)
        if not cog:
            return
        self.browser.click(cog["x"], cog["y"], self.session)
        time.sleep(0.6)
        self.check("#75 点 ⚙ 之后面板落在这一条通道上",
                   self.js("i3pro.state.setupChannel") == name,
                   self.js("i3pro.state.setupChannel"))
        self.check("#75 面板真的显示出来了",
                   self.js("!document.getElementById('setupForm').hidden"))

        # -------------------------------- 选低通（零相位）+ 真键盘敲截止频率
        self.js("(function(){var s=document.getElementById('setupFilterKind');"
                "s.value='lp0';s.dispatchEvent(new Event('change'));return true;})()")
        self.js("(function(){var i=document.getElementById('setupFilterValue');"
                "i.focus();i.select();return true;})()")
        self.browser.key("20", self.session)
        apply_box = rect_of("setupApply")
        self.check("#75 「应用」在屏幕上点得到", bool(apply_box) and apply_box.get("w", 0) > 0,
                   apply_box)
        if not apply_box:
            return
        self.browser.click(apply_box["x"], apply_box["y"], self.session)
        time.sleep(2.5)                       # 服务端要把这一场重算一遍

        # ------------------------------------------------- 磁盘上的证据
        written = definitions().get(name) or {}
        self.check("#75 处理真的落进了 <场次>.maths.json", bool(written), sorted(definitions()))
        self.check("#75 写的是**同名**定义（i2 Pro 的覆盖语义）",
                   str(written.get("expr", "")).startswith("filter_lp0("), written.get("expr"))
        self.check("#75 定义里带着四栏参数，下次打开能原样读回",
                   bool((written.get("setup") or {}).get("filter")), written.get("setup"))

        # --------------------------------------------- 屏幕上的角标与数据
        self.check("#75 界面认出来这条已经处理过", self.js("i3pro.isProcessed(%s)" % json.dumps(name)))
        self.check("#75 通道列表那一行挂了 ƒ 角标",
                   self.js("document.querySelectorAll('#channelList .proc').length") >= 1,
                   self.js("document.querySelectorAll('#channelList .proc').length"))
        self.check("#75 这条通道现在按数学通道算（主时间基）",
                   self.js("(i3pro.channels.get(%s)||{}).derived === true" % json.dumps(name)))

        # ------------------------------- 光标处归零：真键盘 Ctrl+Shift+Z
        self.js("(function(){var l=(i3pro.data.laps||[]).filter(function(x){"
                "return x.complete;});var t=l.length?(l[0].start_time+5):10;"
                "i3pro.state.cursor=t;i3pro.renderAll();return true;})()")
        time.sleep(1.2)
        before = self.js("String(i3pro.cursorValueFor(%s))" % json.dumps(name))
        self.browser.key_named("Z", "KeyZ", 90, self.session, modifiers=10)   # 2|8 = Ctrl+Shift
        time.sleep(0.8)
        self.check("#75 Ctrl+Shift+Z 打开的还是这条通道的面板",
                   self.js("i3pro.state.setupChannel") == name,
                   self.js("i3pro.state.setupChannel"))
        self.js("(function(){var c=document.getElementById('setupZero');"
                "c.checked=true;c.dispatchEvent(new Event('change'));return true;})()")
        # 面板长高了一行（"正在处理"那行字），**按钮位置得重新量**：拿第一次坐标
        # 去点会落到别的地方，而那样"没写进 zero_at"会被当成功能坏了。
        apply_box = rect_of("setupApply")
        self.check("#75 勾上归零之后，「应用」还在屏幕上点得到",
                   bool(apply_box) and apply_box.get("w", 0) > 0, apply_box)
        if not apply_box:
            return
        self.browser.click(apply_box["x"], apply_box["y"], self.session)
        time.sleep(2.5)
        zero = (definitions().get(name) or {}).get("setup", {}).get("zero_at")
        self.check("#75 归零用的是光标处的值，而且把这个常数写进了定义",
                   zero is not None and abs(float(zero) - float(before))
                   <= abs(float(before)) * 1e-4 + 1e-6, "%s vs %s" % (before, zero))
        after = float(self.js("String(i3pro.cursorValueFor(%s))" % json.dumps(name)))
        self.check("#75 归零之后光标处的读数接近 0",
                   abs(after) < max(0.02, abs(float(before)) * 0.05),
                   "%s -> %s" % (before, after))

        # ---------------------------------------- 切走再切回：从侧车读回来
        def click_sheet(label):
            raw_box = self.js(
                "(function(){var n=%s;var bs=document.querySelectorAll('#presetRow button');"
                "for(var i=0;i<bs.length;i++){if(bs[i].textContent===n)"
                "return JSON.stringify(__center(bs[i]));}return null;})()" % json.dumps(label))
            if not raw_box or raw_box == "null":
                return False
            point = json.loads(raw_box)
            self.browser.click(point["x"], point["y"], self.session)
            time.sleep(1.2)
            return True

        others = [label for label in ("分析", "对比", "动力")
                  if label != preset_before and click_sheet(label)]
        if others and click_sheet(preset_before):
            self.check("#75 切走再切回，这条通道还是处理过的（从侧车读回来）",
                       self.js("i3pro.isProcessed(%s)" % json.dumps(name)),
                       self.js("i3pro.state.preset"))

        # --------------------------------------------------- 删除处理
        # 切回来之后那一行的位置变了，重新量一次，仍然用真鼠标点。
        raw = self.js(
            "(function(){var rows=document.querySelectorAll('#channelList label.ch');"
            "for(var i=0;i<rows.length;i++){var nm=rows[i].querySelector('.nm');"
            "if(nm&&nm.textContent===%s){var b=rows[i].querySelector('.cog');"
            "return b?JSON.stringify(__center(b)):null;}}return null;})()" % json.dumps(name))
        cog = json.loads(raw) if raw and raw != "null" else None
        if cog and cog.get("w", 0) > 0:
            self.browser.click(cog["x"], cog["y"], self.session)
            time.sleep(0.6)
        remove_box = rect_of("setupRemove")
        self.check("#75 「删除处理」在屏幕上点得到",
                   bool(remove_box) and remove_box.get("w", 0) > 0, remove_box)
        if remove_box:
            self.browser.click(remove_box["x"], remove_box["y"], self.session)
            time.sleep(2.5)
            self.check("#75 删掉处理之后侧车里那条定义没了",
                       name not in definitions(), sorted(definitions()))
            self.check("#75 删掉处理之后角标也没了",
                       self.js("i3pro.isProcessed(%s)" % json.dumps(name)) is False)

    def display_settings(self, work_dir):
        """#35：真点开颜色菜单选色、真切换调色板、真开关抬头字段——而且**真的存住**。

        配色与抬头都写进工作表，所以这一关的判据不是"点了有反应"，而是
        **保存 → 切走 → 切回之后还在**：切回来那一步读的是磁盘上的文件，
        内存里那份早被换掉了。假 DOM 里既没有文件也没有真正命中测试，只能在这里验。
        """
        def center_of(selector):
            raw = self.js(
                "(function(){var el=document.querySelector(%r);"
                "return el?JSON.stringify(__center(el)):null;})()" % selector)
            return json.loads(raw) if raw and raw != "null" else None

        def click_at(selector, label):
            box = center_of(selector)
            self.check("#35 「%s」点得到" % label,
                       bool(box) and box.get("w", 0) > 0, box)
            if not box:
                return False
            self.browser.click(box["x"], box["y"], self.session)
            time.sleep(0.5)
            return True

        def open_popmenu(selector, label):
            if not click_at(selector, label):
                return False
            shown = self.js(
                "!!document.querySelector('.popmenu')")
            self.check("#35 点「%s」弹出小菜单" % label, shown,
                       self.js("document.querySelectorAll('.popmenu').length"))
            return shown

        # 先确认停在一套有图的工作表上（前两关会把页面切来切去）。
        self.js("i3pro.applyPreset(i3pro.worksheetCatalogue().sheets[0].name); true")
        time.sleep(1.0)
        first = self.js("i3pro.state.components[0].config.channels[0]")
        self.check("#35 第一张图有通道（不然配色没得验）", bool(first), first)
        if not first:
            return

        # ---------------------------------------------------------- 手选色
        if not open_popmenu("#worksheet .comp .graphhead .swatch", "图例上的色块"):
            return
        seats = self.js(
            "(function(){var s=document.querySelectorAll('.popmenu .popswatch');"
            "if(s.length<8)return -1;var last=s[s.length-1];"
            "var r=last.getBoundingClientRect();"
            "return JSON.stringify({n:s.length,x:r.left+r.width/2,y:r.top+r.height/2});})()")
        seat = json.loads(seats) if seats and seats not in ("-1", "null") else None
        self.check("#35 颜色菜单里有一整排可选颜色", bool(seat) and seat.get("n", 0) >= 8, seat)
        if not seat:
            return
        self.browser.shot(
            os.path.join(ROOT, "out", "shots", "verify-color-menu.png"), self.session)
        wanted = self.js(
            "(function(){var s=document.querySelectorAll('.popmenu .popswatch');"
            "return s[s.length-1].style.background;})()")
        # 浏览器把颜色统一成 rgb(...)：要拿同一个尺子量，不然 "#b6ff00" 和
        # "rgb(182, 255, 0)" 会被判成"没换色"（这条第一次跑就是这么红的）。
        wanted = self.js(
            "(function(){var p=document.createElement('i');"
            "p.style.background=%r;return p.style.background;})()" % wanted)
        self.browser.click(seat["x"], seat["y"], self.session)
        time.sleep(0.6)
        now = self.js(
            "(function(){var p=document.createElement('i');"
            "p.style.background=i3pro.channelColor(%r, i3pro.state.components[0]);"
            "return p.style.background;})()" % first)
        self.check("#35 真点一个颜色之后这条通道真的换了色",
                   str(now).lower() == str(wanted).lower(),
                   "%s -> %s" % (wanted, now))
        self.check("#35 手选色记在组件配置里（不是只改了一次画布）",
                   self.js("!!(i3pro.state.components[0].config.colors || {})[%r]" % first),
                   self.js("JSON.stringify(i3pro.state.components[0].config.colors||{})"))
        swatch_bg = self.js(
            "getComputedStyle(document.querySelector('#worksheet .comp .graphhead .swatch'))"
            ".backgroundColor")

        # ------------------------------------------------- 抬头字段 / 调色板
        if not open_popmenu("#worksheet .comp .compbar button[title*='配色与抬头']",
                            "组件栏上的「显示」"):
            return
        fields = self.js(
            "(function(){var ls=document.querySelectorAll('.popmenu .popcheck input');"
            "var out={n:ls.length,boxes:[]};for(var i=0;i<ls.length;i++){"
            "var r=ls[i].getBoundingClientRect();"
            "out.boxes.push({x:r.left+r.width/2,y:r.top+r.height/2,on:ls[i].checked});}"
            "return JSON.stringify(out);})()")
        fields = json.loads(fields) if fields else None
        self.check("#35 「显示」菜单里有抬头字段的开关", bool(fields) and fields.get("n", 0) >= 4,
                   fields)
        if not fields:
            return
        self.browser.shot(
            os.path.join(ROOT, "out", "shots", "verify-display-menu.png"), self.session)
        average = fields["boxes"][4]                       # 光标值 / Δ / Min / Max / Avg / 单位
        self.browser.click(average["x"], average["y"], self.session)
        time.sleep(0.5)
        self.check("#35 真点一下之后只关掉了 Avg（Min/Max 还在）",
                   self.js("i3pro.headFields(i3pro.state.components[0]).avg") is False
                   and self.js("i3pro.headFields(i3pro.state.components[0]).min") is True
                   and self.js("i3pro.headFields(i3pro.state.components[0]).max") is True,
                   self.js("JSON.stringify(i3pro.headFields(i3pro.state.components[0]))"))

        chips = self.js(
            "(function(){var cs=document.querySelectorAll('.popmenu .popchip');"
            "for(var i=0;i<cs.length;i++){if(cs[i].textContent==='色盲友好'){"
            "var r=cs[i].getBoundingClientRect();"
            "return JSON.stringify({x:r.left+r.width/2,y:r.top+r.height/2});}}return null;})()")
        chip = json.loads(chips) if chips and chips != "null" else None
        self.check("#35 「显示」菜单里能真点到「色盲友好」", bool(chip), chip)
        if chip:
            self.browser.click(chip["x"], chip["y"], self.session)
            time.sleep(0.6)
            self.check("#35 真点一下换成了色盲友好那一套",
                       self.js("i3pro.state.components[0].config.palette") == "colorblind",
                       self.js("i3pro.state.components[0].config.palette"))
        self.js("i3pro.closePopMenu(); true")

        # ---------------------------------------------------- 保存 → 切走 → 切回
        sheet_id = self.js("(i3pro.currentSheet()||{}).id")
        if not click_at("#wsSave", "保存"):
            return
        time.sleep(0.8)
        on_disk = {}
        path = os.path.join(work_dir, "_worksheets_root", "worksheets",
                            str(sheet_id) + ".json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                body = json.load(handle)
            first_comp = (body.get("components") or [{}])[0]
            on_disk = (first_comp.get("config") or {})
        self.check("#35 保存把配色真的写进了 worksheets/%s.json" % sheet_id,
                   (on_disk.get("colors") or {}).get(first) and
                   on_disk.get("palette") == "colorblind",
                   json.dumps(on_disk, ensure_ascii=False)[:200])
        self.check("#35 抬头开关也写进了文件",
                   (on_disk.get("show") or {}).get("avg") is False,
                   json.dumps(on_disk.get("show"), ensure_ascii=False))
        sheets = self.js(
            "JSON.stringify(i3pro.worksheetCatalogue().sheets.map(function(s){return s.name;}))")
        sheets = json.loads(sheets) if sheets else []
        if len(sheets) < 2:
            self.check("#35 至少要有两套工作表才能验「切走再切回」", False, sheets)
            return
        other = sheets[1]
        if not click_at("#presetRow button:nth-child(2)", "切到另一套工作表"):
            return
        self.check("#35 真的切到了另一套",
                   self.js("i3pro.state.preset") == other,
                   "%s / 想要 %s" % (self.js("i3pro.state.preset"), other))
        click_at("#presetRow button:nth-child(1)", "切回原来那一套")
        time.sleep(1.0)
        self.check("#35 切回来还是原来那一套",
                   self.js("i3pro.state.preset") == sheets[0],
                   self.js("i3pro.state.preset"))
        self.check("#35 切走再切回之后，**手选色**还在（这一条读的是磁盘上的文件）",
                   self.js("!!(i3pro.state.components[0].config.colors || {})[%r]" % first),
                   self.js("JSON.stringify(i3pro.state.components[0].config.colors||{})"))
        self.check("#35 切回来之后调色板选择还在",
                   self.js("i3pro.state.components[0].config.palette") == "colorblind",
                   self.js("i3pro.state.components[0].config.palette"))
        self.check("#35 切回来之后抬头开关还在",
                   self.js("i3pro.headFields(i3pro.state.components[0]).avg") is False,
                   self.js("JSON.stringify(i3pro.headFields(i3pro.state.components[0]))"))
        if swatch_bg:
            self.check("#35 图例上的色块画出来也是手选色",
                       self.js("getComputedStyle(document.querySelector("
                               "'#worksheet .comp .graphhead .swatch')).backgroundColor")
                       == swatch_bg, swatch_bg)
        self.browser.shot(
            os.path.join(ROOT, "out", "shots", "verify-display-settings.png"), self.session)

        # 收尾：把这一套恢复成没动过的样子（不然后面几个用例看到的是色盲配色）。
        self.js("(function(){var c=i3pro.state.components[0];"
                "delete c.config.colors;delete c.config.palette;delete c.config.show;"
                "return true;})()")
        click_at("#wsSave", "保存（收尾还原）")
        time.sleep(0.6)

    def aliases(self, work_dir):
        """#36：同一套工作表换一份数据还能用——别名真落地、真保存、真读回来。

        造的是真实场景：**左后轮速在耐久那台车上叫 `SpeedRL`，高避那台没有这条
        通道**（只有 `Gear Speed1`）。所以这条用例导进来一套写 `@左后轮速` 的
        工作表，在这一场（高避）必须落到 `Gear Speed1`；然后真点着改候选、调顺序、
        点保存，再回磁盘把文件读出来核对。
        """
        bogus = "@左后轮速"
        sheet = {
            "schema": 1, "name": "别名样例", "order": 98, "hints": [],
            "aliases": [{"name": "左后轮速",
                         "candidates": ["SpeedRL", "Gear Speed1", "GPS Speed"]}],
            "components": [
                {"type": "graph", "x": 0, "y": 0, "w": 12, "h": 10,
                 "config": {"mode": "tiled", "channels": [bogus]}},
            ],
        }
        blob = base64.b64encode(
            json.dumps(sheet, ensure_ascii=False).encode("utf-8")).decode("ascii")
        self.js(
            "(function(){var b=atob('%s'),a=new Uint8Array(b.length);"
            "for(var i=0;i<b.length;i++)a[i]=b.charCodeAt(i);"
            "var f=new File([a],'codex-alias.json',{type:'application/json'});"
            "var d=new DataTransfer();d.items.add(f);"
            "var el=document.getElementById('wsFile');el.files=d.files;"
            "el.dispatchEvent(new Event('change'));return true;})()" % blob)
        appeared = self.browser.wait_for(
            "(function(){var bs=document.querySelectorAll('#presetRow button');"
            "for(var i=0;i<bs.length;i++){if(bs[i].textContent==='别名样例')return true;}"
            "return false;})()",
            self.session, timeout=20,
        )
        self.check("#36 导入一套写着 @左后轮速 的工作表之后，按钮上有了它",
                   appeared, self.js("i3pro.state.preset"))
        if not appeared:
            return
        raw = self.js(
            "(function(){var bs=document.querySelectorAll('#presetRow button');"
            "for(var i=0;i<bs.length;i++){if(bs[i].textContent==='别名样例')"
            "return JSON.stringify(__center(bs[i]));}return null;})()")
        point = json.loads(raw) if raw and raw != "null" else None
        if not point:
            self.check("#36 「别名样例」在按钮栏里真的点得到", False, raw)
            return
        self.browser.click(point["x"], point["y"], self.session)
        time.sleep(1.0)

        # 落点：高避这场没有 SpeedRL，所以必须落到 Gear Speed1。
        self.check("#36 引用被判成 present（落到了真通道）",
                   self.js("i3pro.channelState(%r)" % bogus) == "present",
                   self.js("i3pro.channelState(%r)" % bogus))
        self.check("#36 这一场落到的是 Gear Speed1（第一条存在的）",
                   self.js("i3pro.aliasLanding(%r)" % bogus) == "Gear Speed1",
                   self.js("i3pro.aliasLanding(%r)" % bogus))
        self.check("#36 图里画的就是那条落地的通道",
                   json.loads(self.js(
                       "JSON.stringify(i3pro.state.components[0].config.channels)"))
                   == ["Gear Speed1"],
                   self.js("JSON.stringify(i3pro.state.components[0].config.channels)"))

        # 状态视图（i2 Pro 的 Channel Status 等价物）：这一行写着落到了哪条。
        row = self.js(
            "(function(){var rs=document.querySelectorAll('#aliasList .alias');"
            "for(var i=0;i<rs.length;i++){if(rs[i].textContent.indexOf('左后轮速')>=0){"
            "var r=rs[i].getBoundingClientRect();"
            "return JSON.stringify({w:r.width,h:r.height,text:rs[i].textContent});}}"
            "return null;})()")
        row = json.loads(row) if row and row != "null" else None
        self.check("#36 状态视图里有这条别名", bool(row), row)
        if row:
            self.check("#36 它写明了落到哪条通道", "Gear Speed1" in row["text"], row["text"])
            self.check("#36 那一条真的占着屏幕上的位置", row["w"] > 0 and row["h"] > 0, row)
            self.check("#36 候选一条不少地摆出来了",
                       all(one in row["text"] for one in ("SpeedRL", "Gear Speed1", "GPS Speed")),
                       row["text"])
        # 截图时把那一栏滚进视口：验收说明里"状态视图写着落到哪条"要能一眼复核。
        self.js("document.getElementById('aliasGroup')"
                ".scrollIntoView({block:'center'}); true")
        time.sleep(0.5)
        self.browser.shot(os.path.join(ROOT, "out", "shots", "verify-alias-status.png"),
                          self.session)

        # 真点「＋ 别名」新建一条（真键盘打字 + 真点按钮）。
        self.js("(function(){var i=document.getElementById('aliasNewName');"
                "i.focus();i.select();return true;})()")
        self.browser.key("验证别名", self.session)
        box = json.loads(self.js(
            "(function(){var b=document.getElementById('aliasAddAlias');"
            "return JSON.stringify(__center(b));})()"))
        self.browser.click(box["x"], box["y"], self.session)
        time.sleep(0.5)
        self.check("#36 真点「＋ 别名」之后工作副本里多了一条",
                   self.js("i3pro.state.aliases.map(function(a){return a.name;})"
                           ".indexOf('验证别名')>=0"),
                   self.js("JSON.stringify(i3pro.currentSheetAliases())"))

        # 真点候选的 ↑：顺序就是优先级，挪上去就该换人。
        move = self.js(
            "(function(){var cs=document.querySelectorAll('#aliasList .cand');"
            "for(var i=0;i<cs.length;i++){var t=cs[i].textContent;"
            "if(t.indexOf('GPS Speed')>=0){var a=cs[i].querySelectorAll('a')[0];"
            "if(!a)return null;var r=a.getBoundingClientRect();"
            "return JSON.stringify({x:r.left+r.width/2,y:r.top+r.height/2});}}return null;})()")
        move = json.loads(move) if move and move != "null" else None
        self.check("#36 候选上的 ↑ 真的点得到", bool(move), move)
        if move:
            self.browser.click(move["x"], move["y"], self.session)
            time.sleep(0.5)
            order = json.loads(self.js(
                "JSON.stringify(i3pro.currentSheetAliases()[0].candidates)"))
            self.check("#36 真点一下之后候选顺序变了（往前挪了一位）",
                       order == ["SpeedRL", "GPS Speed", "Gear Speed1"], order)
            # 再点一次把它顶到第一位——顺序就是优先级，落点必须跟着换人。
            move = self.js(
                "(function(){var cs=document.querySelectorAll('#aliasList .cand');"
                "for(var i=0;i<cs.length;i++){var t=cs[i].textContent;"
                "if(t.indexOf('GPS Speed')>=0){var a=cs[i].querySelectorAll('a')[0];"
                "if(!a)return null;var r=a.getBoundingClientRect();"
                "return JSON.stringify({x:r.left+r.width/2,y:r.top+r.height/2});}}"
                "return null;})()")
            move = json.loads(move) if move and move != "null" else None
            if move:
                self.browser.click(move["x"], move["y"], self.session)
                time.sleep(0.5)
            order = json.loads(self.js(
                "JSON.stringify(i3pro.currentSheetAliases()[0].candidates)"))
            self.check("#36 连点两下之后它排到了第一位",
                       order == ["GPS Speed", "SpeedRL", "Gear Speed1"], order)

        # 保存 → 回磁盘核对：引用不进配置文件、别名进文件。
        box = json.loads(self.js(
            "(function(){return JSON.stringify(__center("
            "document.getElementById('wsSave')));})()"))
        self.browser.click(box["x"], box["y"], self.session)
        time.sleep(0.9)
        stem = self.js("(i3pro.currentSheet()||{}).id")
        path = os.path.join(work_dir, "_worksheets_root", "worksheets",
                            str(stem) + ".json")
        body = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                body = json.load(handle)
        self.check("#36 别名写进了 worksheets/%s.json" % stem,
                   (body.get("aliases") or [{}])[0].get("candidates") ==
                   ["GPS Speed", "SpeedRL", "Gear Speed1"],
                   json.dumps(body.get("aliases"), ensure_ascii=False))
        self.check("#36 组件里存的还是**引用**，不是真通道名（换场次才还能用）",
                   (body.get("components") or [{}])[0].get("config", {}).get("channels")
                   == [bogus],
                   json.dumps((body.get("components") or [{}])[0], ensure_ascii=False)[:200])
        self.check("#36 保存之后落点跟着新顺序走（GPS Speed 排到了第一位）",
                   self.js("i3pro.aliasLanding(%r)" % bogus) == "GPS Speed",
                   self.js("i3pro.aliasLanding(%r)" % bogus))
        self.check("#36 落点换了之后图里的通道也跟着换",
                   json.loads(self.js(
                       "JSON.stringify(i3pro.state.components[0].config.channels)"))
                   == ["GPS Speed"],
                   self.js("JSON.stringify(i3pro.state.components[0].config.channels)"))

        # 收尾：切回第一套、滚回视口（后面几个用例按坐标点画布）。
        self.js("(function(){i3pro.state.cursor=null;i3pro.state.view=null;"
                "i3pro.applyPreset(i3pro.worksheetCatalogue().sheets[0].name);"
                "document.querySelector('#worksheet .comp')"
                ".scrollIntoView({block:'start'});return true;})()")
        time.sleep(1.0)

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

    def imports(self, base_url, work_dir):
        """#31：Excel 导入这一关只在这里能验。

        `smoke_viewer.js` 跑在一份**快照 HTML** 上，够不到本地服务现生成的导入页；
        而"点文件选择器 → 上传 → 列表里多出一个场次"整条都是"点了才出现"的状态，
        正是这道回归存在的理由。递进去的是**真 File 对象**（走页面自己的 change 事件），
        不是直接改 input.value。
        """
        sys.path.insert(0, os.path.join(ROOT, "src"))
        from i3pro import xlsx as xlsxmod

        name = "验证用Excel"
        # 先写在场外：直接写进 work_dir 的话，打开导入页时它已经在列表里了，
        # "上传之后才出现"这条断言就成了空跑（第一次跑就是这么红的）。
        staging = tempfile.TemporaryDirectory(prefix="i3pro-verify-import-")
        sample = os.path.join(staging.name, name + ".xlsx")
        xlsxmod.write_workbook(sample, [{
            "name": "数据", "header": ["time_s", "Vx KF [km/h]", "TH"],
            "rows": [[i / 100.0, i * 0.5, i * 2.0] for i in range(200)],
            "split": False,
        }])
        with open(sample, "rb") as handle:
            blob = base64.b64encode(handle.read()).decode("ascii")
        staging.cleanup()

        page = self.browser.open(base_url, wait=0.8)
        accept = self.browser.js("(document.getElementById('files')||{}).accept||''", page)
        self.check("#31 导入页的文件选择器收得到 .xlsx", ".xlsx" in (accept or ""), accept)
        # 判据看的是**表格里的链接**，不是页面上有没有这几个字：上传进度那行也会写
        # 文件名，拿它当判据就等于没验（第一次跑就是这么假通过的）。
        listed = ("Array.prototype.some.call(document.querySelectorAll('a'),"
                  "function(a){return decodeURIComponent(a.getAttribute('href')||'')"
                  ".indexOf(%r)>=0;})" % name)
        before = self.browser.js(listed, page)
        self.check("#31 导入前列表里没有这份 Excel", not before)

        self.browser.js(
            "(function(){var b=atob('%s'),a=new Uint8Array(b.length);"
            "for(var i=0;i<b.length;i++)a[i]=b.charCodeAt(i);"
            "var f=new File([a],'%s.xlsx',{type:'application/vnd.openxmlformats-"
            "officedocument.spreadsheetml.sheet'});"
            "var d=new DataTransfer();d.items.add(f);"
            "var el=document.getElementById('files');el.files=d.files;"
            "el.dispatchEvent(new Event('change'));return true;})()" % (blob, name),
            page)
        appeared = self.browser.wait_for(listed, page, timeout=25)
        self.check("#31 用页面自己的文件选择器上传后，场次列表里出现了它", appeared)
        self.browser.shot(os.path.join(ROOT, "out", "shots", "verify-import.png"), page)

        view = self.browser.open(
            base_url + "session/" + urllib.parse.quote(name), wait=1.2)
        ready = self.browser.wait_for(
            "!!(window.i3pro && i3pro.data && i3pro.data.channels"
            " && i3pro.data.channels.length)", view)
        self.check("#31 这份 Excel 场次能当工作台打开", ready)
        if ready:
            names = json.loads(self.browser.js(
                "JSON.stringify((i3pro.data.channels||[]).map(function(c){return c.name;}))",
                view))
            self.check("#31 列名里的 [单位] 拆回成了通道名 + 单位",
                       "Vx KF" in names and "TH" in names, names)
            speed = json.loads(self.browser.js(
                "JSON.stringify(i3pro.data.channels.filter(function(c){"
                "return c.name==='Vx KF';})[0]||{})", view))
            self.check("#31 单位跟着列名一起读回来了", speed.get("unit") == "km/h", speed)
            self.browser.shot(os.path.join(ROOT, "out", "shots", "verify-import-view.png"),
                              view)

    def text_import(self, base_url, work_dir):
        """#32：分隔文本的导入预览——**真鼠标点、真键盘改**，然后看它是不是照改的读。

        `smoke_viewer.js` 跑在快照 HTML 上，够不到 serve 现生成的导入页；而这条
        流程整个都是"点了才出现"：卡片要不要弹、改完下拉框预览会不会变、按钮
        点不点得到、点完场次列表里有没有多一个。

        三件事只有真浏览器给得了答案：① 预览卡片在**猜错**时是否说清下一步
        （这份样例没有时间列）；② 改了采样率之后卡片是否跟着变绿；③ 点「导入」
        与「取消」是否真的走到底（取消要连暂存文件一起删掉）。
        """
        name = "验证用文本"
        doomed = "验证用文本取消"
        staging = tempfile.TemporaryDirectory(prefix="i3pro-verify-text-")

        def sample(where, text):
            path = os.path.join(where, text)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("Vx KF;TH\n")
                for i in range(200):
                    handle.write("%d;%d\n" % (i * 0.5, i * 2))
            return path

        # 没有时间列，而且分隔符是分号：正好是"预览里要改一下"的那种文件。
        blob = _blob(sample(staging.name, name + ".txt"))
        blob2 = _blob(sample(staging.name, doomed + ".txt"))
        staging.cleanup()

        def listed(needle):
            return ("Array.prototype.some.call(document.querySelectorAll('a'),"
                    "function(a){return decodeURIComponent(a.getAttribute('href')||'')"
                    ".indexOf(%r)>=0;})" % needle)

        def rect_of(selector):
            raw = self.browser.js(
                "(function(){var el=document.querySelector(%r);if(!el)return 'null';"
                "var r=el.getBoundingClientRect();return JSON.stringify({x:r.left+r.width/2,"
                "y:r.top+r.height/2,w:r.width,h:r.height,disabled:!!el.disabled});})()"
                % selector, page)
            return json.loads(raw) if raw and raw != "null" else None

        def text(selector):
            return self.browser.js(
                "((document.querySelector(%r)||{}).textContent||'')" % selector, page)

        def card_open():
            return self.browser.js(
                "(function(){var c=document.getElementById('importCard');"
                "return !!c && c.style.display !== 'none';})()", page)

        def hand_over(data, filename):
            self.browser.js(
                "(function(){var b=atob('%s'),a=new Uint8Array(b.length);"
                "for(var i=0;i<b.length;i++)a[i]=b.charCodeAt(i);"
                "var f=new File([a],'%s',{type:'text/plain'});"
                "var d=new DataTransfer();d.items.add(f);"
                "var el=document.getElementById('files');el.files=d.files;"
                "el.dispatchEvent(new Event('change'));return true;})()" % (data, filename),
                page)

        before_staging = set(glob.glob(os.path.join(tempfile.gettempdir(),
                                                    "i3pro-import-*")))
        page = self.browser.open(base_url, wait=0.8)
        accept = self.browser.js("(document.getElementById('files')||{}).accept||''", page)
        self.check("#32 导入页的文件选择器收得到 .txt", ".txt" in (accept or ""), accept)
        self.check("#32 导入前列表里没有这份文本", not self.browser.js(listed(name), page))

        hand_over(blob, name + ".txt")
        opened = self.browser.wait_for(
            "(function(){var c=document.getElementById('importCard');"
            "return !!c && c.style.display !== 'none';})()", page, timeout=20)
        self.check("#32 选一份 .txt 之后弹出预览卡片", opened)
        self.browser.shot(os.path.join(ROOT, "out", "shots", "verify-text-preview.png"), page)
        self.check("#32 预览里说清了这份表没有时间列（下一步是填采样率）",
                   "时间列" in text("#cardError") or "时间列" in text("#cardInfo"),
                   (text("#cardError") + text("#cardInfo"))[:120])
        # 预览表里的格子是**按认出来的分隔符切过**的：分号表在这里就该是两列，
        # 而不是一行 "Vx KF;TH" 的文字。
        table = text("#cardTable")
        self.check("#32 预览表按认出来的分隔符切成了多列",
                   "Vx KF" in table and "TH" in table, table[:80])

        # 真键盘填采样率：点进去、打 100、按 Tab（Tab 触发 change，预览随即重算）
        box = rect_of("#optRate")
        self.browser.click(box["x"], box["y"], page)
        self.browser.insert_text("100", page)
        self.browser.key_named("Tab", "Tab", 9, page)
        green = self.browser.wait_for(
            "(function(){var e=document.getElementById('cardError');"
            "return e.style.display === 'none' && /\\d+ Hz/.test("
            "document.getElementById('cardInfo').textContent);})()", page, timeout=15)
        self.check("#32 填上 100 Hz 之后预览变绿并报出通道数", green, text("#cardInfo")[:90])
        self.check("#32 绿了之后写明了读法（分号 · 生成的时间列）",
                   "分号分隔" in text("#cardInfo") and "生成" in text("#cardInfo"),
                   text("#cardInfo")[:120])

        ok = rect_of("#cardOk")
        self.browser.click(ok["x"], ok["y"], page)
        appeared = self.browser.wait_for(listed(name), page, timeout=25)
        self.check("#32 真点「导入」之后，场次列表里出现了它", appeared)
        self.check("#32 导入之后暂存目录被收掉",
                   not (set(glob.glob(os.path.join(tempfile.gettempdir(), "i3pro-import-*")))
                        - before_staging),
                   "剩下了 %s" % sorted(set(glob.glob(os.path.join(
                       tempfile.gettempdir(), "i3pro-import-*"))) - before_staging))

        # 打开它：抬头要写明这份表是怎么读出来的，采样率要真是 100
        view = self.browser.open(base_url + "session/" + urllib.parse.quote(name), wait=1.2)
        if self.browser.wait_for("!!(window.i3pro && i3pro.data && i3pro.data.meta)", view):
            note = self.browser.js("(i3pro.data.meta.parse_note||'')", view)
            rate = float(self.browser.js("i3pro.data.meta.sample_rate", view) or 0)
            self.check("#32 场次抬头写明了读法（分号 · 生成的时间列）",
                       "分号" in note and "100 Hz" in note, note)
            self.check("#32 生成的时间列就是 100 Hz", abs(rate - 100.0) < 1e-6, rate)
            header = self.browser.js("document.getElementById('fileInfo').innerHTML", view)
            self.check("#32 读法真的画在页头那一行上", "读法" in (header or ""),
                       (header or "")[:120])
            self.browser.shot(os.path.join(ROOT, "out", "shots", "verify-text-session.png"),
                              view)
        else:
            self.check("#32 这份文本场次能当工作台打开", False)

        # 取消那条路：暂存文件必须立刻删掉，场次列表里不许出现
        page = self.browser.open(base_url, wait=0.6)
        hand_over(blob2, doomed + ".txt")
        if self.browser.wait_for(
                "(function(){var c=document.getElementById('importCard');"
                "return !!c && c.style.display !== 'none';})()", page, timeout=20):
            cancel = rect_of("#cardCancel")
            self.browser.click(cancel["x"], cancel["y"], page)
            time.sleep(1.0)
            self.check("#32 点「取消」之后卡片关掉、场次列表里没有它",
                       not card_open() and not self.browser.js(listed(doomed), page))
            self.check("#32 取消也要把暂存文件删掉",
                       not (set(glob.glob(os.path.join(tempfile.gettempdir(),
                                                       "i3pro-import-*")))
                            - before_staging))
        else:
            self.check("#32 取消那条路能走到（第二份文件也弹了卡片）", False)

    # ------------------------------------------------ #77 CSV 场次上的通道处理
    def channel_processing_on_an_imported_session(self, base_url, work_dir):
        """A77：**导入进来的** CSV / 文本场次上，真点两次「应用」都不准报错。

        用户报障的原话是"设置完滤波参数以后，提示数学通道未找到 / 未知场次"。
        金标准那两份是 ``.ld``（派生列住在 ``LogFile.derived``），踩不到；CSV 场次
        的派生列以前和原生列同住一本字典：第一轮的覆盖把原生列顶掉，第二轮开头的
        ``detach`` 再把它 pop 掉，求值于是 ``KeyError``，被 API 层报成
        "未知场次: <通道名>"。所以这一条必须在 **CSV 场次**上、而且必须**点两次**。

        用的就是 ``text_import`` 刚导入的那一场（它就是一个 CSV 场次），走完全程：
        应用 -> 再应用 -> 删除处理，最后一步是"原生列还在不在"的判据。
        """
        session_name = "验证用文本"
        sidecar = os.path.join(work_dir, session_name + ".maths.json")
        if os.path.exists(sidecar):
            os.remove(sidecar)

        def read_sidecar():
            if not os.path.exists(sidecar):
                return {}
            with open(sidecar, encoding="utf-8") as handle:
                return json.load(handle)

        def definitions():
            return {d.get("name"): d for d in (read_sidecar().get("definitions") or [])}

        page = self.browser.open(
            base_url + "session/" + urllib.parse.quote(session_name), wait=1.0
        )
        if not self.browser.wait_for("!!(window.i3pro && i3pro.data && i3pro.data.channels)",
                                     page, timeout=60):
            self.check("#77 导入的场次能当工作台打开", False)
            return
        time.sleep(1.0)
        self.check("#77 导入的场次能当工作台打开", True)

        def js(expression):
            return self.browser.js(expression, page)

        # 这个标签页是**新开的**，主页面那份 HELPERS 不在里面：`__center` 得重新注入，
        # 否则量元素坐标那一步会在页面里抛 Uncaught（A33 那条注释里同样的坑）。
        js(HELPERS)

        def toast():
            return js("(document.getElementById('toast')||{}).textContent||''")

        def rect_of(selector):
            raw = js("(function(){var el=document.querySelector(%r);if(!el)return 'null';"
                     "var r=el.getBoundingClientRect();return JSON.stringify({x:r.left+r.width/2,"
                     "y:r.top+r.height/2,w:r.width,h:r.height,disabled:!!el.disabled});})()"
                     % selector)
            return json.loads(raw) if raw and raw != "null" else None

        def item_rect(expression, label):
            raw = js(expression)
            box = json.loads(raw) if raw and raw != "null" else None
            height = float(js("window.innerHeight"))
            self.check("#77 %s 在屏幕上真的点得到" % label,
                       bool(box) and box.get("w", 0) > 0
                       and 0 < box["y"] < height - 2,
                       "box=%s innerHeight=%s" % (box, height))
            return box

        def clickable(selector, label):
            """把元素滚进视野再量。**必须先滚**：应用一次之后左边栏会长出东西
            （数学通道列表多一条），「通道处理」那两枚按钮跟着往下走——实测在
            1000 px 高的窗口里它们会掉到 907 px 的视口外面，CDP 的鼠标事件落在
            视口外是**静默无效**的（不报错、什么也不发生）。所以这里量完还要断言
            "真的在窗口里"，否则点到空气上会被当成功能坏了。
            """
            js("(function(){var el=document.querySelector(%r);"
               "el.scrollIntoView({block:'end'});return true;})()" % selector)
            time.sleep(0.3)
            box = rect_of(selector)
            height = js("window.innerHeight")
            inside = bool(box) and box.get("w", 0) > 0 and box["y"] > 0 \
                and box["y"] < float(height) - 2
            self.check("#77 %s 滚进视野之后真的点得到" % label, inside,
                       "box=%s innerHeight=%s" % (box, height))
            return box if inside else None

        def apply_once():
            box = clickable("#setupApply", "「应用」")
            if not box:
                return False
            self.browser.click(box["x"], box["y"], page)
            time.sleep(2.0)
            return True

        # 挑一条本场次真有数据的通道（优先不是"在算距离轴"的那条速度）
        name = js(
            "(function(){var cs=(i3pro.state.components.filter(function(c){"
            "return c.type==='graph';})[0]||{config:{}}).config.channels||[];"
            "var spd=(i3pro.data.meta||{}).speed_channel;var first=null;"
            "for(var i=0;i<cs.length;i++){if(i3pro.channelState(cs[i])==='present'){"
            "if(cs[i]!==spd)return cs[i];if(first===null)first=cs[i];}}"
            "return first;})()")
        self.check("#77 导入的场次里有能处理的通道（避开在算距离轴的那条速度）",
                   bool(name), name)
        if not name:
            return

        # ---------------------------------------------------------- 真点 ⚙
        cog = item_rect(
            "(function(){var rows=document.querySelectorAll('#channelList label.ch');"
            "for(var i=0;i<rows.length;i++){var nm=rows[i].querySelector('.nm');"
            "if(nm&&nm.textContent===%s){var b=rows[i].querySelector('.cog');"
            "return b?JSON.stringify(__center(b)):null;}}return null;})()"
            % json.dumps(name), "这条通道的 ⚙")
        if not cog:
            return
        self.browser.click(cog["x"], cog["y"], page)
        time.sleep(0.6)
        self.check("#77 点 ⚙ 之后面板落在这一条通道上", js("i3pro.state.setupChannel") == name,
                   js("i3pro.state.setupChannel"))

        # 低通 20 Hz（用户说的"设置完滤波参数"），真键盘敲进去
        js("(function(){var s=document.getElementById('setupFilterKind');"
           "s.value='lp';s.dispatchEvent(new Event('change'));return true;})()")
        js("(function(){var i=document.getElementById('setupFilterValue');"
           "i.focus();i.select();return true;})()")
        self.browser.key("20", page)
        js("(function(){var l=(i3pro.data.laps||[]).filter(function(x){return x.complete;});"
           "var t=l.length?(l[0].start_time+5):10;i3pro.state.cursor=t;i3pro.renderAll();"
           "return true;})()")
        time.sleep(0.6)
        if not apply_once():
            return

        written = definitions().get(name) or {}
        self.check("#77 第一次「应用」落进了 <场次>.maths.json",
                   bool(written), sorted(definitions()))
        self.check("#77 写的是**同名**定义、而且是那条低通",
                   str(written.get("expr", "")).startswith("filter_lp(")
                   and written.get("name") == name, written.get("expr"))
        self.check("#77 第一次应用之后界面认出来这条被处理过",
                   js("i3pro.isProcessed(%s)" % json.dumps(name))
                   and js("(i3pro.channels.get(%s)||{}).derived===true" % json.dumps(name)))
        before = float(js("String(i3pro.cursorValueFor(%s))" % json.dumps(name)))
        self.check("#77 处理之后光标处的读数读得出来", math.isfinite(before), before)

        # -------------------------------------- 第二次「应用」：用户报的就是这一步
        if not apply_once():
            return
        said = toast()
        self.check("#77 第二次「应用」没有报错（原话是「数学通道保存失败：未知场次: …」）",
                   "保存失败" not in said and "未知场次" not in said
                   and "加上处理" in said, said)
        self.check("#77 第二次应用之后侧车里仍然只有一条定义",
                   sorted(definitions()) == [name], sorted(definitions()))
        after = js("String(i3pro.cursorValueFor(%s))" % json.dumps(name))
        self.check("#77 第二次应用之后这条通道还取得到值（原生列没被删掉）",
                   after not in ("NaN", "null", "undefined") and math.isfinite(float(after)),
                   after)
        self.check("#77 第二次应用之后这一列还是「算出来的」那一档",
                   js("(i3pro.channels.get(%s)||{}).derived===true" % json.dumps(name)))

        # ------------------------------------------------ 删除处理：回到原值
        remove = clickable("#setupRemove", "「删除处理」")
        if not remove:
            return
        self.browser.click(remove["x"], remove["y"], page)
        time.sleep(2.0)
        self.check("#77 「删除处理」之后侧车里没有它了", name not in definitions(),
                   "侧车=%s · toast=%r · 还标着处理过=%s"
                   % (sorted(definitions()), toast(),
                      js("i3pro.isProcessed(%s)" % json.dumps(name))))
        back = js("String(i3pro.cursorValueFor(%s))" % json.dumps(name))
        self.check("#77 删除之后这条通道回到文件里的原值、而且还在",
                   back not in ("NaN", "null", "undefined")
                   and js("i3pro.channelState(%s)==='present'" % json.dumps(name)),
                   "%s（原始列还在 = 覆盖没把文件里那列吃掉）" % back)
        self.browser.shot(os.path.join(ROOT, "out", "shots", "verify-csv-override.png"), page)

    # ------------------------------------------------ A78 侧边栏快速跳转
    def sidebar_jump(self):
        """A78：左边那一栏比屏幕长得多，"跳到哪一栏"要**真的滚过去**、光标放对地方。

        无头那一条（smoke_viewer）只能证明接线对得上：假 DOM 里每个元素的高都是
        同一个数，滚动量不出来。这条用真鼠标真键盘量三件事：

        ① 点了最靠下的那一栏，滚动容器（aside）的 ``scrollTop`` 真的变了；
        ② 那一栏的标题**正好贴在按钮排的下面**（既不是没滚到、也不是滚过头）；
        ③ 点「通道」光标落在搜索框里，真按 Ctrl+K 光标落到那一排按钮上。
        """
        def rect_of(selector):
            raw = self.js(
                "(function(){var el=document.querySelector(%r);if(!el)return 'null';"
                "var r=el.getBoundingClientRect();return JSON.stringify({x:r.left+r.width/2,"
                "y:r.top+r.height/2,w:r.width,h:r.height,top:r.top,bottom:r.bottom});})()"
                % selector)
            return json.loads(raw) if raw and raw != "null" else None

        def scroll_top():
            return float(self.js("document.getElementById('asideScroll').scrollTop") or 0)

        def click_chip(target):
            box = rect_of("#jumpRow button[data-jump='%s']" % target)
            if not box or box.get("w", 0) <= 0:
                return None
            self.browser.click(box["x"], box["y"], self.session)
            time.sleep(0.6)
            return box

        first = click_chip("sheetGroup")
        self.check("A78 侧边栏顶上有一排「跳到哪一栏」的按钮", bool(first), first)
        if not first:
            return
        self.browser.shot(os.path.join(ROOT, "out", "shots", "verify-jump-rail.png"),
                          self.session)
        top = scroll_top()
        self.check("A78 跳到最上面那一栏时滚动条在顶（不然下面那两步量不出东西）",
                   top <= 30, top)
        # 高亮那条 bug（2026-10-14 评审补的真浏览器版本）：滚动条在顶、而「光标值」
        # 还是收着的时候，"滚到哪儿了"不能算到它头上。收起来的元素 rect 全是 0，
        # 顶边永远"过了按钮排"——不跳过它，滚到哪儿都亮着它。
        collapsed = self.js(
            "document.getElementById('valuesGroup').getBoundingClientRect().height<=0")
        here = self.js("i3pro.syncJumpActive(); i3pro.jumpActive()")
        self.check("A78 收起来的模块不会把高亮拦在自己身上（滚动条在顶时）",
                   (not collapsed) or here != "valuesGroup",
                   "收着=%s · 高亮=%s" % (collapsed, here))

        # 靠前的某一栏：这一栏量得准"标题贴到按钮排下面"。**不能拿最靠下的那几栏
        # 量**：它们底下没有足够内容，滚到底就顶住了（那是正常的，不是 bug）。
        # 「光标值」还多一层：它默认是收起来的（按 V 才出来），跳过去要顺手打开它——
        # 隐藏元素的 rect 全是 0，跳了等于没跳（这一条就是真浏览器里量出来的）。
        hidden_before = self.js(
            "document.getElementById('valuesGroup').getBoundingClientRect().height<=0")
        click_chip("valuesGroup")
        moved = scroll_top()
        self.check("A78 真点中间那一栏「光标值」-> 侧边栏真的滚下去了",
                   moved > top + 40, "%s -> %s" % (top, moved))
        self.check("A78 跳到一个收起来的模块会顺手打开它（光标值默认收着）",
                   (not hidden_before) or self.js(
                       "document.getElementById('valuesGroup').getBoundingClientRect().height>0"),
                   "跳之前收着=%s" % hidden_before)
        group = rect_of("#valuesGroup > h2")
        rail = rect_of("#jumpRail")
        if group and rail:
            gap = group["top"] - rail["bottom"]
            self.check("A78 滚过去之后那一栏的标题就在按钮排下面（没被盖住、也没滚过头）",
                       -2 <= gap <= 60, "%.1f px" % gap)
        else:
            self.check("A78 能读到目标那一栏的位置", False, "读不到 rect")
        self.check("A78 滚过去的那一栏在按钮上也亮着",
                   self.js("i3pro.jumpActive()") == "valuesGroup",
                   self.js("i3pro.jumpActive()"))

        # 最靠下的那一栏：到底之后滚不到顶（正常的），但它必须进视野，
        # 而且按钮排上必须**有一栏亮着**（"我在哪"这件事不许因为滚动而全部灭掉）。
        click_chip("setupGroup")
        host = rect_of("#asideScroll")
        head = rect_of("#setupGroup > h2")
        if host and head and rail:
            inside = (head["top"] >= rail["bottom"] - 2) and (head["top"] < host["bottom"] - 20)
            self.check("A78 最下面那一栏（到底了、滚不到顶）也被带进了视野",
                       inside, "标题 top=%.1f · 按钮排 bottom=%.1f · 视野底=%.1f"
                       % (head["top"], rail["bottom"], host["bottom"]))
        else:
            self.check("A78 能读到最下面那一栏的位置", False, "读不到 rect")
        self.check("A78 滚到底之后按钮排上仍然亮着一栏（滚一下不会把高亮全抹掉）",
                   bool(self.js("i3pro.jumpActive()")), self.js("i3pro.jumpActive()"))
        self.browser.shot(os.path.join(ROOT, "out", "shots", "verify-jump-bottom.png"),
                          self.session)

        click_chip("channelGroup")
        self.check("A78 点「通道」之后光标落在搜索框里（下一步就是打字找通道）",
                   self.js("(document.activeElement||{}).id") == "chSearch",
                   self.js("(document.activeElement||{}).id"))

        # 真按 Ctrl+K（CDP 的 modifiers：2 = Ctrl），光标该落到那一排按钮上
        self.browser.key_named("k", "KeyK", 75, self.session, modifiers=2)
        time.sleep(0.4)
        def focused_chip():
            return self.js(
                "(document.activeElement&&document.activeElement.dataset||{}).jump || ''")

        here = focused_chip()
        self.check("A78 真按 Ctrl+K -> 光标落到那一排按钮上",
                   bool(here), here)
        # 这一排里的方向键换的是**选中项**；它不该冒到页面上那把全局快捷键去
        # （那一层 ArrowLeft/Right 是"挪测量光标"）。冒上去的后果是"换一栏"顺手
        # 把光标也挪了 0.01 s——评审时真浏览器量出来的第三条。
        cursor_before = self.js("String(i3pro.state.cursor)")
        self.browser.key_named("ArrowRight", "ArrowRight", 39, self.session)
        time.sleep(0.4)
        moved = focused_chip()
        self.check("A78 这一排里按右方向键换的是选中项（通道 -> 别名）",
                   moved == "aliasGroup" and moved != here, "%s -> %s" % (here, moved))
        self.check("A78 在这一排里按方向键不会顺手把测量光标挪走",
                   self.js("String(i3pro.state.cursor)") == cursor_before,
                   "%s -> %s" % (cursor_before, self.js("String(i3pro.state.cursor)")))
        # 真按回车：真的跳到**新选中**的那一栏。判据是"高亮从通道换到别名"——
        # 原先那条是在通道上点完再在通道上按回车，前后都是通道，什么都不证明。
        # （CDP 还得带 text，不然页面收不到按键默认动作，回车根本不会激活按钮。）
        landed = self.js("i3pro.jumpActive()")
        self.browser.key_named("Enter", "Enter", 13, self.session, text="\r")
        time.sleep(0.6)
        self.check("A78 键盘回车真的跳到选中那一栏（高亮从「通道」换到「别名」）",
                   self.js("i3pro.jumpActive()") == "aliasGroup",
                   "回车前亮着 %s，回车后亮着 %s" % (landed, self.js("i3pro.jumpActive()")))
        # 收尾：回到最上面那一栏，后面的验收从原来那一屏继续
        click_chip("sheetGroup")

    # ------------------------------------------- A83 写路径的唯一出口
    def attitude(self):
        """#43 姿态图：真鼠标拖拽转视角、真按 R 复位、真移动光标重画。

        假 DOM 证明不了"用户拖得动、点得到"——那里的元素没有面积、没有命中测试，
        disabled 的控件照样收得到 click。而这一票的验收里正好有三条是这一类：
        **拖拽后画布像素变化**、**复位回到初始视角**、**移动光标后画布再变**。
        （单位换算那一条在无头那边钉得更死：常量 10 + 精确读数，这里不重复。）
        """
        def rect_of(component_id):
            raw = self.js(
                "(function(){var c=document.querySelector('[data-id=\"" + component_id
                + "\"] canvas');return c?JSON.stringify(__center(c)):null;})()")
            return json.loads(raw) if raw and raw != "null" else None

        def attitude_of():
            raw = self.js(
                "JSON.stringify((function(){var c=i3pro.state.components.filter("
                "function(x){return x.type==='attitude';})[0];"
                "return c?{id:c.id,camera:c.config.camera}:null;})())")
            return json.loads(raw) if raw and raw != "null" else None

        def pixels(component_id):
            return self.js(
                "(function(){var c=document.querySelector('[data-id=\"" + component_id
                + "\"] canvas');if(!c)return '';var d=c.toDataURL();"
                "return d.length+':'+d.slice(-48);})()")

        def head_of(component_id):
            return self.js(
                "(function(){var el=document.querySelector('[data-id=\"" + component_id
                + "\"] .graphhead');return el?el.textContent:'';})()")

        self.js("i3pro.applyPreset(i3pro.worksheetCatalogue().sheets[0].name); true")
        time.sleep(0.8)
        before = self.js("i3pro.state.components.length")
        self.js("(function(){document.getElementById('addType').value='attitude';"
                "return true;})()")
        add = json.loads(self.js("JSON.stringify(__rectOf('addBtn'))"))
        self.browser.click(add["x"], add["y"], self.session)
        self.browser.wait_for("i3pro.state.components.length === %d" % (before + 1),
                              self.session, timeout=10)
        time.sleep(0.8)
        made = attitude_of()
        self.check("#43 真点「添加」就加出一个姿态组件", bool(made),
                   json.dumps(made, ensure_ascii=False))
        if not made:
            return
        component_id = made["id"]
        box = rect_of(component_id)
        self.check("#43 姿态画布真的占地方（点得到、拖得动）",
                   bool(box) and box.get("w", 0) > 0 and box.get("h", 0) > 0, box)
        if not box:
            return

        # 两台车的 IMU 不一样：按名字自动挑到的那三条**在某一台上是空模板通道**
        # （实测：高避的 `Aceinna Roll/Pitch` 全是 0，活的在 `Roll (2)`/`Pitch (2)`；
        # 耐久正好反过来）。这一组验的是"拖得动 / 跟光标"，所以换成两份金标准里
        # 都一定有数的三条；"按名字挑到哪三条"归无头那边（它拿后端算出来的名字对拍）。
        switched = self.js(
            "(function(){var el=document.querySelector('[data-id=\"" + component_id
            + "\"] .scattercfg');if(!el)return '';var s=el.querySelectorAll('select');"
            "if(s.length<5)return 'selects='+s.length;"
            "['Vx KF','G Force Lat','G Force Long'].forEach(function(name,i){"
            "s[2+i].value=name;s[2+i].dispatchEvent(new Event('change'));});"
            "var c=i3pro.state.components.filter(function(x){return x.type==='attitude';})[0];"
            "return [c.config.roll,c.config.pitch,c.config.yaw].join('|');})()")
        time.sleep(0.6)
        self.check("#43 从下拉里换成有数据的通道（这一场自动挑到的是空模板）",
                   switched == "Vx KF|G Force Lat|G Force Long", switched)

        # 1) 真拖拽：相机跟着转，画布像素也真的变
        start_camera = made.get("camera")
        start_pixels = pixels(component_id)
        x0 = box["x"] - box["w"] * 0.2
        y0 = box["y"]
        self.browser.mouse("mouseMoved", x0, y0, self.session, buttons=0)
        self.browser.mouse("mousePressed", x0, y0, self.session)
        for step in range(1, 7):
            self.browser.mouse("mouseMoved", x0 + step * 11, y0 + step * 4, self.session)
            time.sleep(0.05)
        self.browser.mouse("mouseReleased", x0 + 66, y0 + 24, self.session, buttons=0)
        time.sleep(0.4)
        dragged = attitude_of()
        self.check("#43 真拖一下画布，相机跟着转",
                   bool(dragged) and bool(start_camera)
                   and abs(dragged["camera"]["yaw"] - start_camera["yaw"]) > 5,
                   "%s → %s" % (start_camera, dragged and dragged.get("camera")))
        self.check("#43 拖拽之后画布像素真的变了", pixels(component_id) != start_pixels,
                   start_pixels)

        # 2) 真按 R：回到默认视角（E 被「状态与故障」占着，所以姿态用 R）
        self.browser.click(x0, y0, self.session)          # 让焦点离开任何输入框
        # 单字母键要走 key_named：只给 text 的话页面收到的 event.key 不是 "r"，
        # 单键快捷键一条都不触发（A78 那条"回车跳不动"就是这么空转的）。
        self.browser.key_named("r", "KeyR", 82, self.session, text="r")
        time.sleep(0.4)
        reset = attitude_of()
        self.check("#43 按 R 把视角复位",
                   bool(reset) and reset["camera"] == {"yaw": 26, "pitch": 18},
                   "%s · toast=%r" % (reset and reset.get("camera"), self.toast()))

        # 3) 真移动光标：光标换一刻，姿态跟着重画
        cursor_before = self.js("i3pro.state.cursor")
        pixels_before = pixels(component_id)
        graph = json.loads(self.js(
            "JSON.stringify((function(){var c=i3pro.state.components.filter("
            "function(x){return x.type==='graph';})[0];"
            "var el=c&&document.querySelector('[data-id=\"'+c.id+'\"] canvas');"
            "return el?__center(el):null;})())") or "null")
        if graph:
            self.browser.mouse("mouseMoved", graph["x"] - graph["w"] * 0.3, graph["y"],
                               self.session, buttons=0)
            time.sleep(0.4)
            self.check("#43 在图上游走换到了另一刻的光标",
                       self.js("i3pro.state.cursor") != cursor_before,
                       "%s → %s" % (cursor_before, self.js("i3pro.state.cursor")))
            head_before = head_of(component_id)
            pixels_now = pixels(component_id)
            self.check("#43 光标一换，姿态画布跟着重画",
                       pixels_now != pixels_before,
                       "读数 %r → %r · 像素 %s vs %s"
                       % (head_before, head_of(component_id), pixels_before, pixels_now))

        self.js("(function(){var c=i3pro.state.components.filter(function(x)"
                "{return x.type==='attitude';})[0];"
                "if(c)i3pro.componentAction(c,'close');return true;})()")
        time.sleep(0.3)

    # ------------------------------------------- A90 热力图
    def heatmap(self):
        """#45 热力图：真点「添加」加一件、真悬停、色标真的画出来、真切范围、量一次响应。

        假 DOM 证明不了两件事：**色标真的画在画布上**（那边的 canvas 只记计数、没有
        文字）与**鼠标划过真的圈到那一格**。这一票的验收里"悬停提示出现"与"切 range
        后点数变化"正是这一类。（"分箱的数算得对不对"归单测：与 `numpy.histogram2d`
        逐箱对拍，这里不重复。）
        """
        def head_of(component_id):
            return self.js(
                "(function(){var el=document.querySelector('[data-id=\"" + component_id
                + "\"] .graphhead');return el?el.textContent:'';})()")

        def box_of(component_id):
            raw = self.js(
                "JSON.stringify((function(){var c=document.querySelector('[data-id=\""
                + component_id + "\"] canvas');return c?__center(c):null;})())")
            return json.loads(raw) if raw and raw != "null" else None

        def fills_since(mark):
            raw = self.js("JSON.stringify(window.__fills.slice(%s).map(function(f)"
                          "{return f.t;}))" % int(mark))
            return json.loads(raw) if raw and raw != "null" else []

        self.js("(function(){document.getElementById('addType').value='heatmap';"
                "return true;})()")
        add = json.loads(self.js("JSON.stringify(__rectOf('addBtn'))"))
        before = self.js("i3pro.state.components.length")
        self.browser.click(add["x"], add["y"], self.session)
        self.browser.wait_for("i3pro.state.components.length === %d" % (before + 1),
                              self.session, timeout=10)
        time.sleep(0.8)
        made = json.loads(self.js(
            "JSON.stringify((function(){var c=i3pro.state.components.filter(function(x)"
            "{return x.type==='heatmap';})[0];"
            "return c?{id:c.id,x:c.config.x,y:c.config.y,z:c.config.z,range:c.config.range}"
            ":null;})())") or "null")
        self.check("#45 真点「添加」就加出一个热力图组件", bool(made),
                   json.dumps(made, ensure_ascii=False))
        if not made:
            return
        component_id = made["id"]
        self.check("#45 默认挑到两条不同的通道（同一条自己对自己只有一条对角线）",
                   bool(made["x"]) and bool(made["y"]) and made["x"] != made["y"],
                   "%s × %s" % (made["x"], made["y"]))

        # 换成两份金标准上一定有数的三条通道。原生 <select> 在真命中测试里点不开
        # （系统弹层），所以按仓库里既有的做法改值 + 派发 change。
        switched = self.js(
            "(function(){var el=document.querySelector('[data-id=\"" + component_id
            + "\"] .scattercfg');if(!el)return 'no-bar';var s=el.querySelectorAll('select');"
            "if(s.length<5)return 'selects='+s.length;"
            "['Vx KF','G Force Lat','TH'].forEach(function(name,i){s[i].value=name;"
            "s[i].dispatchEvent(new Event('change'));});"
            "var c=i3pro.state.components.filter(function(x){return x.type==='heatmap';})[0];"
            "return [c.config.x,c.config.y,c.config.z].join('|');})()")
        time.sleep(0.6)
        self.check("#45 从三个下拉里换成有数据的通道", switched == "Vx KF|G Force Lat|TH",
                   switched)
        ready = self.browser.wait_for(
            "String((document.querySelector('[data-id=\"" + component_id
            + "\"] .graphhead')||{}).textContent||'').indexOf('点') >= 0",
            self.session, timeout=30)
        self.check("#45 serve 模式下真的把热力图算回来了（抬头有点数）", bool(ready),
                   head_of(component_id))
        window_head = head_of(component_id)
        self.check("#45 抬头写着这是哪一段 + 多少点 + 色标范围",
                   "当前窗口" in window_head and "点" in window_head
                   and "样本数" in window_head, window_head)

        # 真切范围：当前窗口 → 整场，点数必须变（同一段数据，窗口大了样本就多）
        self.js("(function(){var el=document.querySelector('[data-id=\"" + component_id
                + "\"] .scattercfg');var s=el.querySelectorAll('select');"
                "s[4].value='all';s[4].dispatchEvent(new Event('change'));return true;})()")
        time.sleep(0.4)
        self.browser.wait_for(
            "String((document.querySelector('[data-id=\"" + component_id
            + "\"] .graphhead')||{}).textContent||'').indexOf('整场') >= 0",
            self.session, timeout=30)
        whole_head = head_of(component_id)
        self.check("#45 切到「整场」之后抬头换了一段、点数也跟着变",
                   "整场" in whole_head and whole_head != window_head,
                   "%s -> %s" % (window_head, whole_head))

        # 悬停：真把鼠标移到画布中间 —— 抬头要写出"这一箱 N 点"
        box = box_of(component_id)
        self.check("#45 热力图画布点得到（有面积）",
                   bool(box) and box.get("w", 0) > 0 and box.get("h", 0) > 0,
                   json.dumps(box, ensure_ascii=False))
        if not box:
            return
        mark = self.js("window.__fills.length")
        # 空格子也照样会写"这一箱 0 点"（那是真的没有样本），所以多探几个位置，
        # 要求**至少有一格是有样本的**——不然这条断言连"悬停映射对不对"都证明不了。
        hover_head = ""
        for dx, dy in ((0.0, 0.0), (-0.12, -0.05), (0.15, 0.1), (-0.2, 0.12)):
            self.browser.mouse("mouseMoved", box["x"] + box["w"] * dx,
                               box["y"] + box["h"] * dy, self.session, buttons=0)
            time.sleep(0.4)
            hover_head = head_of(component_id)
            if "这一箱" in hover_head and "这一箱 0 点" not in hover_head:
                break
        self.check("#45 真悬停：抬头写出「这一箱 N 点」，而且真的落到有样本的格子上",
                   "这一箱" in hover_head and "这一箱 0 点" not in hover_head, hover_head)
        drawn = fills_since(mark)
        self.check("#45 色标真的画在画布上（画布文字里有「点数」）",
                   any("点数" in str(one) for one in drawn),
                   "这一段画布文字：" + json.dumps(drawn[-12:], ensure_ascii=False))

        # 真发一次整场 + 第三通道着色的请求，把耗时量下来（写进 A90）
        spent = self.js(
            "(async function(){var t0=performance.now();"
            "await i3pro.apiGet(i3pro.apiUrl('/heatmap',{x:'Vx KF',y:'G Force Lat',z:'TH',"
            "bx:60,by:40,scheme:'z-mean'}));"
            "return Math.round(performance.now()-t0);})()")
        self.check("#45 整场 60×40 带第三通道着色的一次请求 < 2000 ms（实测写进 A90）",
                   isinstance(spent, (int, float)) and spent < 2000, "%s ms" % spent)
        print("  NOTE  #45 实测一次 /heatmap（整场 60×40 + z 着色）：%s ms" % spent)
        self.browser.shot(os.path.join(ROOT, "out", "shots", "verify-heatmap.png"),
                          self.session)

        # 收尾：把这件组件撤掉，别给后面的验收留东西
        self.js("(function(){var c=i3pro.state.components.filter(function(x)"
                "{return x.type==='heatmap';})[0];"
                "if(c)i3pro.componentAction(c,'close');return true;})()")
        time.sleep(0.3)

    # ------------------------------------------- A92 组件栏的小改
    def component_bar(self):
        """#48：添加入口分组、不可用原因、空状态、换成与复制。

        DOM 证明得了"下拉里有分组、原因行说得出下一步"；真鼠标这一段只补它证明不了
        的两件：点「复制」之后两块能各改各的，点「换成…」之后同一个 id 还在、通道
        还在。
        """
        def button_at(component_id, label):
            raw = self.js(
                "JSON.stringify((function(){var el=document.querySelector('[data-id=\""
                + component_id + "\"] .ctools');if(!el)return null;"
                "var b=[].filter.call(el.querySelectorAll('button'),function(x)"
                "{return x.textContent===" + json.dumps(label, ensure_ascii=False) + ";})[0];"
                "return b?__center(b):null;})())") or "null"
            return json.loads(raw)

        self.js("i3pro.applyPreset(i3pro.worksheetCatalogue().sheets[0].name); true")
        time.sleep(0.8)
        groups = json.loads(
            self.js("JSON.stringify([].map.call(document.querySelectorAll('#addType optgroup'),"
                    "function(g){return g.label;}))") or "[]")
        self.check("#48 添加组件下拉按用途分组",
                   groups == ["曲线", "聚合", "姿态", "状态与报告"], groups)
        saved = json.loads(
            self.js("JSON.stringify({channels:i3pro.data.channels,status:i3pro.data.status})"))
        self.js("i3pro.data.channels=[];i3pro.data.status=[];"
                "document.getElementById('addType').value='graph';"
                "i3pro.renderAddTypeNote();true")
        note = self.js("document.getElementById('addTypeNote').textContent")
        self.check("#48 没有通道时添加入口写明原因和下一步",
                   "这一场没有任何通道" in str(note) and "先导入数据" in str(note), note)
        self.js("i3pro.data.channels=%s;i3pro.data.status=%s;i3pro.renderAddTypeNote();true"
                % (json.dumps(saved["channels"], ensure_ascii=False),
                   json.dumps(saved["status"], ensure_ascii=False)))
        self.check("#48 空组件状态说得出下一步",
                   self.js("i3pro.emptyComponentHint()") == "把左边的通道拖进来",
                   self.js("i3pro.emptyComponentHint()"))

        graph = json.loads(
            self.js("JSON.stringify((function(){var c=i3pro.state.components.filter(function(x)"
                    "{return x.type==='graph';})[0];"
                    "if(!c)return null;c.config.channels=['Vx KF','GPS Speed'];"
                    "i3pro.buildWorksheet();i3pro.renderAll();"
                    "return {id:c.id,x:c.x,y:c.y};})())") or "null")
        self.check("#48 工作表里有一张可复制、可换类型的图", bool(graph), graph)
        if not graph:
            return
        copy_btn = button_at(graph["id"], "复制")
        self.check("#48 真点得到组件栏上的「复制」",
                   bool(copy_btn) and copy_btn.get("w", 0) > 0,
                   json.dumps(copy_btn, ensure_ascii=False))
        if not copy_btn:
            return
        self.browser.click(copy_btn["x"], copy_btn["y"], self.session)
        time.sleep(0.6)
        made_js = (
            "JSON.stringify((function(){var a=i3pro.state.components;"
            "var orig=a.filter(function(x){return x.id==="
            + json.dumps(graph["id"]) + ";})[0];if(!orig)return null;"
            "var idx=a.indexOf(orig);if(idx<0||!a[idx+1])return null;"
            "var c=a[idx+1];"
            "var copyX=c.x,copyY=c.y;"
            "c.config.channels=(c.config.channels||[]).concat(['__copy__']);"
            "return {id:c.id,x:copyX,y:copyY,independent:c.config!==orig.config"
            "&&(orig.config.channels||[]).indexOf('__copy__')<0};})())")
        made = json.loads(self.js(made_js) or "null")
        self.check("#48 真点「复制」后多一块，而且两块配置独立",
                   bool(made) and made.get("independent") is True, made)
        if not made:
            return

        replace_btn = button_at(made["id"], "换成…")
        self.check("#48 真点得到组件栏上的「换成…」",
                   bool(replace_btn) and replace_btn.get("w", 0) > 0,
                   json.dumps(replace_btn, ensure_ascii=False))
        if not replace_btn:
            return
        self.browser.click(replace_btn["x"], replace_btn["y"], self.session)
        time.sleep(0.4)
        chip = json.loads(
            self.js("JSON.stringify((function(){var b=[].filter.call("
                    "document.querySelectorAll('.popmenu .popchip'),function(x)"
                    "{return x.textContent==='直方图';})[0];return b?__center(b):null;})())")
            or "null")
        self.check("#48 换成菜单里有「直方图」",
                   bool(chip) and chip.get("w", 0) > 0, json.dumps(chip, ensure_ascii=False))
        if not chip:
            self.js("i3pro.closePopMenu(); true")
            return
        self.browser.click(chip["x"], chip["y"], self.session)
        time.sleep(0.8)
        after = json.loads(
            self.js("JSON.stringify((function(){var c=i3pro.state.components.filter(function(x)"
                    "{return x.id===" + json.dumps(made["id"]) + ";})[0];"
                    "return c?{id:c.id,type:c.type,channel:c.config.channel,x:c.x,y:c.y}:null;})())")
            or "null")
        self.check("#48 真点「换成…」后通道与位置都还在",
                   bool(after) and after.get("type") == "histogram"
                   and after.get("channel") == "Vx KF"
                   and after.get("x") == made.get("x") and after.get("y") == made.get("y"),
                   after)
        self.js("(function(){var c=i3pro.state.components.filter(function(x)"
                "{return x.id===" + json.dumps(made["id"]) + ";})[0];"
                "if(c)i3pro.componentAction(c,'close');return true;})()")
        time.sleep(0.3)

    # ------------------------------------------- A89 光标 A–B 区间统计
    def cursor_stats(self):
        """#44 光标 A–B 区间统计：真点「显示 → A–B」、真放 A/B、数跟着 B 跑。

        假 DOM 证明不了"这个菜单点得到"：那里的元素没有面积、没有命中测试。
        这一票的验收里"摆好 A/B 后抬头数值随光标变化"正是这一类。
        （三个数的**算法**在无头那边钉得死：受控序列 + 精确读数，这里不重复。）
        """
        sys.path.insert(0, os.path.join(ROOT, "src"))
        from i3pro import cursorstats as cursorstatsmod

        def graph_of():
            raw = self.js(
                "JSON.stringify((function(){var c=i3pro.state.components.filter(function(x)"
                "{return x.type==='graph';})[0];"
                "return c?{id:c.id,range:i3pro.statsRangeOf(c)}:null;})())")
            return json.loads(raw) if raw and raw != "null" else None

        def head_of(component_id):
            """抬头第一行的三格数字 + 那句提示（有没有显示出来）。"""
            raw = self.js(
                "(function(){var el=document.querySelector('[data-id=\"" + component_id
                + "\"] .graphhead');if(!el)return null;"
                "var rows=el.querySelectorAll('.lrow');"
                "for(var i=0;i<rows.length;i++){"
                "if(String(rows[i].className).indexOf('labnote')>=0)continue;"
                "var mm=rows[i].querySelectorAll('.lmm');"
                "if(!mm.length)continue;"
                "var note=el.querySelector('.labnote');"
                "return JSON.stringify({mm:[].map.call(mm,function(s){return s.textContent;}),"
                "shown:[].map.call(mm,function(s){return s.style.display!=='none';}),"
                "note:note?note.textContent:'',"
                "noteShown:!!(note&&note.style.display!=='none')});}return null;})()")
            return json.loads(raw) if raw and raw != "null" else None

        self.js("i3pro.applyPreset(i3pro.worksheetCatalogue().sheets[0].name); true")
        time.sleep(0.8)
        made = graph_of()
        self.check("#44 页面上有一件时间/距离图（统计范围挂在它上面）", bool(made),
                   json.dumps(made, ensure_ascii=False))
        if not made:
            return
        component_id = made["id"]

        # 1) 真点标题栏上的「显示」：菜单真的开出来（这一件组件的按钮里按文案找）
        button = json.loads(self.js(
            "JSON.stringify((function(){var el=document.querySelector('[data-id=\"" + component_id
            + "\"] .ctools');if(!el)return null;"
            "var b=[].filter.call(el.querySelectorAll('button'),function(x)"
            "{return x.textContent==='显示';})[0];"
            "return b?__center(b):null;})())") or "null")
        self.check("#44 「显示」按钮点得到", bool(button) and button.get("w", 0) > 0,
                   json.dumps(button, ensure_ascii=False))
        if not button:
            return
        self.browser.click(button["x"], button["y"], self.session)
        time.sleep(0.4)

        # 2) 菜单里真点「A–B」
        chip = json.loads(self.js(
            "JSON.stringify((function(){"
            "var c=document.querySelector('.popmenu [data-stats-range=\"ab\"]');"
            "return c?__center(c):null;})())") or "null")
        self.check("#44 菜单里有一枚点得到的「A–B」按钮",
                   bool(chip) and chip.get("w", 0) > 0, json.dumps(chip, ensure_ascii=False))
        if not chip:
            self.js("i3pro.closePopMenu(); true")
            return
        self.browser.click(chip["x"], chip["y"], self.session)
        time.sleep(0.5)
        self.check("#44 真点「A–B」之后这一件组件换了统计范围",
                   (graph_of() or {}).get("range") == "ab",
                   json.dumps(graph_of(), ensure_ascii=False))
        self.js("i3pro.closePopMenu(); true")
        time.sleep(0.2)

        # 3) 没有基准光标：抬头不印数字，改说那句现成的下一步（与导出面板同一句）
        self.js("i3pro.state.datumOn=false;i3pro.state.datum=null;i3pro.renderAll();true")
        time.sleep(0.3)
        empty = head_of(component_id)
        self.check("#44 选了 A–B 又没放基准光标：抬头不印数字、改说下一步",
                   bool(empty) and not any(empty["shown"])
                   and all(one == "" for one in empty["mm"]) and empty["noteShown"]
                   # 那句话不在这个文件里再抄一遍：拿后端那份常量当裁判
                   and empty["note"].strip() == cursorstatsmod.MISSING_AB,
                   json.dumps(empty, ensure_ascii=False))

        # 4) 真移动鼠标 + 真按空格放 A，再移动鼠标：三个数跟着 B 跑
        box = json.loads(self.js(
            "JSON.stringify((function(){var c=document.querySelector('[data-id=\"" + component_id
            + "\"] canvas');return c?__center(c):null;})())") or "null")
        if not box:
            self.check("#44 这一件组件的画布点得到", False, "找不到 canvas")
            return
        left = box["x"] - box["w"] * 0.25
        right = box["x"] + box["w"] * 0.30
        self.browser.mouse("mouseMoved", left, box["y"], self.session, buttons=0)
        time.sleep(0.4)
        at_left = self.js("i3pro.state.cursor")
        self.browser.key_named(" ", "Space", 32, self.session, text=" ")
        time.sleep(0.4)
        self.check("#44 真按空格把基准光标 A 放在光标处",
                   self.js("i3pro.state.datumOn===true") is True
                   and abs(self.js("i3pro.state.datum") - at_left) < 1e-9,
                   "A=%s，放之前光标在 %s" % (self.js("i3pro.state.datum"), at_left))
        first = head_of(component_id)
        self.check("#44 摆好 A/B 之后抬头又印数字了",
                   bool(first) and all(first["shown"]) and not first["noteShown"],
                   json.dumps(first, ensure_ascii=False))

        # 前端算出来的区间与后端同一口径（两边分叉时，一个在浏览器里、一个在
        # points/报表里，用户看到的是两套数）
        span = json.loads(self.js("JSON.stringify(i3pro.abSpan())"))
        expected = cursorstatsmod.span_of(self.js("i3pro.state.datum"),
                                          self.js("i3pro.state.cursor"))
        self.check("#44 前端算出来的 A–B 区间与后端 cursorstats 同一口径",
                   span.get("from") == expected[0] and span.get("to") == expected[1],
                   "%s vs %s" % (json.dumps(span), expected))

        self.browser.mouse("mouseMoved", right, box["y"], self.session, buttons=0)
        time.sleep(0.5)
        second = head_of(component_id)
        self.check("#44 主光标 B 一动，抬头那三个数跟着变",
                   bool(second) and all(second["shown"]) and second["mm"] != first["mm"],
                   "%s -> %s" % (json.dumps(first and first["mm"]),
                                 json.dumps(second and second["mm"])))
        # 截图是给人复核用的，不是断言：导入那两关开过别的标签页之后，被压到后台的
        # 这张 target 上 captureScreenshot 会一直等（实测 30 s 超时）——先把它调到
        # 前台；万一还是没截下来，说一声、别把整条回归拖下水。
        try:
            self.browser.call("Page.bringToFront", session=self.session)
            time.sleep(0.2)
            self.browser.shot(os.path.join(ROOT, "out", "shots", "verify-ab-stats.png"),
                              self.session)
        except Exception as trouble:      # noqa: BLE001 - 截图失败不该是一条断言
            print("  NOTE  #44 截图没存下来（不影响断言）：%s" % trouble)

        # 收尾：范围回到默认（可见区间），别给后面的验收留下一个改了范围的组件
        self.js("(function(){var c=i3pro.state.components.filter(function(x)"
                "{return x.type==='graph';})[0];"
                "if(c)i3pro.setStatsRange(c,'view');"
                "i3pro.state.datumOn=false;i3pro.renderAll();return true;})()")
        time.sleep(0.3)

    def write_seam(self):
        """A83：写路径只有 ``sessionRequest`` 一个出口，服务端那句中文原因要活着到用户眼前。

        这一条只有真浏览器验得了：失败那句 toast 是页面在 fetch 之后拼的，而假 DOM 里
        fetch 永远 reject（``smoke_viewer.js`` 自己写着），所以"HTTP 400 会不会盖掉
        服务端的原因"在无头那关根本看不见——A83 修的就是这一处。
        """
        self.check("A83 页面上有写路径的唯一出口 sessionRequest",
                   self.js("typeof i3pro.sessionRequest") == "function",
                   self.js("typeof i3pro.sessionRequest"))
        # 一条落不到这一场里的注释：服务端 400 并给出中文原因（`notes.normalize`）
        raw = self.js(
            "(async function(){var ok=await i3pro.saveNotes([{time:1e9,text:'越界'}]);"
            "var el=document.getElementById('toast');"
            "return JSON.stringify({ok:ok,toast:(el&&el.textContent)||''});})()")
        got = json.loads(raw) if isinstance(raw, str) else (raw or {})
        text = got.get("toast") or ""
        self.check("A83 服务端拒绝时，前端说的是它那句中文原因（不是 HTTP 400）",
                   got.get("ok") is False and "HTTP 400" not in text and len(text) > 10,
                   text)

    # ------------------------------------------------ A79 曲线连接（Up-sampling）
    def curve_join(self, base_url):
        """A79：i2 Pro 的 Channel Up-sampling——真点「显示 → 曲线连接」，曲线**真的变了**。

        判据不是"选中状态变了"，而是**取回来的值变了**：同一条 10 Hz 的通道、同一个
        2 秒窗口，线性时相邻点互不相同（斜线），阶梯保持时是 10 个一样的值（平顶）。
        最后再点回线性，值要变回去——一次单向的闩不算过。
        """
        # 挑一条"线性与保持真的不一样"的慢通道：把**所有**比主采样率慢的通道一次性
        # 各取一份 2 秒窗口（两个请求），在 Python 这边比。**不能随便挑一条**——
        # 1 Hz 的开关量（Gear）本身就是平的，两种取值同一个答案，量不出差别
        # （第一次跑就挑中了它）；而只扫前 8 条会先撞上一堆全空的通道。
        candidates = json.loads(self.js(
            "(function(){var s=(i3pro.data.channels||[]).filter(function(c){"
            "return c.rate>=2&&c.rate<50&&!c.derived&&c.has_data;});"
            "return JSON.stringify(s.map(function(c){return c.name;}));})()") or "[]")
        self.check("A79 这一场有比主采样率慢的通道（不然看不到平顶）",
                   bool(candidates), len(candidates))
        if not candidates:
            return

        def trace_window(channels, mode):
            url = (base_url + "api/session/" + urllib.parse.quote(str(sess_name))
                   + "/trace?channels=" + urllib.parse.quote(",".join(channels))
                   + "&from=0&to=2&buckets=2000&upsample=" + mode)
            with urllib.request.urlopen(url, timeout=60) as handle:
                return json.loads(handle.read().decode("utf-8"))

        sess_name = self.js("i3pro.data.session")
        linear_all = trace_window(candidates, "linear")
        previous_all = trace_window(candidates, "previous")
        name = ""
        for candidate in candidates:
            lin = (linear_all.get(candidate) or {}).get("value") or []
            prev = (previous_all.get(candidate) or {}).get("value") or []
            if len(lin) >= 10 and len(prev) >= 10 and lin[:10] != prev[:10]:
                name = candidate
                probe = {"name": candidate, "linear": lin[:10], "previous": prev[:10]}
                break
        self.check("A79 找到一条「线性与保持真的不一样」的慢通道（不然这一票没得验）",
                   bool(name), name or "204 条候选里一条都没有")
        if not name:
            return
        rate = (linear_all[name].get("rate"))

        def values(n):
            raw = self.js(
                "(function(){var t=i3pro.state.traces[%s]||{};"
                "return JSON.stringify((t.value||[]).slice(0,%d));})()"
                % (json.dumps(name), n))
            return json.loads(raw) if raw else []

        def load(name):
            """把这条通道放进第一张图、窗口缩到 2 秒，并**立刻**取一次数。

            直接改 `state.view` 不会触发取数（那条防抖只在缩放/平移那条路上），
            所以这里显式强制一次——不强制的话读到的永远是上一次那几条通道。
            """
            self.js(
                "(function(){var g=i3pro.state.components.filter(function(c){"
                "return c.type==='graph';})[0];g.config.channels=[%s];"
                "i3pro.state.view=[0,2];i3pro.state.windowKey='';"
                "i3pro.refreshWindowTraces(true);i3pro.renderAll();return true;})()"
                % json.dumps(name))
            self.browser.wait_for(
                "((i3pro.state.traces[%s]||{}).value||[]).length>0" % json.dumps(name),
                self.session, timeout=15)

        load(name)
        linear = values(10)
        self.check("A79 界面默认取的也是线性那一份（前后端默认是同一个词）",
                   linear == probe["linear"],
                   "界面 %s · 直接问接口 %s" % (linear[:4], probe["linear"][:4]))

        self.browser.wait_for(
            "((i3pro.state.traces[%s]||{}).value||[]).length>0" % json.dumps(name),
            self.session, timeout=25)
        linear = values(10)
        self.check("A79 默认是线性：同一个 10 Hz 通道的相邻点互不相同",
                   len(linear) >= 10 and len(set(linear[:10])) == 10,
                   "%s Hz · %s" % (rate, linear[:6]))

        def rect_of_local(selector):
            raw = self.js(
                "(function(){var el=document.querySelector(%r);if(!el)return 'null';"
                "var r=el.getBoundingClientRect();return JSON.stringify({x:r.left+r.width/2,"
                "y:r.top+r.height/2,w:r.width,h:r.height});})()" % selector)
            return json.loads(raw) if raw and raw != "null" else None

        def open_display_menu():
            box = rect_of_local("#worksheet .comp .compbar button[title*='配色与抬头']")
            if not box:
                return False
            self.browser.click(box["x"], box["y"], self.session)
            time.sleep(0.5)
            return bool(self.js("!!document.querySelector('.popmenu')"))

        def click_mode(mode):
            box = rect_of_local(".popmenu .popchip[data-upsample='%s']" % mode)
            if not box:
                return False
            self.browser.click(box["x"], box["y"], self.session)
            time.sleep(1.4)
            return True

        self.check("A79 「显示」菜单真的弹得出来", open_display_menu())
        self.browser.shot(os.path.join(ROOT, "out", "shots", "verify-curve-join-menu.png"),
                          self.session)
        self.check("A79 菜单里有「曲线连接」这一排，而且点得到「阶梯保持」",
                   open_display_menu() and click_mode("previous"))
        self.browser.wait_for(
            "(i3pro.state.traces[%s]||{}).upsample==='previous'" % json.dumps(name),
            self.session, timeout=20)
        kept = values(10)
        self.check("A79 选「阶梯保持」之后取回来的值真的换了（就是保持前值那一份）",
                   kept == probe["previous"] and kept != probe["linear"],
                   "阶梯 %s · 线性 %s" % (kept[:4], probe["linear"][:4]))
        self.check("A79 服务端回的那一份也说是阶梯保持",
                   self.js("(i3pro.state.traces[%s]||{}).upsample||''" % json.dumps(name))
                   == "previous",
                   self.js("(i3pro.state.traces[%s]||{}).upsample||''" % json.dumps(name)))
        title = self.js(
            "(document.querySelector('#worksheet .comp .ctitle')||{}).textContent || ''")
        self.check("A79 组件标题里写明了现在这一屏是阶梯保持", "阶梯保持" in (title or ""), title)
        self.browser.shot(os.path.join(ROOT, "out", "shots", "verify-curve-join-step.png"),
                          self.session)
        self.check("A79 再点回「线性」，值也变回去（不是一次性的闩）",
                   open_display_menu() and click_mode("linear"))
        self.browser.wait_for(
            "(i3pro.state.traces[%s]||{}).upsample==='linear'" % json.dumps(name),
            self.session, timeout=20)
        back = values(10)
        self.check("A79 点回线性之后取回来的又是线性那一份（不是一次性的闩）",
                   back == probe["linear"], back[:6])
        # 收尾：把窗口放开，后面的验收从整场那一屏继续
        self.js("(function(){i3pro.state.view=null;i3pro.renderAll();return true;})()")


def _blob(path):
    with open(path, "rb") as handle:
        return base64.b64encode(handle.read()).decode("ascii")


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

    # 工作表文件也复制一份出来：这一票的验收会真的新建 / 改名 / 删除它们。
    worksheets_root = stage_worksheets(work_dir)
    server = start_server(work_dir, args.port, worksheets_root)
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
            checker.worksheets()
            checker.worksheet_editing(work_dir)
            checker.missing_channels(work_dir)
            checker.channel_processing(work_dir, args.session)
            checker.display_settings(work_dir)
            # A78 / A79：侧边栏那一排跳转按钮，以及「曲线连接」换了之后值真的跟着换。
            checker.sidebar_jump()
            checker.curve_join("http://127.0.0.1:%d/" % args.port)
            checker.aliases(work_dir)
            checker.notes(work_dir, args.session)
            checker.gps(work_dir, args.session)
            checker.write_seam()
            checker.imports("http://127.0.0.1:%d/" % args.port, work_dir)
            checker.text_import("http://127.0.0.1:%d/" % args.port, work_dir)
            # 接着用刚导入的那一场（CSV 场次）验 A77：末尾那句"再应用一次"就是
            # 用户踩到的那一步；这一条必须在 text_import **之后**。
            checker.channel_processing_on_an_imported_session(
                "http://127.0.0.1:%d/" % args.port, work_dir)
            # #43 姿态图：假 DOM 证明不了的那三条（拖得动 / 复位 / 跟光标）。
            checker.attitude()
            # #44 光标 A–B 区间统计：真点「显示 → A–B」、真放 A/B、数跟着 B 跑。
            checker.cursor_stats()
            # #45 热力图：真加一件、真悬停、色标真的画出来、真切范围、量一次响应。
            checker.heatmap()
            # #48 组件栏小改：真复制一块、真换成直方图，通道与位置都还在。
            checker.component_bar()
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
