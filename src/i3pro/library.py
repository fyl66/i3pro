"""场次库：在几个根目录下找 ``.ld`` / ``.csv``，解析结果做 LRU 缓存。

从 ``server.py`` 里分出来的（ticket #23）：以前"HTTP 怎么回"和"场次从哪来"写在
同一个文件里，测试想拿一个场次库就得先起一个 socket。这个模块**不 import server**，
也不 import api——两边都 import 它。
"""

from __future__ import annotations

import json
import math
import threading
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote

import numpy as np

from . import beacons as beaconsmod, canlog, csvlog, maths, render, sidecar
from . import ld as ldmod

__all__ = ["SessionLibrary", "dumps", "json_safe"]


def json_safe(value):
    """Turn numpy scalars/arrays into JSON, mapping NaN/Inf to ``null``."""
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, np.ndarray):
        return json_safe(value.tolist())
    if isinstance(value, (np.floating, float)):
        return None if not math.isfinite(float(value)) else float(value)
    # 布尔要排在整数前面：Python 里 ``isinstance(True, int)`` 是真的，顺序反了
    # 就会把 ``{"ok": true}`` 写成 ``{"ok": 1}``，界面拿到的"真/假"变成数字。
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    return value


def dumps(value) -> str:
    return json.dumps(json_safe(value), ensure_ascii=False, allow_nan=False)


def _file_stamp(path: str | Path) -> tuple:
    """``(存在?, mtime_ns, size)``：判断一份侧车文件改没改过的便宜办法。"""
    try:
        stat = Path(path).stat()
    except OSError:
        return (False, 0, 0)
    return (True, stat.st_mtime_ns, stat.st_size)


