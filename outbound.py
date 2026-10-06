"""Opt-in outbound echo framing policy."""

import zlib
from dataclasses import dataclass


@dataclass(frozen=True)
class OutboundPolicy:
    frame_bytes: int
    compress_at: int = 256

    def frames(self, opcode, payload, negotiated):
        chunks = [
            payload[i : i + self.frame_bytes]
            for i in range(0, len(payload), self.frame_bytes)
        ] or [b""]
        result = []
        for index, part in enumerate(chunks):
            compressed = negotiated and len(payload) >= self.compress_at
            if compressed:
                compressor = zlib.compressobj(wbits=-15)
                part = (
                    compressor.compress(part) + compressor.flush(zlib.Z_SYNC_FLUSH)
                )[:-4]
            result.append((index == len(chunks) - 1, compressed, opcode, part))
        return result
