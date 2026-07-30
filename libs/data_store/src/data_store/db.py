"""Storage layer for the data-store service, backed by rqlite.

rqlite is SQLite with an HTTP API and Raft-based durability, so all storage
access here is HTTP to a local ``rqlited`` process rather than a file handle.
The data model is unchanged: named *collections* of arbitrary-JSON *documents*
with a string id and created/updated timestamps, stored verbatim.

Every statement is sent as an rqlite parameterized statement -- ``[sql, p1, p2,
...]`` -- so caller-supplied values are never interpolated into SQL text; the
service still exposes only CRUD, never raw SQL.
"""

import json
import uuid
from datetime import datetime
from datetime import timezone
from typing import Any

import httpx

_TIMEOUT_SECONDS = 30.0
# Reads use strong consistency so a value written a moment ago is visible.
_QUERY_QUERY_STRING = "associative&level=strong"


def _utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string (timezone-aware)."""
    return datetime.now(timezone.utc).isoformat()


def _execute(base_url: str, statements: list[list[Any]]) -> list[dict[str, Any]]:
    """Run one or more write statements in a single transaction.

    ``statements`` is a list of ``[sql, *params]``. Returns the per-statement
    result dicts (each may carry an ``error`` key, which callers inspect).
    """
    response = httpx.post(
        f"{base_url}/db/execute?transaction",
        json=statements,
        timeout=_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json().get("results", [])


def _query(base_url: str, sql: str, *params: Any) -> list[dict[str, Any]]:
    """Run a single read statement and return its rows as dicts."""
    response = httpx.post(
        f"{base_url}/db/query?{_QUERY_QUERY_STRING}",
        json=[[sql, *params]],
        timeout=_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    result = response.json()["results"][0]
    if "error" in result:
        raise RqliteError(result["error"])
    return result.get("rows", [])


def initialize_database(base_url: str) -> None:
    """Create the schema if it does not yet exist.

    Uses a connection-retrying client so this tolerates rqlite still opening its
    port at startup (httpx backs off between connect attempts internally, so no
    manual sleep loop is needed).
    """
    documents_table = """
        CREATE TABLE IF NOT EXISTS documents (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            collection  TEXT NOT NULL,
            doc_id      TEXT NOT NULL,
            body        TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            UNIQUE(collection, doc_id)
        )
    """
    audit_table = """
        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            method      TEXT NOT NULL,
            path        TEXT NOT NULL,
            collection  TEXT,
            doc_id      TEXT,
            status      INTEGER NOT NULL,
            authed      INTEGER NOT NULL,
            remote      TEXT
        )
    """
    statements = [
        [documents_table],
        ["CREATE INDEX IF NOT EXISTS idx_documents_collection ON documents(collection)"],
        [audit_table],
        ["CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)"],
    ]
    transport = httpx.HTTPTransport(retries=10)
    with httpx.Client(transport=transport, timeout=_TIMEOUT_SECONDS) as client:
        response = client.post(f"{base_url}/db/execute?transaction", json=statements)
        response.raise_for_status()
        results = response.json().get("results", [])
    for result in results:
        if "error" in result:
            raise RqliteError(result["error"])


def _row_to_document(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a document row into the API-facing shape (body parsed back to JSON)."""
    return {
        "id": row["doc_id"],
        "collection": row["collection"],
        "body": json.loads(row["body"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_collections(base_url: str) -> list[dict[str, Any]]:
    """Return each collection name with its document count."""
    rows = _query(
        base_url,
        "SELECT collection, COUNT(*) AS count FROM documents GROUP BY collection ORDER BY collection",
    )
    return [{"name": row["collection"], "count": row["count"]} for row in rows]


def list_documents(base_url: str, collection: str, limit: int, offset: int) -> dict[str, Any]:
    """Return a page of documents in a collection plus the total count."""
    count_rows = _query(
        base_url,
        "SELECT COUNT(*) AS count FROM documents WHERE collection = ?",
        collection,
    )
    total = count_rows[0]["count"] if count_rows else 0
    rows = _query(
        base_url,
        """
        SELECT collection, doc_id, body, created_at, updated_at
        FROM documents
        WHERE collection = ?
        ORDER BY updated_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        collection,
        limit,
        offset,
    )
    return {
        "collection": collection,
        "total": total,
        "limit": limit,
        "offset": offset,
        "documents": [_row_to_document(row) for row in rows],
    }


def get_document(base_url: str, collection: str, doc_id: str) -> dict[str, Any] | None:
    """Return a single document, or None if it does not exist."""
    rows = _query(
        base_url,
        """
        SELECT collection, doc_id, body, created_at, updated_at
        FROM documents
        WHERE collection = ? AND doc_id = ?
        """,
        collection,
        doc_id,
    )
    return _row_to_document(rows[0]) if rows else None


def create_document(base_url: str, collection: str, body: Any, doc_id: str | None) -> dict[str, Any]:
    """Insert a new document. Generates a uuid id when none is supplied.

    Raises ``DocumentConflictError`` if the given id already exists in the
    collection.
    """
    resolved_id = doc_id if doc_id else uuid.uuid4().hex
    now = _utc_now_iso()
    serialized = json.dumps(body)
    results = _execute(
        base_url,
        [
            [
                "INSERT INTO documents (collection, doc_id, body, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                collection,
                resolved_id,
                serialized,
                now,
                now,
            ]
        ],
    )
    result = results[0]
    if "error" in result:
        if "UNIQUE constraint failed" in result["error"]:
            raise DocumentConflictError(collection, resolved_id)
        raise RqliteError(result["error"])
    return {
        "id": resolved_id,
        "collection": collection,
        "body": body,
        "created_at": now,
        "updated_at": now,
    }


def put_document(base_url: str, collection: str, doc_id: str, body: Any) -> dict[str, Any]:
    """Create or replace a document at a known id (upsert), preserving created_at."""
    now = _utc_now_iso()
    serialized = json.dumps(body)
    # ON CONFLICT keeps the original created_at (only body/updated_at change).
    results = _execute(
        base_url,
        [
            [
                """
                INSERT INTO documents (collection, doc_id, body, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(collection, doc_id)
                DO UPDATE SET body = excluded.body, updated_at = excluded.updated_at
                """,
                collection,
                doc_id,
                serialized,
                now,
                now,
            ]
        ],
    )
    result = results[0]
    if "error" in result:
        raise RqliteError(result["error"])
    stored = get_document(base_url, collection, doc_id)
    if stored is None:
        raise RqliteError("document vanished immediately after upsert")
    return stored


def delete_document(base_url: str, collection: str, doc_id: str) -> bool:
    """Delete a document. Returns True if a row was removed."""
    results = _execute(
        base_url,
        [["DELETE FROM documents WHERE collection = ? AND doc_id = ?", collection, doc_id]],
    )
    result = results[0]
    if "error" in result:
        raise RqliteError(result["error"])
    return result.get("rows_affected", 0) > 0


def delete_collection(base_url: str, collection: str) -> int:
    """Delete every document in a collection. Returns the number removed."""
    results = _execute(base_url, [["DELETE FROM documents WHERE collection = ?", collection]])
    result = results[0]
    if "error" in result:
        raise RqliteError(result["error"])
    return result.get("rows_affected", 0)


def record_audit(
    base_url: str,
    method: str,
    path: str,
    collection: str | None,
    doc_id: str | None,
    status: int,
    authed: bool,
    remote: str | None,
) -> None:
    """Append one row to the audit log."""
    _execute(
        base_url,
        [
            [
                "INSERT INTO audit_log (ts, method, path, collection, doc_id, status, authed, remote) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                _utc_now_iso(),
                method,
                path,
                collection,
                doc_id,
                status,
                1 if authed else 0,
                remote,
            ]
        ],
    )


def list_audit(base_url: str, limit: int) -> list[dict[str, Any]]:
    """Return the most recent audit-log entries, newest first."""
    rows = _query(
        base_url,
        "SELECT ts, method, path, collection, doc_id, status, authed, remote FROM audit_log ORDER BY id DESC LIMIT ?",
        limit,
    )
    return [
        {
            "ts": row["ts"],
            "method": row["method"],
            "path": row["path"],
            "collection": row["collection"],
            "doc_id": row["doc_id"],
            "status": row["status"],
            "authed": bool(row["authed"]),
            "remote": row["remote"],
        }
        for row in rows
    ]


class RqliteError(Exception):
    """Raised when rqlite returns an error for a statement."""


class DocumentConflictError(Exception):
    """Raised when creating a document whose id already exists in the collection."""

    def __init__(self, collection: str, doc_id: str) -> None:
        super().__init__(f"document {doc_id!r} already exists in collection {collection!r}")
        self.collection = collection
        self.doc_id = doc_id
