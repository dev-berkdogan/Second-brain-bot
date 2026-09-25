import glob
import os
from typing import List

from PIL import Image


IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".heic",
}

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".webm",
    ".m4v",
    ".mkv",
}


def _get_media_files(media_dir: str) -> List[str]:
    """
    Return all files under media_dir recursively.
    """
    files: List[str] = []

    for root, _, filenames in os.walk(media_dir):
        for filename in filenames:
            files.append(
                os.path.join(root, filename)
            )

    return files


def detect_media_composition(media_dir: str) -> str:
    """
    Detect the actual media composition.

    Returns only values supported by the Supabase content_type enum:
    - image
    - carousel
    - video
    - unknown
    """
    files = _get_media_files(media_dir)

    images: List[str] = []
    videos: List[str] = []

    for path in files:
        extension = os.path.splitext(path)[1].lower()

        if extension in IMAGE_EXTENSIONS:
            images.append(path)

        elif extension in VIDEO_EXTENSIONS:
            videos.append(path)

    if len(images) > 1:
        return "carousel"

    if images and videos:
        return "carousel"

    if len(images) == 1:
        return "image"

    if len(videos) >= 1:
        return "video"

    return "unknown"


def optimize_image_for_analysis(
    image_path: str,
    max_dimension: int = 1600,
) -> str:
    """
    Downscale very large images before multimodal analysis.

    Keeps the original file path and only changes the file when
    resizing is actually needed.
    """
    try:
        with Image.open(image_path) as img:
            if (
                img.width <= max_dimension
                and img.height <= max_dimension
            ):
                return image_path

            original_mode = img.mode

            img.thumbnail(
                (max_dimension, max_dimension),
                Image.Resampling.LANCZOS,
            )

            if image_path.lower().endswith(
                (".jpg", ".jpeg")
            ):
                if original_mode in ("RGBA", "LA", "P"):
                    img = img.convert("RGB")

                img.save(
                    image_path,
                    format="JPEG",
                    quality=85,
                    optimize=True,
                )
            else:
                img.save(
                    image_path,
                    optimize=True,
                )

    except Exception:
        # Image optimization is best-effort.
        return image_path

    return image_path