"""Pure-Python Protobuf Decoder for AOSP build_progress.pb.

Matches soong_build_progress.BuildProgress schema:
  uint64 total_actions = 1
  uint64 finished_actions = 2
  uint64 current_actions = 3
  uint64 failed_actions = 4
"""

from typing import Optional, NamedTuple


class BuildProgress(NamedTuple):
    total_actions: int
    finished_actions: int
    current_actions: int
    failed_actions: int


def parse_build_progress(file_path: str) -> Optional[BuildProgress]:
    """Parse AOSP build_progress.pb file.
    
    Returns BuildProgress namedtuple or None if file cannot be read or is invalid.
    """
    try:
        with open(file_path, "rb") as f:
            data = f.read()
    except Exception:
        return None

    if not data:
        return None

    idx = 0
    length = len(data)
    fields = {}

    while idx < length:
        tag = data[idx]
        idx += 1
        field_num = tag >> 3
        wire_type = tag & 7

        if wire_type == 0:  # Varint
            val = 0
            shift = 0
            while idx < length:
                b = data[idx]
                idx += 1
                val |= (b & 0x7F) << shift
                shift += 7
                if not (b & 0x80):
                    break
            fields[field_num] = val
        elif wire_type == 1:  # 64-bit
            idx += 8
        elif wire_type == 2:  # Length-delimited
            if idx >= length:
                break
            length_val = 0
            shift = 0
            while idx < length:
                b = data[idx]
                idx += 1
                length_val |= (b & 0x7F) << shift
                shift += 7
                if not (b & 0x80):
                    break
            idx += length_val
        elif wire_type == 5:  # 32-bit
            idx += 4
        else:
            # Unsupported wire type
            break

    total = fields.get(1, 0)
    finished = fields.get(2, 0)
    current = fields.get(3, 0)
    failed = fields.get(4, 0)

    return BuildProgress(
        total_actions=total,
        finished_actions=finished,
        current_actions=current,
        failed_actions=failed,
    )
