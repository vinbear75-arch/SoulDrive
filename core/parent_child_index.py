import re
from dataclasses import dataclass
from typing import Callable


TokenCounter = Callable[[str], int]


@dataclass(frozen=True)
class ChildDocument:
    parent_id: str
    child_id: str
    content: str
    start_char: int
    end_char: int
    child_index: int
    overlap_chars: int = 0
    chunk_strategy: str = "dynamic_structure_overlap_v1"
    token_count: int = 0
    semantic_unit_count: int = 0


@dataclass(frozen=True)
class _TextBlock:
    content: str
    start_char: int
    end_char: int
    kind: str = "paragraph"


def split_parent_document(
    parent_id: str,
    text: str,
    child_size: int = 900,
    child_overlap: int = 120,
    *,
    formal_structure: bool = False,
    token_counter: TokenCounter | None = None,
    max_tokens: int | None = None,
) -> list[ChildDocument]:
    clean_text = (text or "").strip()
    if not clean_text:
        return []
    if child_size <= 0:
        raise ValueError("child_size must be positive")
    if child_overlap < 0 or child_overlap >= child_size:
        raise ValueError("child_overlap must be smaller than child_size")
    if max_tokens is not None and max_tokens <= 0:
        raise ValueError("max_tokens must be positive")

    target_size = _dynamic_child_size(clean_text, child_size)
    blocks = _split_structural_blocks(
        clean_text,
        target_size,
        child_overlap,
        token_counter=token_counter,
        max_tokens=max_tokens,
    )
    if not blocks:
        return []

    if formal_structure:
        windows = _formal_structure_windows(
            blocks,
            target_size,
            token_counter=token_counter,
            max_tokens=max_tokens,
        )
        windows = _enforce_window_limits(
            windows,
            target_size,
            token_counter=token_counter,
            max_tokens=max_tokens,
        )
        return _children_from_windows(
            parent_id,
            windows,
            chunk_strategy="formal_structure_paragraph_window_v2",
            token_counter=token_counter,
        )

    windows: list[list[_TextBlock]] = []
    block_start = 0

    while block_start < len(blocks):
        block_end = block_start
        content_parts: list[str] = []
        content_length = 0

        while block_end < len(blocks):
            block = blocks[block_end]
            candidate_length = content_length + len(block.content) + (2 if content_parts else 0)
            candidate_content = "\n\n".join([*content_parts, block.content])
            if content_parts and not _fits_limits(
                candidate_content,
                target_size,
                token_counter=token_counter,
                max_tokens=max_tokens,
            ):
                break
            content_parts.append(block.content)
            content_length = candidate_length
            block_end += 1
            if not _fits_limits(
                candidate_content,
                target_size,
                token_counter=token_counter,
                max_tokens=max_tokens,
            ) or content_length >= target_size:
                break

        if block_end == block_start:
            block_end += 1
            content_parts.append(blocks[block_start].content)

        window = blocks[block_start:block_end]
        if window:
            windows.append(window)

        if block_end >= len(blocks):
            break

        overlap_blocks = _overlap_block_count(window, child_overlap)
        next_start = max(block_start + 1, block_end - overlap_blocks)
        block_start = next_start

    windows = _enforce_window_limits(
        windows,
        target_size,
        token_counter=token_counter,
        max_tokens=max_tokens,
    )
    return _children_from_windows(
        parent_id,
        windows,
        chunk_strategy="dynamic_structure_overlap_v1",
        token_counter=token_counter,
    )


