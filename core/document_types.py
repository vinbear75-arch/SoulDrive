from pathlib import Path


SUPPORTED_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
SUPPORTED_TEXT_EXTENSIONS = frozenset({".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".log"})
SUPPORTED_OFFICE_EXTENSIONS = frozenset({".docx", ".xlsx", ".pptx"})
SUPPORTED_DOCUMENT_EXTENSIONS = frozenset({".pdf", *SUPPORTED_IMAGE_EXTENSIONS, *SUPPORTED_TEXT_EXTENSIONS, *SUPPORTED_OFFICE_EXTENSIONS})
SUPPORTED_PAPER_EXTENSIONS = frozenset({".pdf", *SUPPORTED_IMAGE_EXTENSIONS})


def document_media_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".md":
        return "text/markdown"
    if suffix in {".txt", ".log"}:
        return "text/plain"
    if suffix == ".csv":
        return "text/csv"
    if suffix == ".json":
        return "application/json"
    if suffix in {".yaml", ".yml"}:
        return "application/yaml"
    if suffix == ".docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if suffix == ".xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if suffix == ".pptx":
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    return "application/pdf"


def is_supported_document(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_DOCUMENT_EXTENSIONS


def is_image_document(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
