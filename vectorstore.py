"""
Vector store module.
Uses ChromaDB (local, free) for semantic storage and retrieval.

Collections:
  rules       — learned behavior rules with training/live mode
  knowledge   — runbooks, KB articles (future: SharePoint sync)

Embeddings are generated via Azure OpenAI (text-embedding-3-small).
ChromaDB persists to disk at CHROMA_PATH (default: ./chroma_db).
"""

import os
import uuid
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

import chromadb
from openai import OpenAI

log = logging.getLogger(__name__)

CHROMA_PATH = os.environ.get("CHROMA_PATH", "./chroma_db")
EMBEDDING_MODEL = "text-embedding-3-small"

_embed_client = OpenAI(
    base_url=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
)


def embed(text: str) -> list[float]:
    response = _embed_client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=text,
    )
    return response.data[0].embedding


_chroma_client = None


def get_client():
    global _chroma_client
    if _chroma_client is None:
        _chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)
        log.info(f"ChromaDB initialized at {CHROMA_PATH}")
    return _chroma_client


def get_collection(name: str):
    return get_client().get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"},
    )


# ---------------------------------------------------------------------------
# Rules collection
# ---------------------------------------------------------------------------

def add_rule(pattern: str, action: str, note: str = "", urgency: str = "medium", mode: str = "training") -> str:
    """Store a new rule. mode: 'training' or 'live'."""
    collection = get_collection("rules")
    rule_id = str(uuid.uuid4())[:8]

    text_to_embed = f"{pattern} {action} {note}".strip()
    embedding = embed(text_to_embed)

    collection.add(
        ids=[rule_id],
        embeddings=[embedding],
        documents=[text_to_embed],
        metadatas=[{
            "pattern": pattern,
            "action": action,
            "urgency": urgency,
            "note": note,
            "mode": mode,
            "created": datetime.utcnow().isoformat(),
        }],
    )

    log.info(f"Rule stored [{rule_id}] ({mode}): {pattern} → {action}")
    return rule_id


def find_relevant_rules(text: str, n: int = 5, threshold: float = 0.4) -> list[dict]:
    """Retrieve semantically relevant rules for a given input."""
    collection = get_collection("rules")

    if collection.count() == 0:
        return []

    embedding = embed(text)
    results = collection.query(
        query_embeddings=[embedding],
        n_results=min(n, collection.count()),
        include=["metadatas", "distances"],
    )

    rules = []
    for metadata, distance in zip(
        results["metadatas"][0],
        results["distances"][0],
    ):
        similarity = 1 - distance
        if similarity >= threshold:
            rules.append({**metadata, "similarity": round(similarity, 3)})

    return rules


def get_rule(rule_id: str) -> dict | None:
    """Fetch a single rule by ID."""
    collection = get_collection("rules")
    try:
        result = collection.get(ids=[rule_id], include=["metadatas"])
        if result["ids"]:
            return {"id": rule_id, **result["metadatas"][0]}
    except Exception:
        pass
    return None


def graduate_rule(rule_id: str) -> bool:
    """Promote a rule from training to live mode."""
    collection = get_collection("rules")
    rule = get_rule(rule_id)
    if not rule:
        return False

    try:
        # ChromaDB doesn't support partial updates — delete and re-add
        collection.delete(ids=[rule_id])
        text_to_embed = f"{rule['pattern']} {rule['action']} {rule.get('note', '')}".strip()
        embedding = embed(text_to_embed)

        collection.add(
            ids=[rule_id],
            embeddings=[embedding],
            documents=[text_to_embed],
            metadatas=[{
                **{k: v for k, v in rule.items() if k != "id"},
                "mode": "live",
                "graduated": datetime.utcnow().isoformat(),
            }],
        )
        log.info(f"Rule graduated to live: {rule_id}")
        return True
    except Exception as e:
        log.error(f"Failed to graduate rule {rule_id}: {e}")
        return False


def list_rules() -> list[dict]:
    """Return all stored rules."""
    collection = get_collection("rules")
    if collection.count() == 0:
        return []
    results = collection.get(include=["metadatas"])
    return [
        {"id": id_, **meta}
        for id_, meta in zip(results["ids"], results["metadatas"])
    ]


def delete_rule(rule_id: str) -> bool:
    collection = get_collection("rules")
    try:
        collection.delete(ids=[rule_id])
        log.info(f"Rule deleted: {rule_id}")
        return True
    except Exception as e:
        log.error(f"Failed to delete rule {rule_id}: {e}")
        return False


# ---------------------------------------------------------------------------
# Knowledge collection
# ---------------------------------------------------------------------------

def add_knowledge(text: str, source: str, title: str = "") -> str:
    collection = get_collection("knowledge")
    doc_id = str(uuid.uuid4())[:8]
    embedding = embed(text)

    collection.add(
        ids=[doc_id],
        embeddings=[embedding],
        documents=[text],
        metadatas=[{
            "source": source,
            "title": title,
            "created": datetime.utcnow().isoformat(),
        }],
    )

    log.info(f"Knowledge stored [{doc_id}]: {title or source}")
    return doc_id


def search_knowledge(query: str, n: int = 3, threshold: float = 0.35) -> list[dict]:
    collection = get_collection("knowledge")

    if collection.count() == 0:
        return []

    embedding = embed(query)
    results = collection.query(
        query_embeddings=[embedding],
        n_results=min(n, collection.count()),
        include=["documents", "metadatas", "distances"],
    )

    chunks = []
    for doc, metadata, distance in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        similarity = 1 - distance
        if similarity >= threshold:
            chunks.append({
                "text": doc,
                "source": metadata.get("source", ""),
                "title": metadata.get("title", ""),
                "similarity": round(similarity, 3),
            })

    return chunks
