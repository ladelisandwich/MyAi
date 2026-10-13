"""Repository layer: the only place where SQL lives.

Services receive a :class:`Repositories` bundle. A future PostgreSQL backend would provide
alternative implementations of these classes with the same async method signatures.
"""

from __future__ import annotations

from dataclasses import dataclass

from homemesh.coordinator.db.database import Database
from homemesh.coordinator.db.repositories.conversations import ConversationRepo, MessageRepo, ProjectRepo
from homemesh.coordinator.db.repositories.jobs import JobRepo
from homemesh.coordinator.db.repositories.knowledge import (
    ChunkRepo,
    CollectionRepo,
    DocumentRepo,
    EmbeddingRepo,
)
from homemesh.coordinator.db.repositories.memory import MemoryRepo
from homemesh.coordinator.db.repositories.nodes import NodeRepo
from homemesh.coordinator.db.repositories.system import AuditRepo, SettingsRepo
from homemesh.coordinator.db.repositories.tools import ToolRepo
from homemesh.coordinator.db.repositories.updates import UpdateRepo
from homemesh.coordinator.db.repositories.users import ApiTokenRepo, SessionRepo, UserRepo


@dataclass
class Repositories:
    db: Database
    users: UserRepo
    sessions: SessionRepo
    api_tokens: ApiTokenRepo
    nodes: NodeRepo
    projects: ProjectRepo
    conversations: ConversationRepo
    messages: MessageRepo
    memories: MemoryRepo
    collections: CollectionRepo
    documents: DocumentRepo
    chunks: ChunkRepo
    embeddings: EmbeddingRepo
    jobs: JobRepo
    tools: ToolRepo
    settings: SettingsRepo
    audit: AuditRepo
    updates: UpdateRepo

    @classmethod
    def create(cls, db: Database) -> Repositories:
        return cls(
            db=db, users=UserRepo(db), sessions=SessionRepo(db), api_tokens=ApiTokenRepo(db), nodes=NodeRepo(db),
            projects=ProjectRepo(db), conversations=ConversationRepo(db), messages=MessageRepo(db),
            memories=MemoryRepo(db), collections=CollectionRepo(db), documents=DocumentRepo(db),
            chunks=ChunkRepo(db), embeddings=EmbeddingRepo(db), jobs=JobRepo(db), tools=ToolRepo(db),
            settings=SettingsRepo(db), audit=AuditRepo(db), updates=UpdateRepo(db),
        )
