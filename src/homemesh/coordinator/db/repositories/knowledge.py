"""Document collections, documents, chunks and embedding vectors."""

from __future__ import annotations

from typing import Any

import numpy as np

from homemesh.common.ids import new_id
from homemesh.common.timeutil import now_iso
from homemesh.coordinator.db.database import Row
from homemesh.coordinator.db.repositories.base import Repo, decode, jdump
from homemesh.coordinator.db.repositories.conversations import fts_query


class CollectionRepo(Repo):
    async def create(self, user_id: str, node_id: str, name: str, root_path: str, *, include_globs: list[str],
                     exclude_dirs: list[str], project_id: str | None = None) -> Row:
        cid = new_id("col")
        now = now_iso()
        await self.db.execute(
            "INSERT INTO collections(id, user_id, project_id, node_id, name, root_path, include_globs, exclude_dirs,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cid, user_id, project_id, node_id, name, root_path, jdump(include_globs), jdump(exclude_dirs), now, now))
        return await self.get(cid)  # type: ignore[return-value]

    async def get(self, collection_id: str, user_id: str | None = None) -> Row | None:
        if user_id:
            row = await self.db.fetchone("SELECT * FROM collections WHERE id=? AND user_id=?", (collection_id, user_id))
        else:
            row = await self.db.fetchone("SELECT * FROM collections WHERE id=?", (collection_id,))
        return decode(row, include_globs=[], exclude_dirs=[])

    async def list(self, user_id: str) -> list[Row]:
        rows = await self.db.fetchall("SELECT * FROM collections WHERE user_id=? ORDER BY name", (user_id,))
        return [decode(r, include_globs=[], exclude_dirs=[]) for r in rows]  # type: ignore[misc]

    async def roots_for_node(self, node_id: str) -> list[str]:
        rows = await self.db.fetchall("SELECT root_path FROM collections WHERE node_id=?", (node_id,))
        return [r["root_path"] for r in rows]

    async def update(self, collection_id: str, **fields: Any) -> None:
        sets, vals = [], []
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(jdump(v) if k in ("include_globs", "exclude_dirs") else v)
        sets.append("updated_at=?")
        vals.append(now_iso())
        await self.db.execute(f"UPDATE collections SET {', '.join(sets)} WHERE id=?", (*vals, collection_id))

    async def refresh_counts(self, collection_id: str, embed_model: str | None) -> dict[str, int]:
        def _q(conn: Any) -> dict[str, int]:
            docs = conn.execute("SELECT COUNT(*) FROM documents WHERE collection_id=?", (collection_id,)).fetchone()[0]
            chunks = conn.execute("SELECT COUNT(*) FROM chunks WHERE collection_id=?", (collection_id,)).fetchone()[0]
            embedded = 0
            if embed_model:
                embedded = conn.execute(
                    "SELECT COUNT(*) FROM embeddings e JOIN chunks c ON c.id = e.owner_id"
                    " WHERE e.owner_type='chunk' AND e.model=? AND c.collection_id=?",
                    (embed_model, collection_id)).fetchone()[0]
            conn.execute("UPDATE collections SET doc_count=?, chunk_count=?, embedded_count=?, updated_at=? WHERE id=?",
                         (docs, chunks, embedded, now_iso(), collection_id))
            return {"doc_count": docs, "chunk_count": chunks, "embedded_count": embedded}

        return await self.db.run(_q)

    async def delete(self, collection_id: str) -> None:
        def _tx(conn: Any) -> None:
            conn.execute("DELETE FROM embeddings WHERE owner_type='chunk' AND owner_id IN"
                         " (SELECT id FROM chunks WHERE collection_id=?)", (collection_id,))
            conn.execute("DELETE FROM chunks WHERE collection_id=?", (collection_id,))
            conn.execute("DELETE FROM collections WHERE id=?", (collection_id,))

        await self.db.transaction(_tx)


class DocumentRepo(Repo):
    async def known_state(self, collection_id: str) -> dict[str, dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT rel_path, size, mtime, content_hash FROM documents WHERE collection_id=? AND status='indexed'",
            (collection_id,))
        return {r["rel_path"]: {"size": r["size"], "mtime": r["mtime"], "content_hash": r["content_hash"] or ""}
                for r in rows}

    async def upsert_document(self, collection_id: str, rec: dict[str, Any]) -> tuple[str, list[str]]:
        """Insert/replace a document and (for part 0) its chunks. Returns (document_id, new chunk ids)."""
        now = now_iso()

        def _tx(conn: Any) -> tuple[str, list[str]]:
            row = conn.execute("SELECT id FROM documents WHERE collection_id=? AND rel_path=?",
                               (collection_id, rec["rel_path"])).fetchone()
            if row:
                doc_id = row[0]
                if rec.get("part", 0) == 0:
                    conn.execute("DELETE FROM embeddings WHERE owner_type='chunk' AND owner_id IN"
                                 " (SELECT id FROM chunks WHERE document_id=?)", (doc_id,))
                    conn.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
                conn.execute(
                    "UPDATE documents SET size=?, mtime=?, content_hash=?, mime=?, status=?, error=?, page_count=?,"
                    " indexed_at=? WHERE id=?",
                    (rec["size"], rec["mtime"], rec.get("content_hash"), rec.get("mime"), rec["status"],
                     rec.get("error"), rec.get("page_count"), now, doc_id))
            else:
                doc_id = new_id("doc")
                conn.execute(
                    "INSERT INTO documents(id, collection_id, rel_path, size, mtime, content_hash, mime, status, error,"
                    " page_count, indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (doc_id, collection_id, rec["rel_path"], rec["size"], rec["mtime"], rec.get("content_hash"),
                     rec.get("mime"), rec["status"], rec.get("error"), rec.get("page_count"), now))
            chunk_ids = []
            for ch in rec.get("chunks", []):
                cid = new_id("chk")
                chunk_ids.append(cid)
                conn.execute(
                    "INSERT INTO chunks(id, document_id, collection_id, ordinal, content, start_line, end_line, page,"
                    " char_start, char_end) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (cid, doc_id, collection_id, ch["ordinal"], ch["content"], ch.get("start_line"),
                     ch.get("end_line"), ch.get("page"), ch.get("char_start"), ch.get("char_end")))
            n = conn.execute("SELECT COUNT(*) FROM chunks WHERE document_id=?", (doc_id,)).fetchone()[0]
            conn.execute("UPDATE documents SET chunk_count=? WHERE id=?", (n, doc_id))
            return doc_id, chunk_ids

        return await self.db.transaction(_tx)

    async def touch_unchanged(self, collection_id: str, rel_path: str, mtime: float) -> None:
        await self.db.execute("UPDATE documents SET mtime=?, indexed_at=? WHERE collection_id=? AND rel_path=?",
                              (mtime, now_iso(), collection_id, rel_path))

    async def remove_missing(self, collection_id: str, seen: list[str]) -> int:
        seen_set = set(seen)

        def _tx(conn: Any) -> int:
            rows = conn.execute("SELECT id, rel_path FROM documents WHERE collection_id=?", (collection_id,)).fetchall()
            removed = 0
            for doc_id, rel in rows:
                if rel not in seen_set:
                    conn.execute("DELETE FROM embeddings WHERE owner_type='chunk' AND owner_id IN"
                                 " (SELECT id FROM chunks WHERE document_id=?)", (doc_id,))
                    conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
                    removed += 1
            return removed

        return await self.db.transaction(_tx)

    async def list(self, collection_id: str, limit: int = 1000, offset: int = 0) -> list[Row]:
        return await self.db.fetchall(
            "SELECT id, rel_path, size, mtime, content_hash, mime, status, error, page_count, chunk_count, indexed_at"
            " FROM documents WHERE collection_id=? ORDER BY rel_path LIMIT ? OFFSET ?", (collection_id, limit, offset))

    async def status_counts(self, collection_id: str) -> dict[str, int]:
        rows = await self.db.fetchall("SELECT status, COUNT(*) AS n FROM documents WHERE collection_id=?"
                                      " GROUP BY status", (collection_id,))
        return {r["status"]: r["n"] for r in rows}


