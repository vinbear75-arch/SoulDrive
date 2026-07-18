import re
from dataclasses import dataclass


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


@dataclass(frozen=True)
class _TextBlock:
    content: str
    start_char: int
    end_char: int


def split_parent_document(
    parent_id: str,
    text: str,
    child_size: int = 900,
    child_overlap: int = 120,
) -> list[ChildDocument]:
    clean_text = (text or "").strip()
    if not clean_text:
        return []
    if child_size <= 0:
        raise ValueError("child_size must be positive")
    if child_overlap < 0 or child_overlap >= child_size:
        raise ValueError("child_overlap must be smaller than child_size")

    blocks = _split_structural_blocks(clean_text, child_size, child_overlap)
    if not blocks:
        return []

    target_size = _dynamic_child_size(clean_text, child_size)
    min_child_size = max(1, min(target_size // 2, target_size - child_overlap))
    if target_size >= 240:
        min_child_size = max(120, min_child_size)
    children = []
    block_start = 0
    child_index = 0
    previous_end = 0

    while block_start < len(blocks):
        block_end = block_start
        content_parts: list[str] = []
        content_length = 0

        while block_end < len(blocks):
            block = blocks[block_end]
            candidate_length = content_length + len(block.content) + (2 if content_parts else 0)
            if content_parts and content_length >= min_child_size and candidate_length > target_size:
                break
            content_parts.append(block.content)
            content_length = candidate_length
            block_end += 1
            if content_length >= target_size:
                break

        if block_end == block_start:
            block_end += 1
            content_parts.append(blocks[block_start].content)

        window = blocks[block_start:block_end]
        content = "\n\n".join(part for part in content_parts if part.strip()).strip()
        if content:
            start_char = window[0].start_char
            end_char = window[-1].end_char
            children.append(ChildDocument(
                parent_id=parent_id,
                child_id=f"{parent_id}_child_{child_index}",
                content=content,
                start_char=start_char,
                end_char=end_char,
                child_index=child_index,
                overlap_chars=max(0, previous_end - start_char) if child_index else 0,
            ))
            child_index += 1
            previous_end = end_char

        if block_end >= len(blocks):
            break

        overlap_blocks = _overlap_block_count(window, child_overlap)
        next_start = max(block_start + 1, block_end - overlap_blocks)
        block_start = next_start

    return children


def _split_structural_blocks(text: str, child_size: int, child_overlap: int) -> list[_TextBlock]:
    blocks: list[_TextBlock] = []
    for match in re.finditer(r"\S(?:.*(?:\n(?!\s*\n).*)*)", text):
        block_text = match.group(0).strip()
        if not block_text:
            continue
        if len(block_text) <= child_size or _is_markdown_table(block_text):
            blocks.append(_TextBlock(block_text, match.start(), match.end()))
            continue
        blocks.extend(_split_long_block(block_text, match.start(), child_size, child_overlap))
    return blocks


def _split_long_block(block_text: str, absolute_start: int, child_size: int, child_overlap: int) -> list[_TextBlock]:
    pieces = [piece for piece in re.split(r"(?<=[。！？.!?；;])\s*", block_text) if piece]
    if len(pieces) <= 1:
        stride = max(1, child_size - child_overlap)
        return [
            _TextBlock(
                block_text[start : start + child_size].strip(),
                absolute_start + start,
                absolute_start + min(start + child_size, len(block_text)),
            )
            for start in range(0, len(block_text), stride)
            if block_text[start : start + child_size].strip()
        ]

    blocks: list[_TextBlock] = []
    current: list[str] = []
    current_start = 0
    offset = 0
    for piece in pieces:
        piece_start = block_text.find(piece, offset)
        if piece_start < 0:
            piece_start = offset
        candidate = "".join([*current, piece])
        if current and len(candidate) > child_size:
            content = "".join(current).strip()
            blocks.append(
                _TextBlock(
                    content,
                    absolute_start + current_start,
                    absolute_start + current_start + len(content),
                )
            )
            current = [piece]
            current_start = piece_start
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
            )
        )
    return blocks


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
