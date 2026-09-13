"""A tiny local web server: ``python -m i3pro serve``.

Pure standard library (``http.server``) - no FastAPI, no Flask, nothing to
install, nothing to keep patched. It exists so a whole squad can point at one
laptop on the pit wall, and so every view is a URL you can paste into the team
chat instead of a screenshot::

    http://192.168.1.20:8731/session/20260524-%E8%80%90%E4%B9%85%E6%AD%A3%E8%B5%9B
        ?channels=Vx%20KF,G%20Force%20Long&ref=10&cmp=12

**这个文件只做 HTTP 管道**：解析 URL、按需读请求体、把 ``Response`` 发出去，
外加两张 HTML 页面（场次列表、工作台）。判断与计算在 :mod:`i3pro.api`，
"场次从哪来"在 :mod:`i3pro.library`（ticket #23 拆出来的）。

The parsed ``.ld`` files are memory mapped and kept in an LRU cache, so opening
the same session twice costs nothing.
"""

from __future__ import annotations

import select
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import render
from .api import Api, Body, ClientGone, Response, csv_arg, float_arg, int_arg
from .library import SessionLibrary, dumps, json_safe

__all__ = [
    "SessionLibrary",
    "bind",
    "dumps",
    "index_page",
    "make_handler",
    "serve",
]