class ChunkRepo(Repo):
    async def get_many(self, chunk_ids: list[str]) -> list[Row]:
        if not chunk_ids:
            return []
        rows = await self.db.fetchall(
            "SELECT c.*, d.rel_path, d.mime, col.name AS collection_name, col.root_path, col.node_id"
            f" FROM chunks c JOIN documents d ON d.id = c.document_id JOIN collections col ON col.id = c.collection_id"
            f" WHERE c.id IN ({','.join('?' * len(chunk_ids))})", chunk_ids)
        order = {cid: i for i, cid in enumerate(chunk_ids)}
        return sorted(rows, key=lambda r: order.get(r["id"], 0))

    async def search_fts(self, text: str, collection_ids: list[str], limit: int = 50) -> list[Row]:
        q = fts_query(text)
        if not q or not collection_ids:
            return []
        return await self.db.fetchall(
            "SELECT c.id, bm25(chunks_fts) AS score FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid"
            f" WHERE chunks_fts MATCH ? AND c.collection_id IN ({','.join('?' * len(collection_ids))})"
            " ORDER BY rank LIMIT ?", (q, *collection_ids, limit))

    async def missing_embeddings(self, model: str, collection_id: str | None = None, limit: int = 1000) -> list[Row]:
        sql = ("SELECT c.id, c.content FROM chunks c LEFT JOIN embeddings e ON e.owner_type='chunk'"
               " AND e.owner_id=c.id AND e.model=? WHERE e.owner_id IS NULL")
        params: list[Any] = [model]
        if collection_id:
            sql += " AND c.collection_id=?"
            params.append(collection_id)
        sql += " ORDER BY c.collection_id, c.document_id, c.ordinal LIMIT ?"
        params.append(limit)
        return await self.db.fetchall(sql, params)


