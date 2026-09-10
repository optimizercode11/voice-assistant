#!/usr/bin/env python3
"""Local retrieval over a configured corpus: SQLite FTS5, so BM25 without numpy.

Why not embeddings
    The host has no numpy and no embedding model, and `/mnt` has 5 GB free.
    A voice turn needs the *right paragraph of my own notes*, not a fuzzy
    vector; FTS5 gives real BM25 with a 40 KB database and no dependency.
    The trade-off is honest and stated in `status()`: this is lexical, so a
    paraphrase with no shared word will miss.

Everything here is offline and read-only with respect to the corpus: the
index is a derived artifact under var/, never a source of truth.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

CHUNK_CHARS = 1200
CHUNK_OVERLAP = 200
SCHEMA_VERSION = 1
SUPPORTED = {".txt", ".md", ".markdown", ".jsonl", ".ndjson", ".csv", ".tsv"}
# A word in an FTS5 query is a quoted prefix term.  Quoting is what keeps a
# query containing ':' or 'NEAR' from being read as query syntax and throwing.
WORD = re.compile(r"[\w]+", re.UNICODE)


class RetrievalError(Exception):
    """A retrieval problem that the caller can turn into a tool error."""


@dataclass
class Hit:
    path: str
    ordinal: int
    text: str
    score: float
    heading: str = ""

    def label(self) -> str:
        return f"{self.path}#chunk{self.ordinal}" + (f" · {self.heading}" if self.heading else "")


def _connect(index: Path) -> sqlite3.Connection:
    index.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(index), timeout=10)
    try:
        connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS probe USING fts5(x)")
    except sqlite3.OperationalError as error:
        raise RetrievalError("this SQLite build has no FTS5; retrieval cannot run") from error
    connection.execute("DROP TABLE probe")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS doc(
            id INTEGER PRIMARY KEY, path TEXT NOT NULL, hash TEXT NOT NULL,
            mtime REAL NOT NULL, size INTEGER NOT NULL, UNIQUE(path));
        CREATE TABLE IF NOT EXISTS chunk(
            id INTEGER PRIMARY KEY, doc_id INTEGER NOT NULL REFERENCES doc(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL, heading TEXT NOT NULL, text TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS chunk_doc ON chunk(doc_id);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
            text, content=chunk, content_rowid=id, tokenize='unicode61 remove_diacritics 2');
        """
    )
    connection.row_factory = sqlite3.Row
    return connection


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:32]


