"""Opt-in outbound echo framing policy.

Turns one fully validated message into wire frames without changing its
type or bytes:

  - the first fragment carries the message opcode, later fragments OP_CONT;
  - every fragment payload is at most ``frame_bytes`` on the wire;
  - permessage-deflate applies to the COMPLETE message (only when negotiated
    and the full payload reaches ``compress_at``), with RSV1 on the first
    frame only and a fresh compressor per message -- no context takeover, so
    the peer needs no history from earlier messages;
  - an empty message is delivered exactly once;
  - control frames never pass through here.
"""

import zlib
from dataclasses import dataclass

OP_CONT = 0x0

MAX_FRAGMENT_BYTES = 4096  # documented range of WS_ECHO_FRAGMENT_BYTES: 1..4096
MAX_COMPRESS_AT = 16 * 1024  # documented range of WS_ECHO_COMPRESS_AT: 1..16384
DEFLATE_TAIL = b"\x00\x00\xff\xff"  # stripped per RFC 7692, as on the receive path


@dataclass(frozen=True)
class OutboundPolicy:
    frame_bytes: int
    compress_at: int = 256

    def __post_init__(self):
        if not 1 <= self.frame_bytes <= MAX_FRAGMENT_BYTES:
            raise ValueError(
                f"WS_ECHO_FRAGMENT_BYTES must be 1..{MAX_FRAGMENT_BYTES}, "
                f"got {self.frame_bytes}"
            )
        if not 1 <= self.compress_at <= MAX_COMPRESS_AT:
            raise ValueError(
                f"WS_ECHO_COMPRESS_AT must be 1..{MAX_COMPRESS_AT}, "
                f"got {self.compress_at}"
            )

    def frames(self, opcode, payload, negotiated):
        compress = negotiated and len(payload) >= self.compress_at
        data = payload
        if compress:
            # one fresh compressor for the whole message: a single DEFLATE
            # stream, no state carried over from previous messages
            compressor = zlib.compressobj(wbits=-15)
            data = compressor.compress(payload) + compressor.flush(zlib.Z_SYNC_FLUSH)
            data = data[: -len(DEFLATE_TAIL)]
        # slice AFTER compression so every wire fragment honours the cap
        chunks = [
            data[i : i + self.frame_bytes]
            for i in range(0, len(data), self.frame_bytes)
        ] or [b""]  # empty message: exactly one empty frame
        last = len(chunks) - 1
        return [
            (
                index == last,  # fin on the last fragment only
                compress and index == 0,  # RSV1 on the first fragment only
                opcode if index == 0 else OP_CONT,
                part,
            )
            for index, part in enumerate(chunks)
        ]