def _split_structural_blocks(
    text: str,
    child_size: int,
    child_overlap: int,
    *,
    token_counter: TokenCounter | None = None,
    max_tokens: int | None = None,
) -> list[_TextBlock]:
    blocks: list[_TextBlock] = []
    for match in re.finditer(r"\S(?:.*(?:\n(?!\s*\n).*)*)", text):
        block_text = match.group(0).strip()
        if not block_text:
            continue
        if _is_markdown_table(block_text):
            if _fits_limits(
                block_text,
                child_size,
                token_counter=token_counter,
                max_tokens=max_tokens,
            ):
                blocks.append(_TextBlock(block_text, match.start(), match.end(), "table"))
            else:
                blocks.extend(
                    _split_markdown_table(
                        block_text,
                        match.start(),
                        child_size,
                        token_counter=token_counter,
                        max_tokens=max_tokens,
                    )
                )
            continue
        for segment in _heading_aware_segments(block_text, match.start()):
            if _fits_limits(
                segment.content,
                child_size,
                token_counter=token_counter,
                max_tokens=max_tokens,
            ):
                blocks.append(segment)
                continue
            blocks.extend(
                _split_long_block(
                    segment.content,
                    segment.start_char,
                    child_size,
                    child_overlap,
                    kind=segment.kind,
                    token_counter=token_counter,
                    max_tokens=max_tokens,
                )
            )
    return blocks


def _heading_aware_segments(text: str, absolute_start: int) -> list[_TextBlock]:
    heading_matches = list(re.finditer(r"(?m)^\s{0,3}#{1,6}\s+.*$", text))
    if not heading_matches:
        stripped = text.strip()
        leading = len(text) - len(text.lstrip())
        return [
            _TextBlock(
                stripped,
                absolute_start + leading,
                absolute_start + leading + len(stripped),
                _block_kind(stripped),
            )
        ] if stripped else []

    segments: list[_TextBlock] = []
    cursor = 0
    for heading_match in heading_matches:
        segments.extend(
            _text_segment(
                text,
                cursor,
                heading_match.start(),
                absolute_start,
                kind="paragraph",
            )
        )
        segments.extend(
            _text_segment(
                text,
                heading_match.start(),
                heading_match.end(),
                absolute_start,
                kind="heading",
            )
        )
        cursor = heading_match.end()
    segments.extend(
        _text_segment(
            text,
            cursor,
            len(text),
            absolute_start,
            kind="paragraph",
        )
    )
    return segments


def _text_segment(
    text: str,
    start: int,
    end: int,
    absolute_start: int,
    *,
    kind: str,
) -> list[_TextBlock]:
    raw = text[start:end]
    content = raw.strip()
    if not content:
        return []
    leading = len(raw) - len(raw.lstrip())
    segment_start = absolute_start + start + leading
    return [
        _TextBlock(
            content,
            segment_start,
            segment_start + len(content),
            kind if kind == "heading" else _block_kind(content),
        )
    ]


def _split_long_block(
    block_text: str,
    absolute_start: int,
    child_size: int,
    child_overlap: int,
    *,
    kind: str = "paragraph",
    token_counter: TokenCounter | None = None,
    max_tokens: int | None = None,
) -> list[_TextBlock]:
    pieces = [piece for piece in re.split(r"(?<=[。！？.!?；;])\s*", block_text) if piece]
    if len(pieces) <= 1:
        return _slice_text_to_limits(
            block_text,
            absolute_start,
            child_size,
            child_overlap,
            kind=kind,
            token_counter=token_counter,
            max_tokens=max_tokens,
        )

    blocks: list[_TextBlock] = []
    current: list[str] = []
    current_start = 0
    offset = 0
    for piece in pieces:
        piece_start = block_text.find(piece, offset)
        if piece_start < 0:
            piece_start = offset
        if not current and not _fits_limits(
            piece,
            child_size,
            token_counter=token_counter,
            max_tokens=max_tokens,
        ):
            blocks.extend(
                _slice_text_to_limits(
                    piece,
                    absolute_start + piece_start,
                    child_size,
                    child_overlap,
                    kind=kind,
                    token_counter=token_counter,
                    max_tokens=max_tokens,
                )
            )
            offset = piece_start + len(piece)
            continue
        candidate = "".join([*current, piece])
        if current and not _fits_limits(
            candidate,
            child_size,
            token_counter=token_counter,
            max_tokens=max_tokens,
        ):
            content = "".join(current).strip()
            blocks.append(
                _TextBlock(
                    content,
                    absolute_start + current_start,
                    absolute_start + current_start + len(content),
                    kind,
                )
            )
            if _fits_limits(
                piece,
                child_size,
                token_counter=token_counter,
                max_tokens=max_tokens,
            ):
                current = [piece]
                current_start = piece_start
            else:
                blocks.extend(
                    _slice_text_to_limits(
                        piece,
                        absolute_start + piece_start,
                        child_size,
                        child_overlap,
                        kind=kind,
                        token_counter=token_counter,
                        max_tokens=max_tokens,
                    )
                )
                current = []
        else:
            if not current:
                current_start = piece_start
            current.append(piece)
        offset = piece_start + len(piece)

    if current:
        content = "".join(current).strip()
        blocks.append(
            _TextBlock(
                content,
                absolute_start + current_start,
                absolute_start + current_start + len(content),
                kind,
            )
        )
    return blocks


