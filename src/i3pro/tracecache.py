"""取数响应（``/trace``）的内存缓存：同一个窗口反复问，不再重算（ticket #54）。

为什么要它：同一个窗口会被反复问——缩放来回、两张图看同一段、几个人看同一场次。
实测一份 12 通道 8000 桶的取数要 **125 ms 计算 + 26 ms 压缩**，而字节一模一样。

三条纪律：

* **纯 key 驱动**：key 里带着"结果会不会变"的全部指纹（场次文件、本地/全局数学定义、
  圈与 GPS 侧车，见 :meth:`i3pro.library.SessionLibrary.data_stamp`）。没有主动失效
  钩子——那会写成第二处真相，忘一处就发陈数据。
* **只在内存**：上限 8 项 / 64 MB，超了淘汰最近最少用的；进程退出就没了，不落盘。
* **两种编码都存**：明文一份、gzip 一份。只存明文的话，命中时那 26 ms 的压缩又回来了，
  而这条缓存的意义正是"第二次几乎不花时间"。
"""

from __future__ import annotations

from collections import OrderedDict

from .api import Response

__all__ = ["MAX_BYTES", "MAX_ENTRIES", "TraceCache"]

#: 项数上限。8 项的账：一份 8000 桶 12 通道的明文 3.6 MB + gzip 0.8 MB ≈ 4.4 MB，
#: 满打满算 35 MB，离下面的字节上限还有余量——留出的是"几份高分辨率场次同时开着"。
MAX_ENTRIES = 8

#: 字节上限。到顶就按最近最少用往下淘汰，**不是**把缓存整个清空。
MAX_BYTES = 64 * 1024 * 1024


def _size(response: Response) -> int:
    return len(response.body) + len(response.gzip_body or b"")


class TraceCache:
    """按 key 存最近用过的取数响应；纯 Python，不依赖 http 那一层。"""

    def __init__(self, max_entries: int = MAX_ENTRIES, max_bytes: int = MAX_BYTES):
        self.max_entries = max(1, int(max_entries))
        self.max_bytes = max(1, int(max_bytes))
        self._items: OrderedDict[str, Response] = OrderedDict()
        self._bytes = 0

    # ------------------------------------------------------------------ 读
    def get(self, key: str) -> Response | None:
        """取一条并把它挪到"最近用过"那一头；没有就给 ``None``。"""
        response = self._items.get(key)
        if response is not None:
            self._items.move_to_end(key)
        return response

    # ------------------------------------------------------------------ 写
    def put(self, key: str, response: Response) -> None:
        """存一条。

        一条自己就超过字节上限的（比如用户把分辨率拉到最高又开了十几条通道）
        干脆**不存**——存进去会把别的一股脑挤掉，自己下次也还是超。
        """
        size = _size(response)
        if size > self.max_bytes:
            return
        old = self._items.pop(key, None)
        if old is not None:
            self._bytes -= _size(old)
        self._items[key] = response
        self._bytes += size
        while len(self._items) > self.max_entries or self._bytes > self.max_bytes:
            _dropped_key, dropped = self._items.popitem(last=False)
            self._bytes -= _size(dropped)

    def clear(self) -> None:
        self._items.clear()
        self._bytes = 0

    # ------------------------------------------------------------ 给测试看的
    def stats(self) -> dict:
        """``{"entries": n, "bytes": n, "keys": [...]}``——验收要量内存别只是"看着小"。"""
        return {"entries": len(self._items), "bytes": self._bytes,
                "keys": list(self._items)}
