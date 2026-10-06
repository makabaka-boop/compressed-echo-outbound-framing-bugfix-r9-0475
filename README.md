# ws-echo — 手写帧引擎的 RFC 6455 回显服务

针对"压缩消息被切成多帧、中间插入 Ping"的联调场景：接收路径完全自行实现
RFC 6455 帧编解码与消息状态机，控制帧永远不会进入解压器，半条消息永远不会
被提前交付。仅 HTTP Upgrade 握手复用成熟组件（Python `http.server`），
未使用任何现成的 WebSocket 收发引擎。

## 运行

```bash
# 唯一监听进程（容器内 8080，宿主映射 127.0.0.1:18080）
docker compose up --build

# 手工构造帧的原始 TCP 测试（默认模式 32 项，分片模式 39 项），从宿主机执行：
python3 test_ws_echo.py --port 18080 --close-deadline-ms 1500

# 或在 compose 内执行（一次性客户端，不是监听进程）：
docker compose --profile test run --rm ws-echo-tests

# 联调分片/压缩回显：宿主与容器测试命令都会透传这两个变量
WS_ECHO_FRAGMENT_BYTES=8 WS_ECHO_COMPRESS_AT=64 docker compose up --build
WS_ECHO_FRAGMENT_BYTES=8 WS_ECHO_COMPRESS_AT=64 \
  docker compose --profile test run --rm ws-echo-tests
```

无 Docker 时也可直接本地运行：`WS_PORT=18080 python3 ws_echo_server.py`；
测试侧用 `--fragment-bytes N --compress-at M` 声明服务端配置以启用分片核对。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WS_PORT` | `8080` | 监听端口 |
| `WS_CLOSE_DEADLINE_MS` | `3000` | 可注入的关闭期限：发出 Close 后等待对端 Close 的上限，到期强制关闭 TCP |
| `WS_ECHO_FRAGMENT_BYTES` | `0`（关闭） | 发送分片上限 `1..4096`；未设置或为 0 时保留原单帧回显 |
| `WS_ECHO_COMPRESS_AT` | `256` | 完整业务载荷达到该字节数（`1..16384`）且已协商压缩时才压缩回显 |

两个 `WS_ECHO_*` 变量只在分片模式开启时生效；非法取值（非整数、越界）会使
进程在绑定端口前明确退出并指明出错的变量，绝不悄悄改写配置。

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
- **回显**：仅在全部校验通过、消息完整后才回显。默认单帧、原操作码、不压缩；
  开启 `WS_ECHO_FRAGMENT_BYTES` 后见下文"可选压缩回显分片"。
- **关闭**：坏帧 → 1002，坏文本（含 Close reason 非 UTF-8）→ 1007，
  超限 → 1009。Close 握手一旦开始（任一方向），不再接收新业务消息；
  等待对端 Close 以 `WS_CLOSE_DEADLINE_MS` 为限，期间遇到的垃圾字节不会
  缩短期限。

## 测试覆盖（test_ws_echo.py，全部手工构造帧）

拆头（逐字节发送）、分片间 Ping、汉字跨帧（多字节字符被切开）、损坏压缩流
（首帧即坏 / 消息中途坏）、解压后超限、16 KiB 边界 ±1、未掩码、未协商 RSV1、
分片控制帧、孤儿 continuation、帧交错、保留操作码、关闭竞争（Close 后紧跟
业务消息不得回显）、关闭期限强制生效、非法关闭码、非法发送配置的启动失败等。
凡校验失败的用例都断言**关闭帧之前没有任何业务帧**（无半条回显）。

所有回显断言都按消息重组：逐帧核对首帧操作码、continuation 操作码、RSV1 只
出现在首帧、FIN 只在末帧，再拼接（必要时解压）与已校验原消息逐字节比较——
默认模式与分片模式共用同一套核对逻辑。分片模式追加：片段字节上限（含压缩后
的线上片段）、空消息恰好交付一次、压缩阈值边界 ±1、未协商不压缩、同一连接
连续消息各自独立解压（无上下文接管）、分片回显前后控制帧独立完整。


## 可选压缩回显分片
设置 `WS_ECHO_FRAGMENT_BYTES=1..4096` 开启发送分片，未设置（或 0）时保留原单帧回显。
`WS_ECHO_COMPRESS_AT=1..16384` 按完整业务载荷字节数选择压缩，仅在已协商压缩时使用；双向仍无上下文接管。
分片不改变业务消息类型与字节：首帧携带原操作码，后续片段为 continuation；压缩作用于整条消息
（单条 DEFLATE 流，RSV1 仅在首帧），先压缩后切片，因此每个线上片段（含压缩后）都不超过配置上限，
重新组装（必要时解压）后等于完整校验过的原消息。每条消息使用全新压缩上下文，后续消息无历史依赖。
控制帧保持独立且不压缩；空消息恰好交付一次。错误输入和关闭后的业务不产生任何回显片段。
取值非法时进程在启动阶段明确失败，不悄悄更改配置。