def _split_markdown_table(
    block_text: str,
    absolute_start: int,
    child_size: int,
    *,
    token_counter: TokenCounter | None = None,
    max_tokens: int | None = None,
) -> list[_TextBlock]:
    lines = [line for line in block_text.splitlines() if line.strip()]
    if len(lines) <= 2:
        if _fits_limits(
            block_text,
            child_size,
            token_counter=token_counter,
            max_tokens=max_tokens,
        ):
            return [_TextBlock(block_text, absolute_start, absolute_start + len(block_text), "table")]
        return _slice_text_to_limits(
            block_text,
            absolute_start,
            child_size,
            0,
            kind="table",
            token_counter=token_counter,
            max_tokens=max_tokens,
        )

    header_lines = lines[:2]
    data_lines = lines[2:]
    header = "\n".join(header_lines)
    blocks: list[_TextBlock] = []
    current_rows: list[str] = []
    current_start = absolute_start
    search_offset = 0

    for row in data_lines:
        row_offset = block_text.find(row, search_offset)
        if row_offset < 0:
            row_offset = search_offset
        candidate = "\n".join([header, *current_rows, row])
        if not current_rows and not _fits_limits(
            candidate,
            child_size,
            token_counter=token_counter,
            max_tokens=max_tokens,
        ):
            if _fits_limits(
                row,
                child_size,
                token_counter=token_counter,
                max_tokens=max_tokens,
            ):
                blocks.append(
                    _TextBlock(
                        row,
                        absolute_start + row_offset,
                        absolute_start + row_offset + len(row),
                        "table",
                    )
                )
            else:
                blocks.extend(
                    _slice_text_to_limits(
                        row,
                        absolute_start + row_offset,
                        child_size,
                        0,
                        kind="table",
                        token_counter=token_counter,
                        max_tokens=max_tokens,
                    )
                )
            search_offset = row_offset + len(row)
            continue
        if current_rows and not _fits_limits(
            candidate,
            child_size,
            token_counter=token_counter,
            max_tokens=max_tokens,
        ):
            content = "\n".join([header, *current_rows])
            last_row = current_rows[-1]
            last_offset = block_text.find(last_row, max(0, current_start - absolute_start))
            end_char = absolute_start + max(0, last_offset) + len(last_row)
            blocks.append(_TextBlock(content, current_start, end_char, "table"))
            current_rows = []
            single_row_candidate = "\n".join([header, row])
            if _fits_limits(
                single_row_candidate,
                child_size,
                token_counter=token_counter,
                max_tokens=max_tokens,
            ):
                current_rows = [row]
                current_start = absolute_start + row_offset
            elif _fits_limits(
                row,
                child_size,
                token_counter=token_counter,
                max_tokens=max_tokens,
            ):
                blocks.append(
                    _TextBlock(
                        row,
                        absolute_start + row_offset,
                        absolute_start + row_offset + len(row),
                        "table",
                    )
                )
            else:
                blocks.extend(
                    _slice_text_to_limits(
                        row,
                        absolute_start + row_offset,
                        child_size,
                        0,
                        kind="table",
                        token_counter=token_counter,
                        max_tokens=max_tokens,
                    )
                )
        else:
            if not current_rows:
                current_start = absolute_start if not blocks else absolute_start + row_offset
            current_rows.append(row)
        search_offset = row_offset + len(row)

    if current_rows:
        content = "\n".join([header, *current_rows])
        last_row = current_rows[-1]
        last_offset = block_text.find(last_row, max(0, current_start - absolute_start))
        end_char = absolute_start + max(0, last_offset) + len(last_row)
        blocks.append(_TextBlock(content, current_start, end_char, "table"))

    return blocks


