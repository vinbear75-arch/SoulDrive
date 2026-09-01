import hashlib
import shutil
from pathlib import Path

from core.document_types import SUPPORTED_DOCUMENT_EXTENSIONS, document_media_type
from core.document_types import SUPPORTED_PAPER_EXTENSIONS
from core.secure_document_store import SecureDocumentStore
from core.security_context import get_workspace_keys
from core.workspace import SoulDriveWorkspace


def import_paper_into_workspace(workspace: SoulDriveWorkspace, source_path: str, *, allowed_extensions=None) -> dict:
    source = Path(source_path)
    display_name = source.name or "unknown"

    allowed_extensions = allowed_extensions or SUPPORTED_DOCUMENT_EXTENSIONS
    if source.suffix.lower() not in allowed_extensions:
        return {
            "name": display_name,
            "status": "rejected",
            "error_code": "UNSUPPORTED_FILE_TYPE",
        }
    if not source.exists() or not source.is_file():
        return {
            "name": display_name,
            "status": "rejected",
            "error_code": "SOURCE_NOT_FOUND",
        }

    papers_dir = Path(workspace.papers_path)
    papers_dir.mkdir(parents=True, exist_ok=True)
    base_destination = papers_dir / safe_document_filename(source.name)

    workspace_keys = get_workspace_keys(workspace.root_path)
    if workspace_keys is not None:
        store = SecureDocumentStore(workspace, workspace_keys)
        try:
            return store.import_document(source_path)
        finally:
            store.close()

    try:
        if source.resolve() == base_destination.resolve():
            return {
                "name": base_destination.name,
                "status": "already_present",
                "media_type": document_media_type(base_destination.name),
            }
    except OSError:
        pass

    if base_destination.exists() and same_file_content(source, base_destination):
        return {
            "name": base_destination.name,
            "status": "already_present",
            "media_type": document_media_type(base_destination.name),
        }

    destination = available_paper_path(base_destination)
    shutil.copy2(str(source), str(destination))
    return {
        "name": destination.name,
        "status": "imported",
        "media_type": document_media_type(destination.name),
    }


def safe_document_filename(filename: str) -> str:
    cleaned = "".join(
        "_" if character in '<>:"/\\|?*' or ord(character) < 32 else character
        for character in Path(filename).name
    ).strip(" .")
    if not cleaned:
        cleaned = "paper.pdf"
    path = Path(cleaned)
    if path.suffix.lower() not in SUPPORTED_DOCUMENT_EXTENSIONS:
        cleaned = f"{path.stem or 'paper'}.pdf"
    return cleaned


def safe_pdf_filename(filename: str) -> str:
    cleaned = safe_document_filename(filename)
    path = Path(cleaned)
    if path.suffix.lower() != ".pdf":
        return f"{path.stem or 'paper'}.pdf"
    return cleaned


def available_paper_path(destination: Path) -> Path:
    if not destination.exists():
        return destination

    stem = destination.stem
    suffix = destination.suffix
    for index in range(2, 1000):
        candidate = destination.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"too many duplicate paper names for {destination.name}")


def same_file_content(left: Path, right: Path) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
    except OSError:
        return False
    return file_sha256(left) == file_sha256(right)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