def make_handler(library: SessionLibrary, buckets: int = render.DEFAULT_BUCKETS):
    """HTTP 管道：只负责把字节收进来、把 ``Response`` 发出去。

    **判断都在 :mod:`i3pro.api`。** 保留"先建 library、再建 handler"这个签名，
    因为 ``serve()`` 与测试都按这个顺序来（handler 是给 ``ThreadingHTTPServer``
    用的类，不是函数返回值意义上的对象）。
    """
    api = Api(library, buckets)

    class Handler(BaseHTTPRequestHandler):
        server_version = "i3pro"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # keep the console clean
            pass

        # ------------------------------------------------------------- verbs
        def do_GET(self):  # noqa: N802 - http.server API
            self.dispatch("GET")

        def do_HEAD(self):  # noqa: N802 - http.server API
            self.dispatch("HEAD")

        def do_POST(self):  # noqa: N802 - http.server API
            self.dispatch("POST")

        def do_PUT(self):  # noqa: N802 - http.server API
            self.dispatch("PUT")

        # ------------------------------------------------------------ plumbing
        def _content_length(self) -> int:
            try:
                return max(0, int(self.headers.get("Content-Length") or 0))
            except (TypeError, ValueError):
                return 0

        def _read_body(self, length: int) -> bytes:
            """只在动作真的要请求体时才被调用（上传 100 MB 也不预先读）。"""
            return self.rfile.read(length)

        def _client_alive(self) -> bool:
            """客户端还在不在？导出写到一半用它早停（尽力而为：看不出来就当还在）。"""
            try:
                ready, _, _ = select.select([self.connection], [], [], 0)
                if not ready:
                    return True
                return self.connection.recv(1, socket.MSG_PEEK) != b""
            except OSError as exc:           # 非阻塞套接字"现在没数据"= 还连着
                return isinstance(exc, BlockingIOError)

        def dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            parts = [unquote(p) for p in parsed.path.split("/") if p]
            try:
                response = self.route(parts, query, method)
            except ClientGone:
                return  # 客户端走了：不算错误，也不用回东西
            except Exception as exc:  # 页面那两条路也可能炸；兜住，别把连接晾着
                response = Response(
                    500,
                    dumps({"error": f"{type(exc).__name__}: {exc}"}).encode("utf-8"),
                )
            if response is None:  # 路由忘了 return：宁可回 500，也不要晾着连接
                response = Response(
                    500,
                    dumps({
                        "error": f"/{'/'.join(parts)} 没有返回响应（这是服务端的 bug）"
                    }).encode("utf-8"),
                )
            try:
                self.send(response)
            except (BrokenPipeError, ConnectionResetError):
                pass  # 发到一半客户端走了；临时文件在 send 的 finally 里已经删了

        def route(self, parts: list[str], query: dict, method: str) -> Response:
            if not parts:
                return self._html(index_page(library))
            if parts[0] == "api":
                return api.handle(
                    parts[1:], query, method,
                    Body(self._content_length(), self._read_body, alive=self._client_alive),
                )
            if parts[0] == "session" and len(parts) >= 2:
                return self._html(self.session_page(parts[1], query))
            if parts[0] == "favicon.ico":
                return Response(204, b"")
            return Response(404, dumps({"error": "no such path"}).encode("utf-8"))

        def _html(self, text: str, status: int = 200) -> Response:
            return Response(status, text.encode("utf-8"), "text/html; charset=utf-8")

        def send(self, response: Response) -> None:
            """把 ``Response`` 发出去。

            ``response.path`` 有值时**流式**发那个文件（导出走这条）：200 MB 的 CSV
            不整份进内存，``Content-Length`` 是文件长度，界面据此画真实进度；
            发完、出错、连接中断三种情况都在 ``finally`` 里删掉它，磁盘不留垃圾。
            """
            path = response.path
            try:
                size = path.stat().st_size if path is not None else len(response.body)
                self.send_response(response.status)
                self.send_header("Content-Type", response.ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-store")
                for key, value in response.headers:
                    self.send_header(key, value)
                if response.close:
                    self.close_connection = True
                self.end_headers()
                if self.command == "HEAD":
                    return
                if path is not None:
                    with path.open("rb") as fh:
                        while True:
                            chunk = fh.read(1 << 20)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                    return
                if response.body:
                    self.wfile.write(response.body)
            finally:
                if path is not None:
                    path.unlink(missing_ok=True)
                    # 导出文件住在自己的临时目录里；删了文件顺带把空目录收掉。
                    if path.parent.name.startswith("i3pro-export-"):
                        try:
                            path.parent.rmdir()
                        except OSError:
                            pass

        # --------------------------------------------------------------- 页面
        def session_page(self, name: str, query: dict) -> str:
            log = library.get(name)
            payload = render.build_payload(
                log,
                channels=csv_arg(query, "channels") or None,
                ref=(query.get("ref") or [None])[0],
                cmp=(query.get("cmp") or [None])[0],
                buckets=int_arg(query, "buckets", buckets),
                api_base="/api",
                step=float_arg(query, "step", 1.0),
            )
            payload["session"] = name
            # 撤销的上一版只在这个进程的内存里，页面自己算不出来，只能由服务告诉它
            payload["laps_can_undo"] = api.can_undo(log)
            return render.render_page(payload)

    return Handler


def _lan_addresses() -> list[str]:
    """Best-effort list of this machine's non-loopback IPv4 addresses.

    The first entry is the address of the interface that would carry the default
    route (found via ``connect`` on a UDP socket - no packets are sent), which is
    the one a team mate can actually reach. The rest are other adapters, kept
    only as a hint because laptops often have WSL / Docker / VirtualBox
    interfaces that look like LANs but are not.
    """
    primary = None
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        primary = probe.getsockname()[0]
    except OSError:
        primary = None
    finally:
        probe.close()

    others: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address.startswith("127.") or address.startswith("169.254."):
                continue
            if address == primary or address in others:
                continue
            others.append(address)
    except OSError:
        pass

    if primary and not primary.startswith("127."):
        return [primary] + others[:3]
    return others[:4]


_IMPORT_BLOCK = """
<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;
            background:#171a21;border:1px solid #2b313c;border-radius:8px;
            padding:10px 12px;margin:0 0 14px">
  <input id="files" type="file" multiple accept=".ld,.ldx" style="display:none">
  <button id="pickBtn" style="background:#1d3b52;border:1px solid #4cc2ff;color:#e6e9ef;
          border-radius:6px;padding:6px 12px;cursor:pointer;font-size:13px">
    选择 .ld 文件导入
  </button>
  <span style="color:#8b94a7;font-size:12px">
    或者把文件直接拖进这个窗口 · 也可以把 .ld 拖到 <code>导入数据.bat</code> 上
  </span>
  <span id="importMsg" style="color:#4cc2ff;font-size:12px;margin-left:auto"></span>
</div>
<script>
(function () {
  var input = document.getElementById("files");
  var msg = document.getElementById("importMsg");
  var busy = false;

  document.getElementById("pickBtn").addEventListener("click", function () {
    if (!busy) input.click();
  });

  async function upload(list) {
    var files = Array.prototype.slice.call(list);
    if (!files.length || busy) return;
    busy = true;
    for (var i = 0; i < files.length; i++) {
      var file = files[i];
      msg.textContent = "上传 " + (i + 1) + "/" + files.length + ": " + file.name
        + " (" + (file.size / 1e6).toFixed(1) + " MB) …";
      try {
        var res = await fetch("/api/upload?name=" + encodeURIComponent(file.name),
                              { method: "PUT", body: file });
        var body = await res.json().catch(function () { return {}; });
        if (!res.ok) throw new Error(body.error || ("HTTP " + res.status));
      } catch (err) {
        msg.style.color = "#ff5d6c";
        msg.textContent = "导入失败: " + file.name + " — " + err.message;
        busy = false;
        return;
      }
    }
    msg.textContent = "导入完成，正在刷新…";
    location.reload();
  }

  input.addEventListener("change", function () { upload(input.files); });
  document.addEventListener("dragover", function (e) { e.preventDefault(); });
  document.addEventListener("drop", function (e) {
    e.preventDefault();
    if (e.dataTransfer && e.dataTransfer.files) upload(e.dataTransfer.files);
  });
})();
</script>
"""


def index_page(library: SessionLibrary, error: str | None = None) -> str:
    """A no-frills session picker; the real UI is the workbench itself."""
    if error:
        return (
            "<!doctype html><meta charset='utf-8'><title>i3pro</title>"
            "<body style='font:14px system-ui;padding:32px;background:#0f1115;color:#e6e9ef'>"
            f"<h1>i3pro 本地服务</h1><p>{error}</p>"
            "</body>"
        )
    rows = []
    for entry in library.listing():
        if "error" in entry:
            rows.append(
                f"<tr><td>{entry['name']}</td><td colspan='5' style='color:#ff5d6c'>"
                f"{entry['error']}</td></tr>"
            )
            continue
        best = "--" if entry.get("best_lap") is None else f"{entry['best_lap']:.3f} s"
        rows.append(
            "<tr>"
            f"<td><a href='{entry['url']}'>{entry['name']}</a></td>"
            f"<td>{entry.get('device', '')}</td>"
            f"<td>{entry.get('log_date', '')} {entry.get('log_time', '')}</td>"
            f"<td>{entry.get('duration', 0):.0f} s</td>"
            f"<td>{entry.get('channels', 0)}</td>"
            f"<td>{entry.get('complete_laps', 0)}</td>"
            f"<td>{best}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<meta charset="utf-8">
<title>i3pro - 场次</title>
<style>
 body {{ margin:0; background:#0f1115; color:#e6e9ef;
        font:14px/1.5 "Segoe UI","Microsoft YaHei",system-ui,sans-serif; }}
 header {{ padding:22px 28px; border-bottom:1px solid #2b313c; }}
 h1 {{ margin:0 0 4px; font-size:18px; }}
 p {{ margin:0; color:#8b94a7; }}
 main {{ padding:18px 28px; }}
 table {{ border-collapse:collapse; width:100%; }}
 th, td {{ text-align:left; padding:7px 10px; border-bottom:1px solid #2b313c; }}
 th {{ color:#8b94a7; font-weight:600; }}
 a {{ color:#4cc2ff; text-decoration:none; }}
 a:hover {{ text-decoration:underline; }}
</style>
<header>
  <h1>i3pro 本地服务</h1>
  <p>选择一个试车场次开始分析；所有数据都在本机解析，不上传。</p>
</header>
<main>
{_IMPORT_BLOCK}
<table>
 <tr><th>场次</th><th>设备</th><th>日期</th><th>时长</th><th>通道</th><th>完整圈</th><th>最快圈</th></tr>
 {''.join(rows) or '<tr><td colspan="7">没有找到 .ld 文件</td></tr>'}
</table>
</main>
"""


def bind(host: str, port: int, handler, attempts: int = 10) -> ThreadingHTTPServer:
    """Bind the first free port in ``[port, port + attempts)``.

    The launcher always asks for 8731; if a previous workbench is still running
    (or something else grabbed the port) we quietly move to the next one instead
    of dying with a bind error the user has to decode.
    """
    last: OSError | None = None
    for candidate in range(port, port + max(1, attempts)):
        try:
            return ThreadingHTTPServer((host, candidate), handler)
        except OSError as exc:
            last = exc
    raise OSError(
        f"端口 {port}-{port + attempts - 1} 都被占用，用 --port {port + attempts} 换一个"
        f"（最后一次错误: {last}）"
    )


def serve(
    roots: list[str | Path],
    host: str = "127.0.0.1",
    port: int = 8731,
    buckets: int = render.DEFAULT_BUCKETS,
    cache_size: int = 3,
    open_browser: bool = False,
    ready: threading.Event | None = None,
    port_attempts: int = 10,
    maths_root: str | Path | None = None,
) -> None:
    """Run the workbench server until Ctrl-C."""
    library = SessionLibrary(roots, cache_size=cache_size, maths_root=maths_root)
    handler = make_handler(library, buckets)
    try:
        httpd = bind(host, port, handler, port_attempts)
    except OSError:
        library.close()
        raise
    actual_port = httpd.server_address[1]
    local = f"http://127.0.0.1:{actual_port}/"
    print(f"i3pro 本地服务已启动: {local}")
    if host in ("0.0.0.0", "::"):
        addresses = _lan_addresses()
        if addresses:
            print(f"  发给队友(局域网): http://{addresses[0]}:{actual_port}/")
            for address in addresses[1:]:
                print(f"  其他网卡(多半是虚拟网卡): http://{address}:{actual_port}/")
        else:
            print("  局域网: 没找到网卡地址，用 ipconfig 查一下本机 IP")
        print("  ⚠ 第一次运行 Windows 防火墙可能弹窗，选“允许访问”。")
    else:
        print("  只监听本机；要发给队友用 --host 0.0.0.0")
    names = library.names()
    print(f"  {len(names)} 个场次, 数据目录: {', '.join(str(r) for r in roots)}")
    if not names:
        print("  ⚠ 这些目录里没有 .ld 文件，用 --data <目录> 指定试车数据所在位置")
    print("  Ctrl-C 停止")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(local)).start()
    if ready is not None:
        ready.set()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止中...")
    finally:
        httpd.server_close()
        library.close()
