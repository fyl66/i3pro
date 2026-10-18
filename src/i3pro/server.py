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

import gzip
import hashlib
import select
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import library as librarymod
from . import render
from .api import Api, Body, ClientGone, Response, csv_arg, float_arg, int_arg
from .indexpage import index_page, unknown_session_page
from .library import SessionLibrary, dumps, json_safe

#: 到这个大小才值得压：几 KB 的响应压完可能更大，还要白付一次 CPU。
GZIP_MIN_BYTES = 64 * 1024

#: 压缩级别。**1 不是 6**：实测高避金标准 8000 桶那份 3.6 MB 载荷，
#: 级别 1 用 29 ms 压到 830 KB（4.3×），级别 6 要用 140 ms 才压到 694 KB（5.2×）
#: ——多花 110 ms 换 20% 的体积，在局域网里不值。这是给人用的交互工具，
#: 迟到的曲线比多传几百 KB 更烦。
GZIP_LEVEL = 1


def _etag(body: bytes) -> str:
    """响应体的强校验码。blake2b 在 Python 里够快：3.6 MB 实测 6 ms。"""
    return '"' + hashlib.blake2b(body, digest_size=16).hexdigest() + '"'


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

        def do_DELETE(self):  # noqa: N802 - http.server API
            """取消导入预览时用（ticket #32）：暂存文件得被主动删掉。"""
            self.dispatch("DELETE")

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
            # 注意用**模块**（``librarymod``）：``make_handler`` 的形参 ``library``
            # 是那个场次库实例，`library.UnknownSession` 会 AttributeError。
            except librarymod.UnknownSession as exc:
                # 页面地址里的场次名对不上（多半是收藏了旧链接，或者那份日志已经
                # 挪走/改名了）。以前这一支落到下面的兜底，回一个 500 加一句
                # ``KeyError: '名字'``——用户的下一步是"回去点一次列表"，不是看栈。
                response = Response(
                    404,
                    unknown_session_page(str(exc.args[0]), library.names()).encode("utf-8"),
                    "text/html; charset=utf-8",
                )
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

        def _json_transport(self, response: Response, headers: list):
            """JSON 响应的传输层：ETag + 条件请求 + gzip。

            只碰 JSON。两张 HTML 页面与导出那条流式文件一个字节都不动——它们各有各的
            缓存语义（``Cache-Control: no-store`` 那一条不能顺手改掉）。
            ``headers`` 是已经算好的响应头，**只能往上加，不能换掉**——工作表「导出」
            就靠 ``Content-Disposition`` 才变成下载而不是跳转（ticket #33，
            2026-10-18 真浏览器验收抓到的就是这个）。
            返回 ``(要发的字节, 头, 是不是 304)``。
            """
            body = response.body
            tag = _etag(body)
            headers = list(headers) + [("ETag", tag), ("Vary", "Accept-Encoding")]
            if (self.headers.get("If-None-Match") or "").strip() == tag:
                return b"", headers, True
            accepted = (self.headers.get("Accept-Encoding") or "").lower()
            if len(body) >= GZIP_MIN_BYTES and "gzip" in accepted:
                headers = headers + [("Content-Encoding", "gzip")]
                return gzip.compress(body, GZIP_LEVEL), headers, False
            return body, headers, False

        def send(self, response: Response) -> None:
            """把 ``Response`` 发出去。

            ``response.path`` 有值时**流式**发那个文件（导出走这条）：200 MB 的 CSV
            不整份进内存，``Content-Length`` 是文件长度，界面据此画真实进度；
            发完、出错、连接中断三种情况都在 ``finally`` 里删掉它，磁盘不留垃圾。
            """
            path = response.path
            body = response.body
            headers = list(response.headers)
            status = response.status
            cache_control = "no-store"
            # 只有 200 的 JSON 走这条路：报错体都很小，压了反而亏。
            if (path is None and response.status == 200
                    and response.ctype.startswith("application/json")):
                body, headers, not_modified = self._json_transport(response, headers)
                if not_modified:
                    status = 304
                # 有 ETag 才谈得上"存下来、回来问一句"。``no-cache`` 不是"别存"，
                # 是"每次都得先问"：数据一变字节就变、ETag 就变，304 只在真没变时出现。
                cache_control = "no-cache"
            try:
                size = path.stat().st_size if path is not None else len(body)
                self.send_response(status)
                self.send_header("Content-Type", response.ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", cache_control)
                for key, value in headers:
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
                if body:
                    self.wfile.write(body)
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
                # 页面顶上那排工作表按钮与 /api/worksheets 必须读**同一个目录**：
                # --worksheets 指到别处时（快照打包、真浏览器验收的副本），
                # 少了这一行就会"接口读 A、页面画 B"。
                worksheets_dir=library.worksheets_root,
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
    worksheets_root: str | Path | None = None,
) -> None:
    """Run the workbench server until Ctrl-C."""
    library = SessionLibrary(roots, cache_size=cache_size, maths_root=maths_root,
                             worksheets_root=worksheets_root)
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