def _rows(path: Path) -> list[str]:
    """Flatten a structured file into one searchable line per record."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        lines = []
        for number, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                lines.append(f"[line {number}] " + json.dumps(json.loads(line), ensure_ascii=False, sort_keys=True))
            except ValueError:
                lines.append(f"[line {number}] {line}")
        return ["\n".join(lines)] if lines else []
    if path.suffix.lower() in {".csv", ".tsv"}:
        import csv
        import io
        rows = list(csv.reader(io.StringIO(text), delimiter="\t" if path.suffix.lower() == ".tsv" else ","))
        header = rows[0] if rows else []
        out = []
        for number, row in enumerate(rows[1:] if header else rows, 1 if header else 0):
            if not any(cell.strip() for cell in row):
                continue
            if header and len(header) == len(row):
                out.append(f"[row {number}] " + "; ".join(f"{h}: {v}" for h, v in zip(header, row) if v.strip()))
            else:
                out.append(f"[row {number}] " + "; ".join(row))
        return ["\n".join(out)] if out else []
    return [text]


def chunk_text(text: str) -> list[tuple[str, str]]:
    """(heading, body) pairs, ~CHUNK_CHARS with overlap, split on blank lines."""
    blocks, heading = [], ""
    for raw in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        raw = raw.strip()
        if not raw:
            continue
        if len(raw) <= 90 and re.match(r"^(#{1,6}\s|\S[^\n]{0,80}\n[=\-]{3,}$)", raw):
            heading = re.sub(r"^#+\s*|[\n=]+$", "", raw).strip()[:120]
        blocks.append(raw)
    chunks, current, current_heading = [], "", heading
    for block in blocks:
        while len(block) > CHUNK_CHARS:                       # a wall of prose
            cut = block.rfind(" ", 0, CHUNK_CHARS) or CHUNK_CHARS
            pieces, block = block[:cut], block[cut + 1:]
            if current:
                chunks.append((current_heading, current))
                current = current[-CHUNK_OVERLAP:].strip() + " " + pieces
            else:
                chunks.append((current_heading, pieces))
                current = ""
        candidate = f"{current}\n\n{block}".strip() if current else block
        if len(candidate) > CHUNK_CHARS and current:
            chunks.append((current_heading, current))
            current = f"{current[-CHUNK_OVERLAP:].strip()} {block}".strip()
        else:
            current = candidate
    if current.strip():
        chunks.append((current_heading, current))
    return chunks


def _sources(sources: list[Path]) -> list[Path]:
    found: list[Path] = []
    for entry in sources:
        entry = Path(entry)
        if entry.is_dir():
            found += sorted(p for p in entry.rglob("*")
                            if p.is_file() and p.suffix.lower() in SUPPORTED and p.stat().st_size <= 8 << 20)
        elif entry.is_file():
            if entry.suffix.lower() in SUPPORTED:
                found.append(entry)
        else:
            raise RetrievalError(f"corpus path does not exist: {entry}")
    seen, unique = set(), []
    for path in found:
        key = str(path.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def fingerprint(sources: list[Path]) -> dict[str, str]:
    return {str(p): _hash(p) for p in _sources(sources)}


def build(sources: list[Path], index: Path) -> dict:
    """Rebuild the index from scratch.  Cheap, and never a partial merge."""
    started = time.monotonic()
    paths = _sources(sources)
    if not paths:
        raise RetrievalError("the configured corpus contains no readable .txt/.md/.jsonl/.csv files")
    manifest = {str(p): _hash(p) for p in paths}
    connection = _connect(index)
    try:
        connection.execute("BEGIN")
        connection.execute("DELETE FROM chunk_fts")
        connection.execute("DELETE FROM chunk")
        connection.execute("DELETE FROM doc")
        total = 0
        for path in paths:
            stat = path.stat()
            label = str(path)
            cursor = connection.execute("INSERT INTO doc(path,hash,mtime,size) VALUES(?,?,?,?)",
                                        (label, manifest[label], stat.st_mtime, stat.st_size))
            doc_id = cursor.lastrowid
            ordinal = 0
            for body in _rows(path):
                for heading, piece in chunk_text(body):
                    connection.execute("INSERT INTO chunk(doc_id,ordinal,heading,text) VALUES(?,?,?,?)",
                                       (doc_id, ordinal, heading, piece))
                    connection.execute("INSERT INTO chunk_fts(rowid,text) VALUES(?,?)",
                                       (connection.execute("SELECT last_insert_rowid()").fetchone()[0], piece))
                    ordinal += 1
                    total += 1
        connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('manifest',?)",
                           (json.dumps(manifest, sort_keys=True),))
        connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('built',?)",
                           (json.dumps({"built_at": int(time.time()), "seconds": round(time.monotonic() - started, 3),
                                        "documents": len(paths), "chunks": total, "schema": SCHEMA_VERSION}),))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"documents": len(paths), "chunks": total, "seconds": round(time.monotonic() - started, 3),
            "index": str(index), "manifest": manifest}


def _match(query: str) -> str:
    terms = [t for t in WORD.findall(query) if len(t) >= 2][:12]
    if not terms:
        return ""
    return " OR ".join(f'"{t}"*' for t in terms)


def search(index: Path, query: str, k: int = 4, max_chars: int = 2400) -> list[Hit]:
    if not Path(index).exists():
        raise RetrievalError(f"no index at {index}; build it first")
    match = _match(query)
    if not match:
        return []
    connection = _connect(Path(index))
    try:
        rows = connection.execute(
            """SELECT doc.path AS path, chunk.ordinal AS ordinal, chunk.heading AS heading,
                      chunk.text AS text, bm25(chunk_fts) AS score
               FROM chunk_fts JOIN chunk ON chunk.id = chunk_fts.rowid
               JOIN doc ON doc.id = chunk.doc_id
               WHERE chunk_fts MATCH ? ORDER BY score LIMIT ?""", (match, max(1, min(8, k)) * 3)).fetchall()
        if not rows:                                                  # no shared word: substring fallback
            needle = f"%{' '.join(WORD.findall(query)[:3]).lower()}%"
            rows = connection.execute(
                """SELECT doc.path AS path, chunk.ordinal AS ordinal, chunk.heading AS heading,
                          chunk.text AS text, -1.0 AS score
                   FROM chunk JOIN doc ON doc.id = chunk.doc_id
                   WHERE lower(chunk.text) LIKE ? LIMIT ?""", (needle, max(1, min(8, k)))).fetchall()
    finally:
        connection.close()
    hits, used, budget = [], set(), max_chars
    for row in rows:
        key = (row["path"], row["ordinal"])
        if key in used:
            continue
        used.add(key)
        text = row["text"]
        if len(text) > budget:
            text = text[:budget] + " […]"
        budget -= len(text)
        hits.append(Hit(row["path"], row["ordinal"], text, row["score"], row["heading"]))
        if not budget or len(hits) >= max(1, min(8, k)):
            break
    return hits


def status(sources: list[Path], index: Path, stale_after: int = 86400) -> dict:
    """What an operator needs before trusting an answer: is this index current?"""
    report: dict = {"index": str(index), "exists": Path(index).exists(), "configured": [str(s) for s in sources]}
    if not report["exists"]:
        report.update(ready=False, reason="no index built yet")
        return report
    connection = _connect(Path(index))
    try:
        built = json.loads((connection.execute("SELECT value FROM meta WHERE key='built'").fetchone() or
                            {"value": "{}"})["value"])
        stored = json.loads((connection.execute("SELECT value FROM meta WHERE key='manifest'").fetchone() or
                            {"value": "{}"})["value"])
    finally:
        connection.close()
    report.update(built)
    try:
        current = fingerprint(sources)
    except RetrievalError as error:
        report.update(ready=False, reason=str(error))
        return report
    added = sorted(set(current) - set(stored))
    removed = sorted(set(stored) - set(current))
    changed = sorted(p for p in set(current) & set(stored) if current[p] != stored[p])
    age = int(time.time()) - int(built.get("built_at", 0))
    report.update(documents=len(current), stale=bool(added or removed or changed), added=added[:5],
                  removed=removed[:5], changed=changed[:5], age_seconds=age,
                  old=bool(age > stale_after), ready=bool(stored) and not (added or removed or changed))
    if not report["ready"] and not report["stale"]:
        report["reason"] = "index is empty"
    elif report["stale"]:
        report["reason"] = "corpus changed since the index was built"
    elif report["old"]:
        report["reason"] = f"index is {age // 3600}h old"
    return report


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, nargs="+", required=True)
    parser.add_argument("--index", type=Path, default=Path("var/notes.sqlite"))
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--query")
    parser.add_argument("--top-k", type=int, default=4)
    arguments = parser.parse_args()
    if arguments.build:
        print(json.dumps(build(arguments.sources, arguments.index), indent=2))
    if arguments.query:
        for hit in search(arguments.index, arguments.query, arguments.top_k):
            print(f"\n=== {hit.label()}  (bm25 {hit.score:.2f}) ===\n{hit.text}")
    if not arguments.build and not arguments.query:
        print(json.dumps(status(arguments.sources, arguments.index), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
