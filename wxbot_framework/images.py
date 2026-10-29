"""本地图片发送前的文件校验；不连接网络或原生微信服务。"""
from __future__ import annotations

import base64
import io
import os
from pathlib import Path
import stat

MAX_IMAGE_BYTES = 20 * 1024 * 1024
VISION_MAX_SIDE = 1280
VISION_MAX_BYTES = 4 * 1024 * 1024


def _has_image_signature(header):
    return (header.startswith(b"\xff\xd8\xff") or
            header.startswith(b"\x89PNG\r\n\x1a\n") or
            header.startswith((b"GIF87a", b"GIF89a")) or
            (header.startswith(b"RIFF") and header[8:12] == b"WEBP") or
            header.startswith(b"BM"))


def validate_image_path(path):
    """返回现存普通图片文件的绝对路径；格式按文件头而非扩展名判断。"""
    try:
        value = os.fspath(path)
    except TypeError as error:
        raise ValueError("图片路径必须是本地文件路径") from error
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError("图片路径必须是非空本地文件路径，且不能包含 NUL")
    try:
        resolved = Path(value).resolve(strict=True)
        if len(str(resolved).encode("utf-8")) > 1023:
            raise ValueError("图片路径不能超过 1023 个 UTF-8 字节")
        information = resolved.stat()
        if not stat.S_ISREG(information.st_mode):
            raise ValueError("图片路径必须指向普通文件")
        if not 0 < information.st_size <= MAX_IMAGE_BYTES:
            raise ValueError("图片文件必须非空，且不能超过 20 MiB")
        with resolved.open("rb") as source:
            header = source.read(16)
            current = os.fstat(source.fileno())
        if (not stat.S_ISREG(current.st_mode) or
                not 0 < current.st_size <= MAX_IMAGE_BYTES):
            raise ValueError("图片文件必须是非空普通文件，且不能超过 20 MiB")
    except (OSError, RuntimeError) as error:
        raise ValueError("图片文件不存在或无法读取") from error
    if not _has_image_signature(header):
        raise ValueError("图片格式仅支持 JPEG、PNG、GIF、WebP 和 BMP")
    return resolved


def image_data_url(path, max_side=VISION_MAX_SIDE, max_bytes=VISION_MAX_BYTES):
    """把本地图片压成视觉模型可用的 JPEG data URL，顺带剥掉原始元数据。"""
    source = validate_image_path(path)
    try:
        from PIL import Image, ImageOps
        with Image.open(source) as decoded:
            picture = ImageOps.exif_transpose(decoded)
            if picture.mode in ("RGBA", "LA", "P"):
                picture = picture.convert("RGBA")
                canvas = Image.new("RGB", picture.size, "white")
                canvas.paste(picture, mask=picture.split()[-1])
                picture = canvas
            picture = picture.convert("RGB")
            picture.thumbnail((max_side, max_side))
    except (OSError, ValueError) as error:
        raise ValueError("图片无法解码，不能发给视觉模型") from error
    for quality in (88, 78, 68, 58):
        buffer = io.BytesIO()
        picture.save(buffer, "JPEG", quality=quality, optimize=True)
        data = buffer.getvalue()
        if len(data) <= max_bytes:
            return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
    raise ValueError("图片压缩后仍超过 4 MiB，不发给视觉模型")
