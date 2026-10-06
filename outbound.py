"""Opt-in outbound echo framing policy.

Enabled with WS_ECHO_FRAGMENT_BYTES. Given one complete, fully validated
business message, ``frames`` produces the on-the-wire fragments:

  * the first frame keeps the original TEXT/BINARY opcode; every later frame
    is a CONTINUATION (0x0), and only the last frame has FIN set
  * permessage-deflate is decided once per message from the COMPLETE payload
    (negotiated AND len(payload) >= compress_at): a single raw-DEFLATE stream
    covers the whole message, RSV1 is set on its first frame only and the
    0x00 0x00 0xff 0xff sync-flush tail is stripped once. The compressed byte
    stream (which may be larger than the input) is then sliced so every
    fragment payload stays within frame_bytes
  * a fresh compressor per message keeps every echo independent
    (no context takeover): later messages need no history from earlier ones
  * an empty message is delivered exactly once
"""

import zlib
from dataclasses import dataclass

OP_CONT = 0x0

MAX_FRAGMENT_BYTES = 4096
MAX_MESSAGE_BYTES = 16 * 1024
DEFAULT_COMPRESS_AT = 256
_DEFLATE_TAIL = b"\x00\x00\xff\xff"  # stripped at message end per RFC 7692


class OutboundConfigError(ValueError):
    """Raised at startup when the outbound echo configuration is invalid."""


def _bounded_int(value, name: str, hi: int) -> int:
    # bool is a subclass of int; reject it explicitly for a clear message
    if isinstance(value, bool) or not isinstance(value, int):
        raise OutboundConfigError(f"{name} must be an integer, got {value!r}")
    if not 1 <= value <= hi:
        raise OutboundConfigError(f"{name} must be in 1..{hi}, got {value}")
    return value


@dataclass(frozen=True)
class OutboundPolicy:
    frame_bytes: int
    compress_at: int = DEFAULT_COMPRESS_AT

    def __post_init__(self):
        _bounded_int(self.frame_bytes, "WS_ECHO_FRAGMENT_BYTES", MAX_FRAGMENT_BYTES)
        _bounded_int(self.compress_at, "WS_ECHO_COMPRESS_AT", MAX_MESSAGE_BYTES)

    def frames(self, opcode: int, payload: bytes, negotiated: bool):
        """Return [(fin, rsv1, opcode, payload), ...] for one full message.

        Pure computation: the whole fragment list exists before any byte is
        sent, so a failure here can never produce a half-sent echo.
        """
        # compression is a per-message decision on the complete payload
        compress = negotiated and len(payload) >= self.compress_at
        if compress:
            # one DEFLATE stream for the whole message; fresh compressor keeps
            # messages history-independent (bidirectional no_context_takeover)
            compressor = zlib.compressobj(
                zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, -15
            )
            wire = (
                compressor.compress(payload)
                + compressor.flush(zlib.Z_SYNC_FLUSH)
            )[: -len(_DEFLATE_TAIL)]  # Z_SYNC_FLUSH always emits this tail
        else:
            wire = payload

        if not wire:
            # empty message: exactly one frame, original opcode, FIN, no RSV1
            return [(True, False, opcode, b"")]

        chunks = [
            wire[i : i + self.frame_bytes]
            for i in range(0, len(wire), self.frame_bytes)
        ]
        last = len(chunks) - 1
        result = []
        for index, part in enumerate(chunks):
            first = index == 0
            result.append(
                (
                    index == last,          # FIN only on the final fragment
                    compress and first,     # RSV1 only on the first fragment
                    opcode if first else OP_CONT,
                    part,
                )
            )
        return result
