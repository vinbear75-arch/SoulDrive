def classify_indexing_error(message: str) -> dict[str, str]:
    normalized = (message or "").lower()
    if "partial_success conversion" in normalized or "failed pages:" in normalized:
        return {"error_code": "PARTIAL_DOCUMENT_PARSE", "category": "parser"}
    if "no parseable chunks" in normalized:
        return {"error_code": "NO_PARSEABLE_CHUNKS", "category": "parser"}
    if "password" in normalized or "encrypted" in normalized:
        return {"error_code": "ENCRYPTED_DOCUMENT", "category": "input"}
    if "permission" in normalized or "access is denied" in normalized:
        return {"error_code": "FILE_ACCESS_DENIED", "category": "filesystem"}
    if "no such file" in normalized or "not found" in normalized:
        return {"error_code": "FILE_NOT_FOUND", "category": "filesystem"}
    if "out of memory" in normalized or "bad_alloc" in normalized:
        return {"error_code": "RESOURCE_EXHAUSTED", "category": "resource"}
    if "insufficient disk" in normalized or "disk space" in normalized or "no space left" in normalized:
        return {"error_code": "INSUFFICIENT_DISK_SPACE", "category": "resource"}
    return {"error_code": "INDEXING_FAILED", "category": "unknown"}


def indexing_failure(source_filename: str, reason: str) -> dict[str, str]:
    classified = classify_indexing_error(reason)
    return {
        "source_filename": source_filename,
        "reason": reason,
        **classified,
    }
