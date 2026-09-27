"""Upload safety: detect type from magic bytes, cap size, re-encode images (drops EXIF). Pure except Pillow."""
import hashlib
import io
from PIL import Image

MAGIC = [
    (b"%PDF-", "application/pdf"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
]
Image.MAX_IMAGE_PIXELS = 40_000_000   # refuse decompression bombs


class FileRejected(Exception):
    def __init__(self, status, problem, detail):
        super().__init__(detail)
        self.status = status
        self.problem = problem
        self.detail = detail


def sniff_content_type(data):
    for signature, content_type in MAGIC:
        if data.startswith(signature):
            return content_type
    return None


def prepare_upload(data, max_bytes):
    """Return (content_type, stored_bytes, sha256_hex) or raise FileRejected."""
    if len(data) == 0:
        raise FileRejected(422, "validation-error", "empty file")
    if len(data) > max_bytes:
        raise FileRejected(413, "file-too-large", "file is larger than the limit")
    content_type = sniff_content_type(data)
    if content_type is None:
        raise FileRejected(415, "unsupported-file-type", "only PDF, JPEG or PNG files are accepted")
    stored = data
    if content_type in ("image/jpeg", "image/png"):
        try:
            image = Image.open(io.BytesIO(data))
            image.load()
        except Exception:
            raise FileRejected(415, "unsupported-file-type", "the image could not be decoded") from None
        output = io.BytesIO()
        # re-encoding without passing exif= drops all metadata (GPS, camera, timestamps)
        if content_type == "image/jpeg":
            image.convert("RGB").save(output, format="JPEG", quality=90)
        else:
            image.save(output, format="PNG")
        stored = output.getvalue()
        if len(stored) > max_bytes:
            raise FileRejected(413, "file-too-large", "file is larger than the limit after processing")
    return content_type, stored, hashlib.sha256(stored).hexdigest()
