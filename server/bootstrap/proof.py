"""One deep-proof operation may reuse a digest only for the same file generation.

No digest survives the operation. Every reuse and the final proof check stat
both the path and its resolved target, including inode, mode, size and ctime.
A changed/missing input invalidates the proof; directory integrity is still
rehashed from bytes on every deep-readiness request.
"""
from __future__ import annotations

import os


class FileDigestProof:
    def __init__(self, hash_file):
        self._hash_file = hash_file
        self._digests = {}
        self._invalid = False

    @staticmethod
    def _generation(path):
        def fields(stat):
            return (stat.st_dev, stat.st_ino, stat.st_mode, stat.st_size,
                    stat.st_mtime_ns, stat.st_ctime_ns)
        return (os.path.realpath(path), fields(os.lstat(path)), fields(os.stat(path)))

    def checksum(self, path):
        path = os.path.abspath(os.fspath(path))
        try:
            generation = self._generation(path)
            previous = self._digests.get(path)
            if previous is not None:
                if previous[0] != generation:
                    self._invalid = True
                    return ""
                return previous[1]
            digest = self._hash_file(path)
            if not digest or self._generation(path) != generation:
                self._invalid = True
                return ""
            self._digests[path] = (generation, digest)
            return digest
        except OSError:
            self._invalid = True
            return ""

    def unchanged(self):
        if self._invalid:
            return False
        try:
            return all(self._generation(path) == generation
                       for path, (generation, _) in self._digests.items())
        except OSError:
            return False
