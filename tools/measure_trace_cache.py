"""量具：真滚轮"放大 N 格 → 原路缩回 N 格"，数 ``/trace`` 响应缓存的命中率（ticket #68）。

**判据只有一条**：返回那一趟的取数必须**全部命中**。

为什么要单独一个量具，而不是塞进 ``tools\\verify_clicks.py``：那条盘验的是"点得到、
画得对"，判据是像素与状态；这一条验的是**服务端那个响应缓存有没有被用上**，得看见每一次
``TraceCache.get`` 的 key。key 只有服务端自己知道，所以服务跑在**本进程**里（不是子
进程），给它打桩记下每一笔。浏览器那一层复用 ``verify_clicks.Browser``（真 Edge + CDP），
滚轮是**真事件**——那个浮点往返只在真滚轮上才发生。

**判据的上限**（别把它读成"整体命中率 100%"）：缩放**进入没看过的位置**，那个窗口天然
是新的、必然冷。所以这张盘钉的是"**返回**"，不是"第一次经过"。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from i3pro import library as librarymod, server, tracecache  # noqa: E402
import verify_clicks as vc  # noqa: E402  # 真 Edge + CDP 那一层只有一处实现

#: 每一次取数：``(命中了吗, 服务端那个 key)``。
HITS: list[tuple[bool, str]] = []

#: 滚轮一格。与 ``viewer.html`` 里那个 ``1.25`` 是一对：负数是放大。
WHEEL_STEP = 120


def instrument() -> None:
    """给 ``TraceCache.get`` 打桩。

    ``server.make_handler`` 里那个 ``TraceCache()`` 是**每个 handler 一个**，打类上的
    方法才盖得住它。
    """
    original = tracecache.TraceCache.get

    def patched(self, key):
        got = original(self, key)
        HITS.append((got is not None, key))
        return got

    tracecache.TraceCache.get = patched


def window_of(key: str) -> str:
    """key 是 ``<场次>\\x00<规范化查询>\\x00<数据指纹>``；第二段里就是窗口。

    打日志时把 ``channels=`` 那段缩掉——它一屏放不下，而这条量具只关心窗口。
    """
    parts = key.split("\x00")
    if len(parts) < 2:
        return "?"
    query = dict(urllib.parse.parse_qsl(parts[1]))
    return "from=%s to=%s" % (query.get("from", "?"), query.get("to", "?"))


def start_server(work_dir: Path, worksheets_root: Path):
    """服务跑在**本进程**里：子进程里的 ``TraceCache`` 打不到桩。"""
    library = librarymod.SessionLibrary(
        [work_dir], cache_size=2, maths_root=ROOT, worksheets_root=worksheets_root,
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(library))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return library, httpd, httpd.server_address[1]


def graph_point(browser, page):
    """找一个**真的落在图画布上**的点。滚轮落上去才走 ``zoomFactor``。

    不能图省事取"第一张 canvas 的中心"：工作台里那些下拉框是**浮在画布上面**的，
    实测取到的那个中心点 ``elementFromPoint`` 给的是 ``SELECT``——事件照样冒泡到
    ``#charts``，但 ``event.target.closest("canvas")`` 是 null，滚轮处理函数当场返回，
    视图一动不动。第一版就是这么量出一批假数的。
    所以这里只在 ``elementFromPoint(x, y) === 那张画布`` 的点里挑。
    """
    return browser.js(
        "(function(){"
        "var cs=document.querySelectorAll('#charts canvas'),"
        "skip={'overview':1,'scrollCanvas':1,'track':1};"
        "for(var i=0;i<cs.length;i++){var c=cs[i];"
        "if(skip[c.id])continue;"
        "var r=c.getBoundingClientRect();"
        "if(r.width<200||r.height<100)continue;"
        "var spots=[[0.5,0.5],[0.35,0.62],[0.65,0.38],[0.5,0.78],[0.25,0.42],[0.75,0.6]];"
        "for(var k=0;k<spots.length;k++){"
        "var x=Math.round(r.left+r.width*spots[k][0]),"
        "y=Math.round(r.top+r.height*spots[k][1]);"
        "if(document.elementFromPoint(x,y)===c)return [x,y];}}"
        "return null;})()",
        page,
    )


def wheel(browser, page, x, y, delta):
    """一格真滚轮，然后等页面安静（``settle`` 是 ticket #67 那个）。"""
    browser.call("Input.dispatchMouseEvent",
                 {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": delta},
                 session=page)
    browser.settle(page)


def warm_up(browser, page, timeout=60.0):
    """先把视图放到第一圈上，并**等这一圈的取数真的落地**再开始量。

    不等的话，滚轮那一阶段可能一次取数都不触发：取数键里的窗口没变过，组件手里那份
    数据还算数，于是整条量具空转、给出一个假的"0 次请求"。

    起点走 ``zoomTo`` 而**不是**直写 ``state.view``：直写出来的是没吸附过的窗口，而
    返回趟落回来的是吸附过的那个——差一格，于是最后一步永远"没命中"，看着像产品没修
    好，其实是量具自己造了一个应用里不存在的起点（``viewer.html`` 里那条结构守卫现在
    也不许再出现这种直写）。
    """
    lap = browser.js(
        "(function(){var l=(i3pro.data.laps||[]).filter(function(x){return x.complete;});"
        "return l.length?[l[0].start_time,l[0].end_time]:null;})()",
        page,
    )
    if not lap:
        return False
    before = len(HITS)
    browser.js("i3pro.zoomTo(%s,%s);" % (lap[0], lap[1]), page)
    browser.js("i3pro.refreshWindowTraces(true);i3pro.renderAll();", page)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(HITS) > before:
            return True
        time.sleep(0.1)
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="数 /trace 响应缓存在「返回那一趟」的命中率")
    parser.add_argument("--session", default="20260908-cjh 高避5圈")
    parser.add_argument("--steps", type=int, default=6, help="放大几格（回来也是几格）")
    parser.add_argument("--port", type=int, default=9337, help="Edge 的调试端口")
    parser.add_argument("--edge", default=None)
    args = parser.parse_args(argv)

    instrument()
    work = ROOT / "out" / "_trace_cache_rig"
    if not vc.stage_session(args.session, str(work)):
        print("找不到金标准场次：%s（量具只认 i2pro_data 里那两份）" % args.session)
        return 2
    worksheets_root = Path(vc.stage_worksheets(str(work)))
    library, httpd, port = start_server(work, worksheets_root)

    edge = vc.find_edge(args.edge)
    if edge is None:
        print("SKIP 这台机器上没有 Edge——这条量具要真浏览器")
        library.close()
        httpd.shutdown()
        return 0

    browser = vc.Browser(edge, args.port, str(work / "_profile"))
    base = "http://127.0.0.1:%d" % port
    try:
        page = browser.open(base + "/session/" + urllib.parse.quote(args.session), wait=0.6)
        if not browser.wait_for("!!(window.i3pro && i3pro.data && i3pro.data.laps)", page,
                                timeout=60):
            print("工作台没起来（等了 60 s）")
            return 2
        if not warm_up(browser, page):
            print("预热没等到第一笔取数——量具会空转，先别信它的数")
            return 2
        point = graph_point(browser, page)
        if not point:
            print("页面上找不到图表画布")
            return 2
        x, y = point

        # 第一次经过：每换一个窗口都该是冷的（没看过）。
        mark = len(HITS)
        for _ in range(args.steps):
            wheel(browser, page, x, y, -WHEEL_STEP)
        outbound = HITS[mark:]

        # 原路缩回：这些窗口**刚才都看过**，每一笔都该命中。
        mark = len(HITS)
        for _ in range(args.steps):
            wheel(browser, page, x, y, WHEEL_STEP)
        back = HITS[mark:]
    finally:
        browser.close()
        library.close()
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(work / "_profile", ignore_errors=True)

    print("\n第一次经过（%d 格）：%d 次取数，%d 命中"
          % (args.steps, len(outbound), sum(1 for hit, _ in outbound if hit)))
    for hit, key in outbound:
        print("   %s  %s" % ("hit " if hit else "miss", window_of(key)))
    print("\n原路缩回（%d 格）：%d 次取数，%d 命中"
          % (args.steps, len(back), sum(1 for hit, _ in back if hit)))
    for hit, key in back:
        print("   %s  %s" % ("hit " if hit else "miss", window_of(key)))

    if not back:
        print("\nFAIL - 返回那一趟一次取数都没触发，量具空转了（先看上面的预热）")
        return 2
    cold = [key for hit, key in back if not hit]
    if cold:
        print("\nFAIL - 返回那一趟有 %d 笔没命中（窗口没能复现，key 就变了）" % len(cold))
        return 1
    print("\nPASS - 返回那一趟 %d 次取数全部命中" % len(back))
    return 0


if __name__ == "__main__":
    sys.exit(main())
