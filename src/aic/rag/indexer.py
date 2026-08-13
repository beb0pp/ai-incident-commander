"""Markdown ingestion for the runbook corpus.

Runbooks are chunked on their ``##`` section headings rather than by a fixed
token window. Operational documents are already organised into "Symptoms",
"Diagnosis", "Mitigation" sections, so heading boundaries produce chunks that
are individually actionable — retrieving half a procedure is worse than
retrieving none.
"""

from __future__ import annotations

import re
from pathlib import Path

from aic.rag.store import Chunk, VectorStore

_H1 = re.compile(r"^#\s+(.*)$", re.MULTILINE)
_H2_SPLIT = re.compile(r"^##\s+", re.MULTILINE)
_STEP = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+(.+)$", re.MULTILINE)


def extract_steps(text: str) -> list[str]:
    """Pull ordered/bulleted lines out of a chunk as discrete procedure steps."""
    return [m.group(1).strip() for m in _STEP.finditer(text)]


def chunk_markdown(document_id: str, content: str) -> list[Chunk]:
    """Split one markdown runbook into per-section chunks."""
    title_match = _H1.search(content)
    doc_title = title_match.group(1).strip() if title_match else document_id

    body = content[title_match.end() :] if title_match else content
    sections = _H2_SPLIT.split(body)

    chunks: list[Chunk] = []
    for index, section in enumerate(sections):
        text = section.strip()
        if not text:
            continue
        heading, _, remainder = text.partition("\n")
        section_title = heading.strip() if index > 0 else "Overview"
        section_body = remainder.strip() if index > 0 else text
        if not section_body:
            continue
        chunks.append(
            Chunk(
                id=f"{document_id}#{index}",
                document_id=document_id,
                title=f"{doc_title} — {section_title}",
                # The document title rides along in the embedded text so a query
                # naming the service matches sections that never repeat the name.
                text=f"{doc_title}\n{section_title}\n{section_body}",
                metadata={"section": section_title, "document_title": doc_title},
            )
        )
    return chunks


def index_directory(store: VectorStore, directory: str | Path, *, pattern: str = "*.md") -> int:
    """Ingest every matching markdown file into ``store``. Returns chunks added."""
    root = Path(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"runbook directory not found: {root}")

    chunks: list[Chunk] = []
    for path in sorted(root.glob(pattern)):
        chunks.extend(chunk_markdown(path.stem, path.read_text(encoding="utf-8")))

    store.add(chunks)
    return len(chunks)