def _formal_structure_windows(
    blocks: list[_TextBlock],
    child_size: int,
    *,
    token_counter: TokenCounter | None,
    max_tokens: int | None,
) -> list[list[_TextBlock]]:
    units: list[list[_TextBlock]] = []
    pending_headings: list[_TextBlock] = []
    for block in blocks:
        if block.kind == "heading":
            pending_headings.append(block)
            continue
        unit = [*pending_headings, block]
        if pending_headings and not _fits_limits(
            _blocks_content(unit),
            child_size,
            token_counter=token_counter,
            max_tokens=max_tokens,
        ):
            units.extend([[heading] for heading in pending_headings])
            unit = [block]
        units.append(unit)
        pending_headings = []
    if pending_headings:
        units.extend([[heading] for heading in pending_headings])

    windows: list[list[_TextBlock]] = []
    prose_segment: list[list[_TextBlock]] = []
    for unit in units:
        if any(block.kind == "table" for block in unit):
            windows.extend(
                _two_unit_windows(
                    prose_segment,
                    child_size,
                    token_counter=token_counter,
                    max_tokens=max_tokens,
                )
            )
            prose_segment = []
            windows.append([block for block in unit])
        else:
            prose_segment.append(unit)
    windows.extend(
        _two_unit_windows(
            prose_segment,
            child_size,
            token_counter=token_counter,
            max_tokens=max_tokens,
        )
    )
    return windows


def _two_unit_windows(
    units: list[list[_TextBlock]],
    child_size: int,
    *,
    token_counter: TokenCounter | None,
    max_tokens: int | None,
) -> list[list[_TextBlock]]:
    windows: list[list[_TextBlock]] = []
    start = 0
    while start < len(units):
        current = units[start]
        if start + 1 < len(units):
            paired = [*current, *units[start + 1]]
            if _fits_limits(
                _blocks_content(paired),
                child_size,
                token_counter=token_counter,
                max_tokens=max_tokens,
            ):
                windows.append(paired)
                start += 1
                if start + 1 == len(units):
                    break
                continue
        windows.append(current)
        start += 1
    return windows


