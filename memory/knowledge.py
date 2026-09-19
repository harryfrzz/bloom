from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from agent.types import Tool


logger = logging.getLogger(__name__)


class Knowledge:
    """What has already happened, kept so it can be found again.

    Conversations, watches that fired and anything worth keeping are embedded
    and stored per person, so "what did we decide on Tuesday" can be answered
    by meaning rather than by exact words. Each person gets their own
    namespace: nothing of one is reachable from another.
    """

    MODEL = "text-embedding-3-small"
    DIMENSION = 1536
    INDEX = "bloom-memory"
    # Long enough to hold a real exchange, short enough that one embedding
    # still describes a single thing.
    EXCERPT = 1600

    def __init__(self, *, index: Any, embed: Callable[[str], list[float]]) -> None:
        self.index = index
        self.embed = embed

    @classmethod
    def from_environment(cls) -> "Knowledge | None":
        """Build one if a key is configured; otherwise bloom simply forgets."""
        api_key = os.getenv("PINECONE_API_KEY")
        if not api_key:
            return None
        try:
            from openai import OpenAI
            from pinecone import Pinecone, ServerlessSpec

            pinecone = Pinecone(api_key=api_key)
            name = os.getenv("PINECONE_INDEX", cls.INDEX)
            if not pinecone.has_index(name):
                logger.warning("Creating the %s index", name)
                pinecone.create_index(
                    name=name,
                    dimension=cls.DIMENSION,
                    metric="cosine",
                    spec=ServerlessSpec(
                        cloud=os.getenv("PINECONE_CLOUD", "aws"),
                        region=os.getenv("PINECONE_REGION", "us-east-1"),
                    ),
                )
            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            model = os.getenv("BLOOM_EMBED_MODEL", cls.MODEL)

            def embed(text: str) -> list[float]:
                return client.embeddings.create(model=model, input=text[: cls.EXCERPT]).data[0].embedding

            return cls(index=pinecone.Index(name), embed=embed)
        except Exception as exc:
            logger.warning("Recall is unavailable: %s", exc)
            return None

    @staticmethod
    def _namespace(user_id: str) -> str:
        return "".join(character if character.isalnum() else "-" for character in user_id)[:60]

    def remember(self, *, user_id: str, text: str, kind: str = "conversation", when: datetime | None = None) -> bool:
        body = " ".join(text.split())[: self.EXCERPT]
        if not body:
            return False
        moment = when or datetime.now(UTC)
        try:
            self.index.upsert(
                vectors=[
                    {
                        "id": uuid.uuid4().hex,
                        "values": self.embed(body),
                        "metadata": {
                            "text": body,
                            "kind": kind,
                            "day": moment.date().isoformat(),
                            "when": moment.isoformat(),
                        },
                    }
                ],
                namespace=self._namespace(user_id),
            )
        except Exception as exc:
            logger.warning("Could not remember something for %s: %s", user_id, exc)
            return False
        return True

    def recall(
        self,
        *,
        user_id: str,
        query: str,
        limit: int = 5,
        on_day: str | None = None,
        since: str | None = None,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions: dict[str, Any] = {}
        if on_day:
            conditions["day"] = {"$eq": on_day}
        elif since:
            conditions["day"] = {"$gte": since}
        if kind:
            conditions["kind"] = {"$eq": kind}
        try:
            found = self.index.query(
                vector=self.embed(query),
                top_k=max(1, min(limit, 20)),
                include_metadata=True,
                namespace=self._namespace(user_id),
                filter=conditions or None,
            )
        except Exception as exc:
            logger.warning("Could not search memory for %s: %s", user_id, exc)
            return []
        matches = found.get("matches") if isinstance(found, dict) else getattr(found, "matches", [])
        results = []
        for match in matches or []:
            metadata = (match.get("metadata") if isinstance(match, dict) else getattr(match, "metadata", {})) or {}
            score = match.get("score") if isinstance(match, dict) else getattr(match, "score", 0.0)
            results.append({"text": metadata.get("text", ""), "when": metadata.get("when", ""), "kind": metadata.get("kind", ""), "score": round(float(score or 0), 3)})
        return results

    def tool(self, user_id: Callable[[], str]) -> Tool:
        def handler(arguments: dict) -> str:
            query = str(arguments.get("about", "")).strip()
            if not query:
                return "Say what to look for."
            found = self.recall(
                user_id=user_id(),
                query=query,
                on_day=str(arguments.get("on_day") or "").strip() or None,
                since=str(arguments.get("since") or "").strip() or None,
                kind=str(arguments.get("kind") or "").strip() or None,
                limit=int(arguments.get("limit") or 5),
            )
            if not found:
                return "Nothing from before matches that."
            return "\n".join(f"[{item['when'][:16]}] {item['text']}" for item in found)

        return Tool(
            name="recall",
            description=(
                "Search what has happened before with this person: earlier conversations, what a watch "
                "found, things worth keeping. Searches by meaning, so the words need not match. Use it "
                "whenever they refer to something from the past — what was decided, what someone said, "
                "what happened on a day — rather than saying you do not remember."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "about": {"type": "string", "description": "What to look for, in plain words."},
                    "on_day": {"type": "string", "description": "A single date as YYYY-MM-DD, if they named one."},
                    "since": {"type": "string", "description": "Only from this date onwards, as YYYY-MM-DD."},
                    "kind": {"type": "string", "description": "Narrow to 'conversation' or 'watch'."},
                    "limit": {"type": "integer"},
                },
                "required": ["about"],
            },
            handler=handler,
        )
