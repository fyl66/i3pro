# `/trace` 的二进制载荷：JSON 留给快照，serve 走 TypedArray

ADR-0007 把性能瓶颈指到了 `json.dumps`：12 通道 × 8000 桶的冷路径约 **118.3 ms**，
其中约 **83.9 ms（七成）** 是把已经算好的 Python 列表再变成 JSON 文本。那份 ADR 的
翻案条件 1 写明：二进制载荷落地后，门槛 1 必须重新量过。这份 ADR 记录那次翻案。

## 决策

1. **现有 `/trace` 加 `format=bin`**，不改路由。`format` 进响应缓存 key，所以 JSON 与
   二进制各自缓存，不互相顶掉。
2. **二进制帧由 `src/i3pro/tracewire.py` 编解码**。帧头是 UTF-8 JSON 元数据，正文是
   连续数组：时间 `float64`，值 / min / max / 距离 `float32`，count `int32`；每个数组
   对齐到 8 字节，浏览器直接建 `Float64Array` / `Float32Array` / `Int32Array` 视图。
3. **JSON 仍是默认格式**。不传 `format` 的老请求、快照内嵌载荷、错误响应都保持原样；
   前端二进制解码失败时回退到 JSON。协议版本写在帧头，未来改形状要升版本。
4. **不引入运行期依赖**。编码只用标准库 `json` / `struct` 与已有的 numpy；浏览器端只用
   `ArrayBuffer` / `DataView` / TypedArray。
5. **传输层仍走 gzip + ETag + 条件请求**。二进制响应和 JSON 一样进 `TraceCache`，
   命中时不再重压、不再重算。

## 实测（2026-10-19，`python tools\bench_viewer.py --http --repeat 3`）

| 场次 / 8000 桶 | JSON 计算 | BIN 计算 | JSON 明文 | BIN 明文 | HTTP 冷 JSON → BIN | HTTP 热 JSON → BIN |
| --- | --- | --- | --- | --- | --- | --- |
| 高避 5 圈 | **118.8 ms** | **49.3 ms** | **3607.6 KB** | **2630.1 KB** | **126.8 → 84.3 ms** | **7.0 → 5.6 ms** |
| 耐久正赛 | **171.3 ms** | **88.9 ms** | **4086.3 KB** | **2630.1 KB** | **178.1 → 135.5 ms** | **7.1 → 5.6 ms** |

高避那一行里，gzip 后的上线字节从 JSON **694.3 KB** 变成 BIN **720.1 KB**：二进制把
明文缩了约 27%，但十进制 JSON 的文本冗余更高、压得更狠，所以 gzip 后略大 3.7%。
这次换来的主要是**服务端 CPU 与冷 HTTP 时间**，不是 gzip 上线体积。

### 后续实测：去掉 list 往返（2026-10-19）

首版 BIN 仍让 `render.downsample()` 把 numpy 数组 `tolist()`，再由
`tracewire.encode()` `np.asarray()` 转回来。耐久 8000 桶的 cProfile 里，`tracewire.encode`
每请求约 **21 ms**，其中大头就是这次无意义往返。现在 `render.trace(..., arrays=True)`
直接走 `downsample_arrays()`，BIN 计算变为：

| 场次 / 8000 桶 | 首版 BIN | 去往返后 BIN | HTTP 冷请求 |
| --- | --- | --- | --- |
| 高避 5 圈 | **49.7 ms** | **28.4 ms** | **84.3 → 63.4 ms** |
| 耐久正赛 | **84.8 ms** | **66.0 ms** | **131.2 → 113.4 ms** |

JSON 路径与二进制帧格式都没有改；这是一次编码前的数据形状优化，不是协议变更。

## 代价与边界

- 值 / min / max / 距离是 float32；它们和 JSON 里按 5 / 3 位小数舍入后的数只差
  **1e-6 量级**。验收里按显示精度容差比较，不要求逐位相等。
- 快照仍内嵌 JSON。快照是离线、自包含、可双击的文件，不为了 serve 的 CPU 把导出协议
  一起改掉。
- 帧头是 JSON，所以通道名、单位、采样率等小元数据仍可读；大数组才是二进制。
- 这次没有删掉 JSON 路径。删它会让老客户端、快照和调试入口一起失去兼容面；保留它
  的成本只是 `act_trace` 的一个分支。

## Considered Options

- **继续 JSON + gzip**：否决。gzip 后体积最小，但 `json.dumps` 的 84 ms 是冷路径主瓶颈，
  且每次都付。
- **MessagePack / FlatBuffers / Arrow IPC**：否决。为了一个本地单文件工具引入新的运行期
  依赖或第二条 schema 工具链，不符合规则 4 和 ADR-0002 的轻量方向。
- **全部用 float64**：否决。时间保留 float64 是为了窗口与轴；值再上 float64 会让明文
  多出约 0.8 MB，而显示精度根本用不到。
- **另开 `/trace-bin` 路由**：否决。路由、参数、缓存 key 都会多一处；`format` 已经能
  让同一个路由表达两种编码。

## 退出方式

删掉 `format=bin` 分支与前端解码器，调用方回到默认 JSON；`tracewire.py` 没有状态，
删除不会留下迁移。若未来协议形状变化，先升帧头 `version`，再让旧版本走 JSON 回退。
