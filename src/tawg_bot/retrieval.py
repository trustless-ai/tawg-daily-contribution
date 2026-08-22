"""Disposable deterministic BM25 retrieval over vault pages and source records."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from tawg_bot.models import SourceRecord
from tawg_bot.storage import JsonlCollection
from tawg_bot.vault import parse_frontmatter

_TOKEN_PART = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*|[\u3400-\u9fff]+", re.I)
_CJK = re.compile(r"^[\u3400-\u9fff]+$")


@dataclass(frozen=True, slots=True)
class IndexStats:
    source_count: int
    chunk_count: int
    source_state_sha256: str


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    chunk_id: str
    path: str
    text: str
    score: float
    mode: str
    record_id: str | None = None
    source_locator: str | None = None


@dataclass(frozen=True, slots=True)
class _Chunk:
    chunk_id: str
    path: str
    index: int
    text: str
    source_sha256: str
    tokens: tuple[str, ...]
    record_id: str | None = None
    source_locator: str | None = None


class VaultRetriever:
    _SCHEMA = "tawg.bm25-index.v1"
    _SOURCE_PATTERNS = (
        "data/telegram/**/*.jsonl",
        "data/github/**/*.jsonl",
        "data/magicians/**/*.jsonl",
    )

    def __init__(self, root: Path, *, max_chunk_chars: int = 1200) -> None:
        if max_chunk_chars < 64:
            raise ValueError("max_chunk_chars must be at least 64")
        self.root = root.resolve()
        self.max_chunk_chars = max_chunk_chars
        self.index_path = self.root / ".vault-meta/bm25.json"

    def build(self) -> IndexStats:
        chunks, state = self._current_chunks()
        document_frequencies: Counter[str] = Counter()
        for chunk in chunks:
            document_frequencies.update(set(chunk.tokens))
        average_length = (
            sum(len(chunk.tokens) for chunk in chunks) / len(chunks) if chunks else 0.0
        )
        payload = {
            "schema": self._SCHEMA,
            "source_state_sha256": state,
            "average_length": average_length,
            "document_frequencies": dict(sorted(document_frequencies.items())),
            "chunks": [self._chunk_json(chunk) for chunk in chunks],
        }
        encoded = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode()
        self._prepare_cache_directory()
        descriptor, temporary = tempfile.mkstemp(prefix="bm25-", dir=self.index_path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
            os.replace(temporary, self.index_path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return IndexStats(self._source_count(), len(chunks), state)

    def query(self, text: str, *, top_k: int = 8) -> list[RetrievedChunk]:
        normalized = " ".join(text.split())
        if not normalized or len(normalized) > 8000:
            raise ValueError("query must contain between 1 and 8000 normalized characters")
        if not 1 <= top_k <= 1000:
            raise ValueError("top_k must be between 1 and 1000")
        index = self._load_index()
        if index is None:
            chunks, _ = self._current_chunks()
            return self._fallback(chunks, normalized, top_k)
        chunks, document_frequencies, average_length = index
        query_terms = _tokens(normalized)
        total = len(chunks)
        scored: list[tuple[float, _Chunk]] = []
        for chunk in chunks:
            frequencies = Counter(chunk.tokens)
            score = 0.0
            for term in query_terms:
                frequency = frequencies[term]
                if not frequency:
                    continue
                document_frequency = document_frequencies.get(term, 0)
                inverse = math.log(
                    1 + (total - document_frequency + 0.5) / (document_frequency + 0.5)
                )
                denominator = frequency + 1.2 * (
                    1 - 0.75 + 0.75 * len(chunk.tokens) / max(average_length, 1.0)
                )
                score += inverse * (frequency * 2.2 / denominator)
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda item: (-item[0], item[1].path, item[1].chunk_id))
        return [self._result(chunk, score, "bm25") for score, chunk in scored[:top_k]]

    def preview_chunks(self) -> tuple[tuple[str, str], ...]:
        chunks, _ = self._current_chunks()
        return tuple((chunk.chunk_id, chunk.text) for chunk in chunks)

    def _fallback(self, chunks: list[_Chunk], query: str, top_k: int) -> list[RetrievedChunk]:
        query_terms = set(_tokens(query))
        normalized_query = query.casefold()
        scored: list[tuple[float, _Chunk]] = []
        for chunk in chunks:
            overlap = len(query_terms.intersection(chunk.tokens))
            exact = 2 if normalized_query in chunk.text.casefold() else 0
            score = float(overlap + exact)
            if score:
                scored.append((score, chunk))
        scored.sort(key=lambda item: (-item[0], item[1].path, item[1].chunk_id))
        return [
            self._result(chunk, score, "text-fallback") for score, chunk in scored[:top_k]
        ]

    def _load_index(
        self,
    ) -> tuple[list[_Chunk], dict[str, int], float] | None:
        if self.index_path.is_symlink():
            return None
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schema") != self._SCHEMA:
                return None
            current_chunks, current_state = self._current_chunks()
            if payload.get("source_state_sha256") != current_state:
                return None
            raw_chunks = payload.get("chunks")
            raw_frequencies = payload.get("document_frequencies")
            average_length = payload.get("average_length")
            if (
                not isinstance(raw_chunks, list)
                or not isinstance(raw_frequencies, dict)
                or not isinstance(average_length, int | float)
            ):
                return None
            chunks = [self._chunk_from_json(item) for item in raw_chunks]
            if any(chunk is None for chunk in chunks):
                return None
            frequencies = {
                key: value
                for key, value in raw_frequencies.items()
                if isinstance(key, str) and isinstance(value, int)
            }
            if len(frequencies) != len(raw_frequencies):
                return None
            valid_chunks = [chunk for chunk in chunks if chunk is not None]
            if valid_chunks != current_chunks:
                return None
            return valid_chunks, frequencies, float(average_length)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return None

    def _current_chunks(self) -> tuple[list[_Chunk], str]:
        chunks: list[_Chunk] = []
        source_state: list[tuple[str, str]] = []
        for path in self._source_paths():
            relative = path.relative_to(self.root).as_posix()
            source_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            source_state.append((relative, source_sha))
            if relative.startswith("knowledge/"):
                text = path.read_text(encoding="utf-8")
                _, body = parse_frontmatter(text)
                chunks.extend(self._split(relative, body, source_sha))
                continue
            collection = JsonlCollection(path, SourceRecord)
            for record in collection.decode(path.read_bytes()):
                record_chunks = self._split(
                    relative,
                    record.text_original,
                    source_sha,
                    record_id=record.record_id,
                    source_locator=record.source_locator,
                )
                chunks.extend(record_chunks)
        chunks.sort(key=lambda item: (item.path, item.record_id or "", item.index, item.chunk_id))
        state_payload = json.dumps(source_state, separators=(",", ":"), ensure_ascii=False).encode()
        return chunks, hashlib.sha256(state_payload).hexdigest()

    def _source_paths(self) -> list[Path]:
        paths = {
            path
            for path in (self.root / "knowledge").rglob("*.md")
            if self._safe_source_path(path)
        }
        for pattern in self._SOURCE_PATTERNS:
            paths.update(path for path in self.root.glob(pattern) if self._safe_source_path(path))
        return sorted(paths)

    def _safe_source_path(self, path: Path) -> bool:
        return (
            path.is_file()
            and not path.is_symlink()
            and path.resolve().is_relative_to(self.root)
            and path.relative_to(self.root).parts[0] in {"knowledge", "data"}
        )

    def _split(
        self,
        path: str,
        text: str,
        source_sha: str,
        *,
        record_id: str | None = None,
        source_locator: str | None = None,
    ) -> list[_Chunk]:
        paragraphs = [" ".join(part.split()) for part in re.split(r"\n\s*\n", text) if part.strip()]
        pieces: list[str] = []
        for paragraph in paragraphs:
            pieces.extend(self._bounded_pieces(paragraph))
        chunks: list[_Chunk] = []
        for index, piece in enumerate(pieces):
            chunk_id = _chunk_id(path, record_id, index, piece)
            chunks.append(
                _Chunk(
                    chunk_id=chunk_id,
                    path=path,
                    index=index,
                    text=piece,
                    source_sha256=source_sha,
                    tokens=tuple(_tokens(piece)),
                    record_id=record_id,
                    source_locator=source_locator,
                )
            )
        return chunks

    def _bounded_pieces(self, paragraph: str) -> list[str]:
        if len(paragraph) <= self.max_chunk_chars:
            return [paragraph]
        words = paragraph.split(" ")
        if len(words) == 1:
            return [
                paragraph[start : start + self.max_chunk_chars]
                for start in range(0, len(paragraph), self.max_chunk_chars)
            ]
        pieces: list[str] = []
        current = ""
        for word in words:
            if len(word) > self.max_chunk_chars:
                if current:
                    pieces.append(current)
                    current = ""
                pieces.extend(
                    word[start : start + self.max_chunk_chars]
                    for start in range(0, len(word), self.max_chunk_chars)
                )
                continue
            candidate = word if not current else f"{current} {word}"
            if len(candidate) <= self.max_chunk_chars:
                current = candidate
                continue
            if current:
                pieces.append(current)
            current = word
        if current:
            pieces.append(current)
        return pieces

    def _chunk_json(self, chunk: _Chunk) -> dict[str, Any]:
        return {
            "chunk_id": chunk.chunk_id,
            "path": chunk.path,
            "index": chunk.index,
            "text": chunk.text,
            "source_sha256": chunk.source_sha256,
            "tokens": list(chunk.tokens),
            "record_id": chunk.record_id,
            "source_locator": chunk.source_locator,
        }

    def _chunk_from_json(self, raw: object) -> _Chunk | None:
        if not isinstance(raw, dict):
            return None
        try:
            path = raw["path"]
            index = raw["index"]
            text = raw["text"]
            source_sha = raw["source_sha256"]
            tokens = raw["tokens"]
            chunk_id = raw["chunk_id"]
        except KeyError:
            return None
        if (
            not isinstance(path, str)
            or not isinstance(index, int)
            or not isinstance(text, str)
            or not isinstance(source_sha, str)
            or not isinstance(tokens, list)
            or not all(isinstance(token, str) for token in tokens)
            or not isinstance(chunk_id, str)
        ):
            return None
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            return None
        source_path = self.root.joinpath(*relative.parts)
        if not self._safe_source_path(source_path):
            return None
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_sha:
            return None
        record_id = raw.get("record_id")
        locator = raw.get("source_locator")
        if record_id is not None and not isinstance(record_id, str):
            return None
        if locator is not None and not isinstance(locator, str):
            return None
        expected_id = _chunk_id(path, record_id, index, text)
        if chunk_id != expected_id or tuple(tokens) != tuple(_tokens(text)):
            return None
        return _Chunk(
            chunk_id=chunk_id,
            path=path,
            index=index,
            text=text,
            source_sha256=source_sha,
            tokens=tuple(tokens),
            record_id=record_id,
            source_locator=locator,
        )

    def _source_count(self) -> int:
        return len(self._source_paths())

    def _prepare_cache_directory(self) -> None:
        cache_directory = self.index_path.parent
        if cache_directory.is_symlink():
            raise ValueError("derived cache directory cannot be a symlink")
        cache_directory.mkdir(parents=True, exist_ok=True)
        if not cache_directory.resolve().is_relative_to(self.root):
            raise ValueError("derived cache directory escapes repository root")

    @staticmethod
    def _result(chunk: _Chunk, score: float, mode: str) -> RetrievedChunk:
        return RetrievedChunk(
            chunk_id=chunk.chunk_id,
            path=chunk.path,
            text=chunk.text,
            score=score,
            mode=mode,
            record_id=chunk.record_id,
            source_locator=chunk.source_locator,
        )


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _TOKEN_PART.finditer(text.casefold()):
        value = match.group(0)
        if _CJK.fullmatch(value):
            characters = list(value)
            tokens.extend(characters)
            tokens.extend(
                characters[index] + characters[index + 1]
                for index in range(len(characters) - 1)
            )
        else:
            tokens.append(value)
    return tokens


def _chunk_id(path: str, record_id: str | None, index: int, text: str) -> str:
    payload = f"{path}\0{record_id or ''}\0{index}\0{text}".encode()
    return hashlib.sha256(payload).hexdigest()
