# 电池能量通道：Drive Energy / Regen Energy / Net Energy

这三条全局数学通道把电池功率累计成 kWh：

| 通道 | 含义 | 口径 |
| --- | --- | --- |
| `Drive Energy` | 驱动电耗 | 电池放电能量，正数 |
| `Regen Energy` | 回收能量 | 回充到电池的能量，正数 |
| `Net Energy` | 总耗电量 | `Drive Energy - Regen Energy` |

三条通道都是**累计曲线**。按圈电耗用现有「通道报告」的**变化量**列读，或者用
光标 A–B 区间统计；没有新增按圈组件。

## 来源与符号

`battery_power()` 按下面的优先级返回主时间基上的电池功率（kW）：

1. `Battery Power`：至少有一个 `|P| > 0.1 kW` 的有效样本时直接使用。
   这条通道的约定是**放电为正、回充为负**。
2. `I Data` + `U1 Data`：`Battery Power` 不可用时用
   `P = -I × U / 1000`。`I Data` 的约定是**放电为负、回充为正**，所以公式带负号。
   `U1 Data <= 100 V`、非有限值或 `I Data` 缺失的样本按 `0 kW` 计；这个电压门限
   与 [`bus-current.md`](bus-current.md) 的拟合口径一致，用来排除预充 / 断电数据。
3. `Battery Power` 存在但整段为 0、且没有可用 IVT 时，返回全 0 功率，让高避
   金标准仍能挂上 0 kWh 通道。
4. 两套来源都没有时，`battery_power()` 报中文 `MathError`，不猜、不静默给 0。

非有限功率样本统一按 `0 kW` 处理，避免一个 NaN 把整条累计曲线污染成 NaN。

## 公式

```
Drive Energy = integrate(max(battery_power(), 0) / 3600)
Regen Energy = integrate(max(-battery_power(), 0) / 3600)
Net Energy   = 'Drive Energy' - 'Regen Energy'
```

`integrate` 是现有数学引擎的梯形累积积分；`/3600` 把 kW·s 换成 kWh。

## 实测（2026-10-19）

命令：

```powershell
python -m unittest tests.test_i3pro.TestEnergy -v
```

| 场次 | Drive Energy | Regen Energy | Net Energy | 来源 |
| --- | ---: | ---: | ---: | --- |
| `20261018-耐久合并.csv` | 6.351 | 1.025 | 5.326 | Battery Power |
| 同上，IVT 对拍 | 6.355 | 1.023 | 5.332 | `-I Data×U1 Data` |
| `20261007-jhy耐久.csv` | 1.454 | 0.305 | 1.149 | Battery Power |
| `20261007-庞仙耐久两圈.csv` | 1.102 | 0.163 | 0.940 | Battery Power |
| `20260524-耐久正赛.ld` | 4.612 | 0.000 | 4.612 | Battery Power |
| `FSS_jhy_endu.ld` | 2.174 | 0.478 | 1.696 | Battery Power |
| `20260908-cjh 高避5圈.ld` | 0.000 | 0.000 | 0.000 | Battery Power 全 0 |

容差 `±0.02 kWh`。10.18 的两套来源相差约 `0.006 kWh`，说明 IVT 回退与
`Battery Power` 在整场能量上是一致的。

## 边界

* 快照与服务模式共用同一套全局数学定义；导出的快照也会带上这三条通道。
* `Battery Power` 是记录仪 / IVT 的整车功率，不是四条电机功率之和；换车或换
  逆变器后，来源和符号约定要重新核对。
* 缺 `Battery Power` 又缺 `I Data` / `U1 Data` 的场次不会生成这三条通道；
  数学面板会报出原因，不会给一条假的 0 kWh 曲线。
