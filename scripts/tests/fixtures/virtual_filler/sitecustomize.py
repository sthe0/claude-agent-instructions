"""Reports /'s real free space minus a fixed byte offset: a filler file that is not there.

The offset is read once from /home/the0/.local/state/cgmi/virtual-filler-bytes (written before
the run, from df, so the reported value starts at 10.3 GB); as the run and other sessions write
to the shared disk, the reported value moves with the real free space, so a run whose own writes
cross the 10 GB floor sees them — without 15 GB of real filler on a 95 percent full shared disk.
The running minimum of the reported / free is recorded to
/home/the0/.local/state/cgmi/virtual-filler-min (bytes, one line), so a caller can read back how
far below the start the run went; the caller clears the file before each run.
"""
import os
import shutil
from pathlib import Path

FILLER_OFFSET_BYTES = Path("/home/the0/.local/state/cgmi/virtual-filler-bytes")
MIN_FILE = Path("/home/the0/.local/state/cgmi/virtual-filler-min")

_real_statvfs = os.statvfs
_DiskUsage = type(shutil.disk_usage("/"))
try:
    _offset = int(FILLER_OFFSET_BYTES.read_text(encoding="utf-8"))
except OSError as exc:
    raise RuntimeError(
        f"virtual_filler fixture: cannot read {FILLER_OFFSET_BYTES} "
        f"(write the byte offset there before the run): {exc}"
    ) from exc
_min_reported = None


def _disk_usage(path):
    global _min_reported
    p = os.fspath(path)
    st = _real_statvfs(p)
    frsize = st[1] or st[0] or 1
    total = st[2] * frsize
    used = total - st[3] * frsize
    free = max(st[4] * frsize - _offset, 0)
    if p == "/":
        if _min_reported is None or free < _min_reported:
            _min_reported = free
        try:
            # Monotonic across processes: a concurrent child writing the same/larger value
            # loses the recompare loop, so the file can only move down.
            try:
                current = MIN_FILE.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                current = ""
            if not current or free < int(current):
                MIN_FILE.write_text(f"{free}\n", encoding="utf-8")
                after = MIN_FILE.read_text(encoding="utf-8").strip()
                if after and free < int(after):
                    MIN_FILE.write_text(f"{free}\n", encoding="utf-8")
        except OSError:
            pass
    return _DiskUsage(total, used, free)


def _statvfs(path):
    st = _real_statvfs(os.fspath(path))
    fields = list(st)
    frsize = fields[1] or 1
    free = max(fields[4] * frsize - _offset, 0)
    # statvfs_result: 0 f_bsize, 1 f_frsize, 2 f_blocks, 3 f_bfree, 4 f_bavail, ...
    fields[3] = min(fields[3], free // frsize)
    fields[4] = min(fields[4], free // frsize)
    return type(st)(fields)


shutil.disk_usage = _disk_usage
os.statvfs = _statvfs