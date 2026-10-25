"""母线电流与四条电机电流的关系（`docs/bus-current.md` 里的每个数字都由它跑出来）。

    python tools\\bus_current.py
    python tools\\bus_current.py i2pro_data        # 换数据目录

前提：`i3pro pair` 已经把 CAN 记录仪那份（IVT 的 `I Data` / `U1 Data`）与 `.ld` 那份
（四条电机的 `TorqueCurrent` / `ActualVelocity` / `ActualTorqueValue`）合成到同一个场次。
这个脚本只做三件事：量 kt、拟合两条公式、报每个系数与残差。**不写任何文件。**

只报实测值：换一批数据跑出来的系数会不一样，这是数据的事实，不是脚本的毛病。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from i3pro import containers, csvlog  # noqa: E402

WHEELS = ("FR", "FL", "RL", "RR")
IDX = {"FR": 1, "FL": 2, "RL": 3, "RR": 4}
RPM2RAD = 2.0 * np.pi / 60.0
#: 母线电压低于这个值 = 没上电 / 采样缺失，不参与拟合（实测有整段为 0 的格子）。
U_MIN = 100.0


def _sessions(data: Path) -> list[dict]:
    out = []
    # 合并产物现在可能是压过的 `.csv.gz`（ADR-0012）——只扫 `*.csv` 的话，这份数据上
    # 会**静默**一个场次都找不到（脚本本来就是拿来跑这些合并场次的）。
    paths = [*data.glob("*.csv"), *data.glob("*.csv.gz")]
    for path in sorted(p for p in paths
                       if not p.name[:4].isdigit() or "_" not in p.name):
        session = csvlog.read_csv_session(path)
        names = {c.name for c in session.channels}
        need = (["I Data", "U1 Data"]
                + [f"AMK{w} ActualTorqueValue" for w in WHEELS]
                + [f"MCU{IDX[w]} {w} TorqueCurrent" for w in WHEELS]
                + [f"MCU{IDX[w]} {w} ActualVelocity" for w in WHEELS])
        if not all(name in names for name in need):
            continue

        def get(name):
            return np.asarray(session.values(name), dtype=np.float64)

        bus, volt = get("I Data"), get("U1 Data")
        current = {w: get(f"MCU{IDX[w]} {w} TorqueCurrent") for w in WHEELS}
        rpm = {w: get(f"MCU{IDX[w]} {w} ActualVelocity") for w in WHEELS}
        torque = {w: get(f"AMK{w} ActualTorqueValue") for w in WHEELS}
        total = np.sum([current[w] for w in WHEELS], axis=0)
        shaft = np.sum([torque[w] * rpm[w] * RPM2RAD for w in WHEELS], axis=0)
        live = (np.isfinite(volt) & (volt > U_MIN) & np.isfinite(bus)
                & np.isfinite(total))
        # 母线电压整段为 0 的格子是"没上电/采样缺失"，先掩掉再除，别让 inf 混进拟合。
        with np.errstate(invalid="ignore", divide="ignore"):
            per_volt = np.where(live, shaft / volt, np.nan)
        out.append(dict(name=path.name, bus=bus, volt=volt, current=current,
                        rpm=rpm, torque=torque, total=total, shaft=shaft,
                        per_volt=per_volt, live=live))
    return out


def _fit(columns, target):
    """最小二乘 ``target ≈ Σ columns_i·x_i + b``，返回 (系数, R², RMS, n)。"""
    mask = np.isfinite(target)
    for column in columns:
        mask &= np.isfinite(column)
    x = np.column_stack([c[mask] for c in columns] + [np.ones(int(mask.sum()))])
    y = target[mask]
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    resid = y - x @ coef
    spread = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float((resid ** 2).sum()) / spread if spread > 0 else float("nan")
    return coef, r2, float(np.sqrt((resid ** 2).mean())), int(y.size)


def main() -> int:
    data = Path(sys.argv[1] if len(sys.argv) > 1 else "i2pro_data")
    rows = _sessions(data)
    if not rows:
        print(f"{data} 里没有『母线电流 + 四条电机』都齐的场次。下一步：先跑 "
              f"i3pro pair {data} --apply，或者换 --data 指对目录。")
        return 1
    print(f"{len(rows)} 场（母线电压 > {U_MIN:g} V 的样本参与拟合）\n")

    print("每个电机的 kt：T = kt·I_q（过原点，只取 |I_q| > 5 A 的样本）")
    print(f"   {'轮':4s} {'kt (Nm/A)':>10s} {'R²':>8s} {'样本':>12s}")
    kt = {}
    for wheel in WHEELS:
        x = np.concatenate([r["current"][wheel][r["live"]] for r in rows])
        y = np.concatenate([r["torque"][wheel][r["live"]] for r in rows])
        strong = np.abs(x) > 5.0
        x, y = x[strong], y[strong]
        value = float((x * y).sum() / (x * x).sum())
        resid = y - value * x
        r2 = 1.0 - float((resid ** 2).sum()) / float(((y - y.mean()) ** 2).sum())
        kt[wheel] = value
        print(f"   {wheel:4s} {value:10.4f} {r2:8.4f} {x.size:12,d}")
    mean_kt = float(np.mean(list(kt.values())))
    print(f"   平均 kt = {mean_kt:.4f} Nm/A\n")

    live = np.concatenate([r["live"] for r in rows])
    bus = np.concatenate([r["bus"] for r in rows])[live]
    volt = np.concatenate([r["volt"] for r in rows])[live]
    total = np.concatenate([r["total"] for r in rows])[live]
    shaft = np.concatenate([r["shaft"] for r in rows])[live]

    print("公式 A（只有电机电流）：I_bus = a·Σi_q + b")
    (a1, b1), r2, rms, n = _fit([total], bus)
    print(f"   a = {a1:+.4f}    b = {b1:+.3f} A    R² = {r2:.4f}"
          f"    RMS = {rms:.2f} A    n = {n:,}\n")

    per_volt = np.concatenate([r["per_volt"] for r in rows])[live]
    print("公式 B（电流 + 转速 + 母线电压）：I_bus = -(1/η)·Σ(T·ω)/U + b")
    (a2, b2), r2b, rmsb, nb = _fit([per_volt], bus)
    print(f"   a = {a2:+.4f} -> 1/η = {abs(a2):.3f}，η = {1.0 / abs(a2):.3f}")
    print(f"   b = {b2:+.3f} A    R² = {r2b:.4f}    RMS = {rmsb:.2f} A"
          f"    n = {nb:,}\n")
    print(f"   同一件事写成电流 × 转速：T = kt·i_q（kt = {mean_kt:.4f} Nm/A），")
    print(f"   所以系数是 -(kt/η) = {mean_kt * a2:+.4f}"
          f"（`I_bus = {mean_kt * a2:+.4f}·Σ(i_q·ω)/U + b`）\n")

    print("逐场（同一条公式 B）")
    print(f"   {'场次':38s} {'1/η':>8s} {'R²':>8s} {'RMS(A)':>8s}")
    for row in rows:
        if int(row["live"].sum()) < 200:
            continue
        (a, _b), r2c, rmsc, _n = _fit(
            [row["per_volt"][row["live"]]], row["bus"][row["live"]])
        print(f"   {row['name']:38s} {abs(a):8.3f} {r2c:8.4f} {rmsc:8.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