class EmbeddingRepo(Repo):
    async def store(self, owner_type: str, items: list[tuple[str, list[float]]], model: str) -> int:
        now = now_iso()
        rows = []
        for owner_id, vec in items:
            arr = np.asarray(vec, dtype=np.float32)
            rows.append((owner_type, owner_id, model, int(arr.shape[0]), arr.tobytes(), now))

        def _tx(conn: Any) -> int:
            # Only store vectors whose owner still exists (a re-index may have replaced the chunk meanwhile).
            table = "chunks" if owner_type == "chunk" else "memories"
            stored = 0
            for r in rows:
                if conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (r[1],)).fetchone():
                    conn.execute(
                        "INSERT OR REPLACE INTO embeddings(owner_type, owner_id, model, dim, vector, created_at)"
                        " VALUES (?,?,?,?,?,?)", r)
                    stored += 1
            return stored

        return await self.db.transaction(_tx)

    async def load_matrix(self, owner_type: str, model: str, collection_ids: list[str] | None = None,
                          user_id: str | None = None) -> tuple[list[str], np.ndarray | None]:
        def _q(conn: Any) -> tuple[list[str], np.ndarray | None]:
            if owner_type == "chunk":
                sql = ("SELECT e.owner_id, e.dim, e.vector FROM embeddings e JOIN chunks c ON c.id = e.owner_id"
                       " WHERE e.owner_type='chunk' AND e.model=?")
                params: list[Any] = [model]
                if collection_ids is not None:
                    if not collection_ids:
                        return [], None
                    sql += f" AND c.collection_id IN ({','.join('?' * len(collection_ids))})"
                    params += collection_ids
            else:
                sql = ("SELECT e.owner_id, e.dim, e.vector FROM embeddings e JOIN memories m ON m.id = e.owner_id"
                       " WHERE e.owner_type='memory' AND e.model=? AND m.status='active'")
                params = [model]
                if user_id:
                    sql += " AND m.user_id=?"
                    params.append(user_id)
            rows = conn.execute(sql, params).fetchall()
            if not rows:
                return [], None
            dim = rows[0][1]
            ids = [r[0] for r in rows if r[1] == dim]
            mat = np.frombuffer(b"".join(r[2] for r in rows if r[1] == dim), dtype=np.float32).reshape(len(ids), dim)
            return ids, mat

        return await self.db.run(_q)

    async def count(self, owner_type: str, model: str) -> int:
        return int(await self.db.fetchval("SELECT COUNT(*) FROM embeddings WHERE owner_type=? AND model=?",
                                          (owner_type, model)))

    async def models(self) -> list[Row]:
        return await self.db.fetchall("SELECT owner_type, model, dim, COUNT(*) AS n FROM embeddings"
                                      " GROUP BY owner_type, model, dim")