def _enforce_window_limits(
    windows: list[list[_TextBlock]],
    child_size: int,
    *,
    token_counter: TokenCounter | None,
    max_tokens: int | None,
) -> list[list[_TextBlock]]:
    safe_windows: list[list[_TextBlock]] = []
    for window in windows:
        if _fits_limits(
            _blocks_content(window),
            child_size,
            token_counter=token_counter,
            max_tokens=max_tokens,
        ):
            safe_windows.append(window)
            continue

        for block in window:
            if _fits_limits(
                block.content,
                child_size,
                token_counter=token_counter,
                max_tokens=max_tokens,
            ):
                safe_windows.append([block])
                continue
            pieces = _slice_text_to_limits(
                block.content,
                block.start_char,
                child_size,
                0 if block.kind == "table" else min(80, max(0, child_size // 8)),
                kind=block.kind,
                token_counter=token_counter,
                max_tokens=max_tokens,
            )
            safe_windows.extend([[piece] for piece in pieces])
    return safe_windows


def _children_from_windows(
    parent_id: str,
    windows: list[list[_TextBlock]],
    *,
    chunk_strategy: str,
    token_counter: TokenCounter | None,
) -> list[ChildDocument]:
    children = []
    previous_end = 0
    for child_index, window in enumerate(windows):
        content = _blocks_content(window)
        if not content:
            continue
        start_char = window[0].start_char
        end_char = window[-1].end_char
        children.append(
            ChildDocument(
                parent_id=parent_id,
                child_id=f"{parent_id}_child_{len(children)}",
                content=content,
                start_char=start_char,
                end_char=end_char,
                child_index=len(children),
                overlap_chars=max(0, previous_end - start_char) if children else 0,
                chunk_strategy=chunk_strategy,
                token_count=_token_count(content, token_counter),
                semantic_unit_count=max(
                    1,
                    sum(1 for block in window if block.kind != "heading"),
                ),
            )
        )
        previous_end = end_char
    return children


def _slice_text_to_limits(
    text: str,
    absolute_start: int,
    child_size: int,
    child_overlap: int,
    *,
    kind: str,
    token_counter: TokenCounter | None,
    max_tokens: int | None,
) -> list[_TextBlock]:
    blocks = []
    start = 0
    while start < len(text):
        upper = min(len(text), start + child_size)
        end = _largest_fitting_end(
            text,
            start,
            upper,
            child_size,
            token_counter=token_counter,
            max_tokens=max_tokens,
        )
        if end <= start:
            end = min(len(text), start + 1)
        content = text[start:end].strip()
        if content:
            blocks.append(
                _TextBlock(
                    content,
                    absolute_start + start,
                    absolute_start + end,
                    kind,
                )
            )
        if end >= len(text):
            break
        overlap = min(child_overlap, max(0, (end - start) // 3))
        start = max(start + 1, end - overlap)
    return blocks


def _largest_fitting_end(
    text: str,
    start: int,
    upper: int,
    child_size: int,
    *,
    token_counter: TokenCounter | None,
    max_tokens: int | None,
) -> int:
    if _fits_limits(
        text[start:upper],
        child_size,
        token_counter=token_counter,
        max_tokens=max_tokens,
    ):
        return upper
    low = start + 1
    high = upper
    best = start
    while low <= high:
        middle = (low + high) // 2
        if _fits_limits(
            text[start:middle],
            child_size,
            token_counter=token_counter,
            max_tokens=max_tokens,
        ):
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


def _fits_limits(
    text: str,
    child_size: int,
    *,
    token_counter: TokenCounter | None,
    max_tokens: int | None,
) -> bool:
    if len(text) > child_size:
        return False
    return max_tokens is None or token_counter is None or token_counter(text) <= max_tokens


def _token_count(text: str, token_counter: TokenCounter | None) -> int:
    return token_counter(text) if token_counter is not None else 0


def _blocks_content(blocks: list[_TextBlock]) -> str:
    return "\n\n".join(block.content for block in blocks if block.content.strip()).strip()


def _block_kind(text: str) -> str:
    stripped = text.lstrip()
    if re.match(r"^#{1,6}\s+", stripped):
        return "heading"
    if re.match(r"^(?:[-*+]\s+|\d+[.)、]\s*)", stripped):
        return "list"
    if re.match(r"^第[一二三四五六七八九十百千万0-9]+条", stripped):
        return "clause"
    return "paragraph"


def _dynamic_child_size(text: str, child_size: int) -> int:
    has_table = "|" in text and "---" in text
    has_headings = bool(re.search(r"(?m)^#{1,6}\s+", text))
    has_numbered_clauses = bool(re.search(r"(?m)^\s*(第[一二三四五六七八九十]+[章节条]|[一二三四五六七八九十]+、|\d+[.、])", text))
    if has_table:
        return min(child_size, max(420, int(child_size * 0.8)))
    if has_headings or has_numbered_clauses:
        return min(child_size, max(480, int(child_size * 0.85)))
    return child_size


def _overlap_block_count(window: list[_TextBlock], child_overlap: int) -> int:
    if not window or child_overlap <= 0:
        return 0
    if _is_markdown_table(window[-1].content):
        return 0
    overlap_chars = 0
    count = 0
    for block in reversed(window):
        overlap_chars += len(block.content)
        count += 1
        if overlap_chars >= child_overlap:
            break
    return max(1, count)


def _is_markdown_table(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return len(lines) >= 2 and lines[0].startswith("|") and any("---" in line for line in lines[1:3])
