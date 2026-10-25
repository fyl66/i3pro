"""从一场日志里定出「单轮载荷」数学通道的锚点，并打印可直接粘贴的表达式。

为什么需要这个脚本
------------------
`i2pro_data/*.maths.json` 是**本地**作用域、不进 git，所以那几条 `X Wheel Load`
通道里的四个常数（`静止分配` 与`锚窗电压`）必须能被人重新算出来，否则下一个人
只能选择相信或重做。

公式（每角独立，单位 kg）：

    载荷_i = 静止分配_i + (锚窗电压_i − 电压_i) × 加权斜率_i

* `加权斜率_i`：四角秤（`cheliang\\senser.xls`）与 .ld 自由拟合按误差方差加权，
  FR 196.4 / FL 206.1 / RL 196.3 / RR 211.5 kg/V；
* `静止分配_i`：无驾驶员四角 FR/FL 42.5、RL/RR 54 kg（`E27108车辆参数.txt`），
  驾驶员质量按前后 0.571 分摊（前每角 +0.2145·m，后每角 +0.2855·m）；
* `锚窗电压_i`：本场**静止且载荷通道都已上线**的那一段里，该角的平均电压。

**门必须收紧**（这条踩过坑） 用 `Vx < 3` 当静止门是不够的：日志开头载荷通道
还没输出时读数是 0 V 或正值，那些窗口也算"静止"，会把锚点拉偏 50–130 kg。
所以这里要求 `|速度| < 3` **且四路载荷都 `|V| > 0.05`**。

用法
----
    python tools\\load_anchors.py "i2pro_data\\mxy动态练习八字.csv" --driver 60
    python tools\\load_anchors.py <场次> --driver 60 --write     # 追加进 .maths.json

没有 `--write` 时只打印，不改任何文件。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# Windows 控制台默认 GBK，中文输出会直接崩在打印上
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from i3pro import csvlog, ld  # noqa: E402

CORNERS = ("FR", "FL", "RL", "RR")
LOAD_CHANNEL = {
    "FR": "Right Front Suspension Load",
    "FL": "Left Front Suspension Load",
    "RL": "Left Rear Suspension Load",
    "RR": "Right Rear Suspension Load",
}
SPEED_CHANNEL = "Vx KF"

# 加权斜率 [kg/V]（来源见模块 docstring）
SLOPE_KG_PER_V = {"FR": 196.4, "FL": 206.1, "RL": 196.3, "RR": 211.5}
# 无驾驶员四角静态载荷 [kg]
NO_DRIVER_KG = {"FR": 42.5, "FL": 42.5, "RL": 54.0, "RR": 54.0}
# 驾驶员质量落在后轴的份额（v4 由停车段反推）
SHARE_REAR = 0.571
# 静止判定：速度上限 [km/h] 与通道存活门限 [V]
STOP_SPEED = 3.0
LIVE_THRESHOLD = 0.05
MIN_WINDOW_S = 8.0


def static_share(driver_kg: float) -> dict:
    """车手就座时的静止四角载荷 [kg]。"""
    front = NO_DRIVER_KG["FR"] + driver_kg * (1 - SHARE_REAR) / 2
    rear = NO_DRIVER_KG["RL"] + driver_kg * SHARE_REAR / 2
    return {"FR": front, "FL": front, "RL": rear, "RR": rear}


def read_series(session, name: str) -> np.ndarray | None:
    """取一条通道的原生序列；没有就返回 None。"""
    checker = getattr(session, "has", None)
    if callable(checker):
        if not checker(name):
            return None
    else:
        try:
            session.channel(name)
        except (KeyError, LookupError):
            return None
    return np.asarray(session.values(name), dtype=float)


def open_session(path: Path):
    """按后缀挑读取器：CSV 走 csvlog，.ld 只把用得到的几条通道读进内存。

    `.ld` 不能像 CSV 那样返回一个常驻会话对象——`with ld.LogFile.read(...)` 一退出
    文件就关了，所以这里读完立刻关，只留下这一轮需要的五条序列。
    """
    if path.suffix.lower() == ".csv":
        return csvlog.open_session(str(path))
    wanted = (SPEED_CHANNEL,) + tuple(LOAD_CHANNEL[c] for c in CORNERS)
    with ld.LogFile.read(path) as log:
        names = {c.name for c in log.channels}
        missing = [n for n in wanted if n not in names]
        if missing:
            raise SystemExit("这个 .ld 缺少通道：%s。下一步：换一场，"
                             "或者确认这场有没有四路悬架载荷。" % "、".join(missing))
        data = {n: np.asarray(log.values(n), dtype=float) for n in wanted}
    return _LoadedSession(data)


class _LoadedSession:
    """只带 `has` / `values` 两个方法的最小会话壳，给 `read_series` 用。"""

    def __init__(self, data: dict):
        self._data = data

    def has(self, name: str) -> bool:
        return name in self._data

    def values(self, name: str) -> np.ndarray:
        return self._data[name]


def stationary_window(session, rate: float,
                      require: tuple[str, ...] = CORNERS) -> tuple[int, int] | None:
    """找最长的「静止 + require 里的载荷通道都活着」窗口；找不到返回 None。

    `require` 默认四角；某一路硬件坏了（例如 `80定速滑行` 的 FL 恒轨到 +6.55 V）
    时用 `--require FR,RL,RR` 退到三路，否则永远找不到窗口。
    """
    speed = read_series(session, SPEED_CHANNEL)
    if speed is None:
        return None
    loads = [read_series(session, LOAD_CHANNEL[c]) for c in require]
    if any(v is None for v in loads):
        return None
    size = min([speed.size] + [v.size for v in loads])
    ok = np.isfinite(speed[:size]) & (np.abs(speed[:size]) < STOP_SPEED)
    for values in loads:
        ok &= np.abs(values[:size]) > LIVE_THRESHOLD
    need = int(MIN_WINDOW_S * rate)
    best, start = None, None
    for index in range(size):
        if ok[index]:
            if start is None:
                start = index
            continue
        if start is not None and index - start >= need:
            if best is None or index - start > best[1] - best[0]:
                best = (start, index)
        start = None
    if start is not None and size - start >= need:
        if best is None or size - start > best[1] - best[0]:
            best = (start, size)
    return best


def anchors(session, driver_kg: float, rate: float,
            require: tuple[str, ...] = CORNERS) -> dict:
    """算出四角锚点；返回 {corner: {v0, share, expr, ok, reason}}。"""
    window = stationary_window(session, rate, require)
    if window is None:
        return {c: {"ok": False,
                    "reason": "本场找不到「静止且 %s 都活着」的窗口——"
                              "先确认载荷通道有没有坏（见 ACCEPTANCE A121）"
                              % "/".join(require)}
                for c in CORNERS}
    start, end = window
    share = static_share(driver_kg)
    out = {}
    for corner in CORNERS:
        values = read_series(session, LOAD_CHANNEL[corner])
        chunk = values[start:end]
        live = float(np.mean(np.abs(chunk) > LIVE_THRESHOLD))
        if live <= 0.95:
            out[corner] = {"ok": False,
                           "reason": "该角在最长窗口里只有 %.0f%% 的样本有效" % (live * 100)}
            continue
        v0 = float(np.nanmean(chunk))
        out[corner] = {
            "ok": True, "v0": v0, "share": share[corner],
            "expr": "%g + (%+.5f - '%s') * %g"
                    % (share[corner], v0, LOAD_CHANNEL[corner], SLOPE_KG_PER_V[corner]),
            "window": (start / rate, end / rate),
        }
    return out


def driver_of(name: str) -> float:
    """从文件名猜车手代号对应的质量；猜不到就返回 60 kg 并提示。"""
    table = {"cjh": 49.0, "jhy": 60.0, "mxy": 60.0}
    low = name.lower()
    for key, mass in table.items():
        if key in low:
            return mass
    return 60.0


def main() -> int:
    parser = argparse.ArgumentParser(description="算单轮载荷数学通道的锚点")
    parser.add_argument("session", help="场次文件（.csv 或 .ld）")
    parser.add_argument("--driver", type=float, default=None, help="车手质量 kg（省略则按文件名猜）")
    parser.add_argument("--require", default="FR,FL,RL,RR",
                        help="锚窗必须活着的角，逗号分隔；某路硬件坏了就把它去掉，"
                             "例如 --require FR,RL,RR")
    parser.add_argument("--write", action="store_true", help="把四条通道追加进 <场次>.maths.json")
    args = parser.parse_args()

    require = tuple(c.strip().upper() for c in args.require.split(",") if c.strip())
    unknown = [c for c in require if c not in CORNERS]
    if unknown:
        print("--require 里有不认识的角：%s（只能是 %s）" % (unknown, "/".join(CORNERS)))
        return 2

    path = Path(args.session)
    if not path.is_absolute():
        path = REPO / path
    session = open_session(path)
    rate = float(getattr(session, "sample_rate", None) or 100.0)
    mass = args.driver if args.driver is not None else driver_of(path.stem)
    print("场次 %s   车手 %.1f kg   采样率 %.1f Hz" % (path.name, mass, rate))

    result = anchors(session, mass, rate, require)
    usable = [c for c in CORNERS if result[c].get("ok")]
    if not usable:
        for corner in CORNERS:
            print("  %-3s 不可用：%s" % (corner, result[corner]["reason"]))
        return 1

    window = result[usable[0]]["window"]
    print("锚窗 t=%.0f-%.0f s（%.0f s）" % (window[0], window[1], window[1] - window[0]))
    for corner in CORNERS:
        row = result[corner]
        if row.get("ok"):
            print("  %-3s 锚点 %+.5f V  静止分配 %5.1f kg  |  %s"
                  % (corner, row["v0"], row["share"], row["expr"]))
        else:
            print("  %-3s 不可用：%s" % (corner, row["reason"]))

    if not args.write:
        print("\n（没加 --write，一个字都没改）")
        return 0

    sidecar = path.with_suffix(".maths.json")
    doc = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() \
        else {"definitions": []}
    have = {d["name"] for d in doc["definitions"]}
    for corner in CORNERS:
        row = result[corner]
        name = "%s Wheel Load" % corner
        if not row.get("ok") or name in have:
            continue
        doc["definitions"].append({
            "name": name, "expr": row["expr"], "unit": "kg",
            "note": "锚定静止窗 t=%.0f-%.0f s；车手 %.1f kg。"
                    "由 tools/load_anchors.py 生成，见 docs/ACCEPTANCE.md A121"
                    % (row["window"][0], row["window"][1], mass),
        })
    sidecar.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
    print("\n写入 %s（共 %d 条定义）" % (sidecar.name, len(doc["definitions"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
