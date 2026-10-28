"""Local storage. Same interface can later be implemented for S3-compatible stores."""
import asyncio
import hashlib
import os
import uuid
from pathlib import Path


class TooLarge(Exception):
    pass


class BadMedia(Exception):
    pass


# MP4, M4V, MKV and WebM container signatures.
EBML_SIGNATURE = b"\x1A\x45\xDF\xA3"


def sniff(ext: str, head: bytes) -> bool:
    ext = ext.lower()

    if ext in (".mp4", ".m4v"):
        return len(head) >= 8 and head[4:8] == b"ftyp"

    if ext in (".mkv", ".webm"):
        # Both MKV and WebM use the EBML container signature.
        # This verifies the container signature, not the specific
        # video codec or the exact Matroska/WebM document type.
        return head.startswith(EBML_SIGNATURE)

    return False


class LocalStorage:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if p.parent != self.root:
            raise ValueError("invalid storage key")
        return p

    async def save(self, chunks, max_bytes: int, ext: str):
        ext = ext.lower()

        if ext not in {".mp4", ".m4v", ".mkv", ".webm"}:
            raise BadMedia()

        key = uuid.uuid4().hex + ext
        tmp = self.path(key + ".part")
        sha = hashlib.sha256()
        size = 0
        head = bytearray()
        checked = False

        try:
            with open(tmp, "wb") as f:
                async for chunk in chunks:
                    if not chunk:
                        continue

                    size += len(chunk)
                    if size > max_bytes:
                        raise TooLarge()

                    # Read only enough bytes to identify the container.
                    if not checked:
                        needed = 12 - len(head)
                        head.extend(chunk[:needed])

                        # EBML signatures are 4 bytes; MP4 requires 8.
                        min_header = 8 if ext in (".mp4", ".m4v") else 4

                        if len(head) >= min_header:
                            if not sniff(ext, bytes(head)):
                                raise BadMedia()
                            checked = True

                    sha.update(chunk)
                    await asyncio.to_thread(f.write, chunk)

            if not checked or size == 0:
                raise BadMedia()

            os.replace(tmp, self.path(key))
            return key, size, sha.hexdigest()

        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def delete(self, key: str):
        self.path(key).unlink(missing_ok=True)
