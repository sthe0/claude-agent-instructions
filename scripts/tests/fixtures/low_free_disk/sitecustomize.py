"""Shadows the free-space readers so every read in this process reports about 1 GiB free.

First on PYTHONPATH (SUITE_GATE_EXTRA_PYTHONPATH puts it there), so this module replaces the
interpreter's own sitecustomize: a gate-shaped run on a machine whose / is nearly full, without
writing a real filler file on the shared disk. Only the free figures are lied about — every
other field of the real reads is kept, f_flag (mount flags) included, so mount-flag readers
keep their real answers.
"""
import os
import shutil

LOW_FREE_BYTES = 1 * 2**30

_real_disk_usage = shutil.disk_usage
_real_statvfs = os.statvfs


def _capped_free(free: int) -> int:
    return min(int(free), LOW_FREE_BYTES)


def _disk_usage(path):
    total, used, free = _real_disk_usage(os.fspath(path))
    return shutil._ntuple_diskusage(total, used, _capped_free(free))


def _statvfs(path):
    st = _real_statvfs(os.fspath(path))
    fields = list(st)
    frsize = fields[1] or 1
    # statvfs_result: 0 f_bsize, 1 f_frsize, 2 f_blocks, 3 f_bfree, 4 f_bavail, ...
    fields[3] = min(fields[3], LOW_FREE_BYTES // frsize)
    fields[4] = min(fields[4], LOW_FREE_BYTES // frsize)
    return type(st)(fields)


shutil.disk_usage = _disk_usage
os.statvfs = _statvfs