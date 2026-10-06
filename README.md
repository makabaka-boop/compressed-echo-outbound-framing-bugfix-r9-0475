# ws-echo — 手写帧引擎的 RFC 6455 回显服务

针对"压缩消息被切成多帧、中间插入 Ping"的联调场景：接收路径完全自行实现
RFC 6455 帧编解码与消息状态机，控制帧永远不会进入解压器，半条消息永远不会
被提前交付。仅 HTTP Upgrade 握手复用成熟组件（Python `http.server`），
未使用任何现成的 WebSocket 收发引擎。

## 运行

```bash
# 唯一监听进程（容器内 8080，宿主映射 127.0.0.1:18080）
docker compose up --build

# 手工构造帧的原始 TCP 测试（39 项），从宿主机执行：
python3 test_ws_echo.py --port 18080 --close-deadline-ms 1500

# 服务端开启发送分片后，须把生效配置传给测试以便重组/解压核对：
python3 test_ws_echo.py --port 18080 --close-deadline-ms 1500 \
  --fragment-bytes 64 --compress-at 256

# 或在 compose 内执行（一次性客户端，不是监听进程）：
docker compose --profile test run --rm ws-echo-tests
```

无 Docker 时也可直接本地运行：`WS_PORT=18080 python3 ws_echo_server.py`。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WS_PORT` | `8080` | 监听端口 |
| `WS_CLOSE_DEADLINE_MS` | `3000` | 可注入的关闭期限：发出 Close 后等待对端 Close 的上限，到期强制关闭 TCP |
| `WS_ECHO_FRAGMENT_BYTES` | 未设置 | 设置为 `1..4096` 开启回显分片；未设置/为空时保留原单帧回显 |
| `WS_ECHO_COMPRESS_AT` | `256` | 开启分片后，完整业务载荷达到该字节数（`1..16384`）且已协商压缩时才压缩 |

## 协议行为

- **帧**：客户端帧必须带掩码；支持 text/binary/continuation/ping/pong/close；
  保留操作码、RSV2/RSV3、64 位长度最高位置位均为协议错误。
- **分片**：continuation 必须属于已打开的消息；消息未结束不得开启新数据帧；
  控制帧不得分片且载荷 ≤ 125；Ping 可出现在分片之间，不影响消息状态，
  立即回复 Pong。
- **permessage-deflate**：仅协商双向 `no_context_takeover`
  （`permessage-deflate; server_no_context_takeover; client_no_context_takeover`）。
  未协商时 RSV1 直接拒绝；协商后 RSV1 只允许出现在消息首个数据帧。
  DEFLATE 由 zlib 承担（raw，`wbits=-15`），`0x00 0x00 0xff 0xff` 尾部只在
  消息结束帧到达时补入。
- **UTF-8**：文本消息使用增量解码器跨分片连续校验，多字节字符可跨帧拆分；
  消息结束时字符边界不完整也算文本错误。
- **大小**：完整消息（解压后）上限 16 KiB；单帧线上 sanity 上限 64 KiB。
- **回显**：仅在全部校验通过、消息完整后才回显（单帧，原操作码，不压缩）。
- **关闭**：坏帧 → 1002，坏文本（含 Close reason 非 UTF-8）→ 1007，
  超限 → 1009。Close 握手一旦开始（任一方向），不再接收新业务消息；
  等待对端 Close 以 `WS_CLOSE_DEADLINE_MS` 为限，期间遇到的垃圾字节不会
  缩短期限。

## 测试覆盖（test_ws_echo.py，全部手工构造帧）

拆头（逐字节发送）、分片间 Ping、汉字跨帧（多字节字符被切开）、损坏压缩流
（首帧即坏 / 消息中途坏）、解压后超限、16 KiB 边界 ±1、未掩码、未协商 RSV1、
分片控制帧、孤儿 continuation、帧交错、保留操作码、关闭竞争（Close 后紧跟
业务消息不得回显）、关闭期限强制生效、非法关闭码等。凡校验失败的用例都断言
**关闭帧之前没有任何业务帧**（无半条回显）。


## 可选压缩回显分片
设置 `WS_ECHO_FRAGMENT_BYTES=1..4096` 开启发送分片，未设置（或为空）时保留原单帧回显。
`WS_ECHO_COMPRESS_AT=1..16384`（默认 256）按**完整业务载荷**字节数选择压缩，
且仅在连接已协商 permessage-deflate 时生效；双向仍无上下文接管，每条消息各自
新建压缩器，后续消息不依赖前一条的历史。

- 首帧保留原 TEXT/BINARY 操作码并仅在压缩时置 RSV1，其余帧全部是 CONTINUATION，
  FIN 只出现在最后一帧；因此分片不改变业务消息类型与字节，客户端按 RFC 6455
  逐帧重组即得到校验过的原消息。
- 压缩对**整条消息**生成一个 raw-DEFLATE 流（sync-flush 尾部只砍一次），
  再按上限切片——即使不可压缩数据压缩后变大，每个片段的线上载荷仍
  ≤ `WS_ECHO_FRAGMENT_BYTES`。
- 控制帧（Pong/Close）始终独立发送、不分片、不压缩，可插在回显片段序列之外；
  空消息恰好交付一个空帧（原操作码、FIN、无 RSV1）。
- 回显片段列表在发送前整体生成；坏输入或 Close 之后的业务不会产生任何片段，
  Close 帧之前不会出现半条回显。
- 配置非法（非整数、超出范围）时进程在绑定端口前以非零状态退出并打印
  `invalid outbound echo configuration: ...`，不悄悄回退或更改配置。

发送链路专项测试（线帧格式、操作码/FIN/RSV1 归属、压缩片段不超上限、
阈值与协商门槛、空消息、同连接连续消息独立、Ping 间 Pong 独立、
非法配置启动失败）均包含在 `test_ws_echo.py`；部分用例在未开启分片时自动跳过。