class SessionLibrary:
    """Discover ``.ld`` files under one or more roots and cache parsed logs."""

    def __init__(
        self,
        roots: list[str | Path],
        cache_size: int = 3,
        maths_root: str | Path | None = None,
        worksheets_root: str | Path | None = None,
    ):
        self.roots = [Path(r) for r in roots]
        self.cache_size = max(1, cache_size)
        #: 全局数学定义（``<仓库>/maths/global.json``）的根目录。
        self.maths_root = maths_root
        #: 工作表（``<仓库>/worksheets/*.json``）的根目录（ticket #30）。
        self.worksheets_root = worksheets_root
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, ldmod.LogFile] = OrderedDict()
        self._maths_cache = maths.DerivedCache()
        #: 场次文件 -> (会话对象身份, 定义指纹)。定义或数据一改就重算。
        self._maths_attached: dict[str, tuple] = {}
        self._maths_errors: dict[str, list[dict]] = {}
        #: 并把连续记录并成一场（ticket #39）时算出来的分组，按文件指纹缓存：
        #: ``_paths`` 每个请求都会走一遍，重新读 9 份文件的首尾行没有必要。
        self._group_cache: tuple | None = None
        #: 场次列表（带圈数/最快圈）。它要**解码**每个场次，CAN 场次更贵，
        #: 所以按"文件 + 信标侧车的指纹"缓存，没变就直接给上一次的结果。
        self._listing_cache: tuple | None = None
        #: 场次文件 -> 上一次信标编辑之前的那一版配置，供"撤销上一步"用。
        #: 按 ticket #6 的约定**只留一版**（一个槽），而且只在内存里：服务一重启
        #: 就没了，撤的是"这个进程里刚才那一步"，不是历史。
        self._laps_undo: dict[str, beaconsmod.LapConfig] = {}

    # ------------------------------------------------------------- discovery
    def _groups(self, frames: list[Path]) -> list[dict]:
        stamp = tuple(
            (str(path),) + _file_stamp(path) for path in frames
        )
        with self._lock:
            if self._group_cache is not None and self._group_cache[0] == stamp:
                return self._group_cache[1]
        groups = canlog.group_recordings(frames) if frames else []
        with self._lock:
            self._group_cache = (stamp, groups)
        return groups

    def _paths(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        frames: list[Path] = []
        for root in self.roots:
            if not root.exists():
                continue
            for pattern in ("*.ld", "*.csv"):
                for path in sorted(root.rglob(pattern)):
                    if path.suffix.lower() == ".csv" and canlog.looks_like_frames(path):
                        frames.append(path)
                        continue
                    name = path.stem
                    if name in found:
                        # Two sources, one stem: keep both, so a CSV export of a
                        # session that also has its .ld is not silently hidden.
                        name = f"{name} ({path.suffix.lstrip('.').lower()})"
                    found.setdefault(name, path)
        # 记录器在恰好 1,000,000 帧处切文件：9 份其实是 7 次记录（实测），这里把
        # 同一次记录的后续文件并进第一份，侧边栏里就只有一场（ticket #39）。
        for group in self._groups(frames):
            items = [item["path"] for item in group["items"]]
            name = items[0].stem + (f"+{len(items) - 1}" if len(items) > 1 else "")
            if name in found:                      # 同名 .ld 在场时两场都要看得见
                name = f"{name} (can)"
            found.setdefault(name, items[0])
        return found

    def names(self) -> list[str]:
        return list(self._paths())

    def path_of(self, name: str) -> Path | None:
        return self._paths().get(name)

    # ------------------------------------------------------------------- load
    def get(self, name: str) -> ldmod.LogFile:
        path = self.path_of(name)
        if path is None:
            raise KeyError(name)
        key = str(path)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        # CAN 场次要一份 DBC 才能解码；DBC 放在各个数据根的 dbc/ 里，挨着场次的
        # 那个目录最优先（原始帧日志在 can_data/，DBC 在 i2pro_data/dbc/）。
        log = csvlog.open_session(path, dbc_dir=[root / "dbc" for root in self.roots])
        with self._lock:
            self._cache[key] = log
            while len(self._cache) > self.cache_size:
                _old_key, old = self._cache.popitem(last=False)
                old.close()
        self.apply_maths(path, log)
        return log

    # ------------------------------------------------------------------ maths
    def apply_maths(self, path: str | Path, log) -> None:
        """把这个场次生效的数学通道算出来挂上去；没变就不重算。"""
        path = Path(path)
        key = str(path)
        stamp = (
            id(log),
            _file_stamp(maths.config_path(path)),
            _file_stamp(maths.global_path(self.maths_root)),
        )
        if self._maths_attached.get(key) == stamp:
            return
        # 定义一变，旧的缓存列就都不作数了（包括引用关系变了的那种）
        self._maths_cache.clear()
        # 定义文件本身坏了也不影响看数据：apply_to_session 把原因当成一条报错返回
        _added, errors = maths.apply_to_session(log, self.maths_root, self._maths_cache)
        self._maths_errors[key] = errors
        self._maths_attached[key] = stamp

    def maths_state(self, name: str) -> dict:
        """界面要的全部数学状态：生效的定义、谁盖住了谁、哪些算不出来。"""
        path = self.path_of(name)
        if path is None:
            raise KeyError(name)
        effective = maths.load_effective(path, self.maths_root)
        return {
            "definitions": [d.as_dict() | {"scope": d.scope} for d in effective.definitions],
            "shadowed": list(effective.shadowed),
            "errors": list(self._maths_errors.get(str(path), [])),
            "local_path": maths.config_path(path).name,
            "global_path": str(maths.global_path(self.maths_root)),
            "functions": maths.function_catalogue(),
        }

    def maths_names(self, log) -> tuple[str, ...]:
        """这个场次现在生效的数学通道名（本地 + 全局），坏定义就当没有。

        保存时要判断"用户写的名字算不算存在"：本地定义引用全局定义完全合法，
        所以不能只看正在提交的那一份。
        """
        try:
            effective = maths.load_effective(log.path, self.maths_root)
        except maths.MathError:
            return ()
        return tuple(definition.name for definition in effective.definitions)

    def summary(self, name: str) -> dict:
        log = self.get(name)
        laps = render.detect(log)
        complete = [l for l in laps if l.complete]
        best = min((l.lap_time for l in complete), default=None)
        meta = log.metadata()
        meta["laps"] = len(laps)
        meta["complete_laps"] = len(complete)
        meta["best_lap"] = None if best is None else round(best, 3)
        meta["name"] = name
        meta["url"] = f"/session/{quote(name)}"
        return _json_safe(meta)

    def listing(self) -> list[dict]:
        paths = self._paths()
        stamp = tuple(
            (name, str(path)) + _file_stamp(path)
            + _file_stamp(sidecar.path_of("laps", path))
            for name, path in sorted(paths.items())
        )
        with self._lock:
            if self._listing_cache is not None and self._listing_cache[0] == stamp:
                return self._listing_cache[1]
        out: list[dict] = []
        for name in paths:
            try:
                out.append(self.summary(name))
            except Exception as exc:  # a broken file must not kill the index
                out.append({"name": name, "error": str(exc)})
        with self._lock:
            self._listing_cache = (stamp, out)
        return out

    # -------------------------------------------------------------- lap edits
    def remember_laps(self, path: str | Path, config: beaconsmod.LapConfig) -> None:
        """记下这次编辑**之前**的那一版，撤销时把它原样交回去。"""
        with self._lock:
            self._laps_undo[str(path)] = config

    def laps_undo_slot(self, path: str | Path) -> beaconsmod.LapConfig | None:
        with self._lock:
            return self._laps_undo.get(str(path))

    def forget_laps_undo(self, path: str | Path) -> None:
        """一级撤销：用掉就清空这个槽，不做重做。"""
        with self._lock:
            self._laps_undo.pop(str(path), None)

    def close(self) -> None:
        with self._lock:
            for log in self._cache.values():
                log.close()
            self._cache.clear()
            self._laps_undo.clear()

    def upload_dir(self) -> Path:
        """Where an uploaded log goes: the first configured data root."""
        root = self.roots[0] if self.roots else Path("i2pro_data")
        root.mkdir(parents=True, exist_ok=True)
        return root


# 老名字：过去从 server 里 import ``_json_safe`` 的地方（含文档）不用改。
_json_safe = json_safe
