from __future__ import annotations

import logging
from pathlib import Path

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from find_my_job.llm import get_embeddings
from find_my_job.models import AppConfig, JobPosting, RagEvidence, ResumeProfile

logger = logging.getLogger("find_my_job.rag")


class ResumeRAG:
    def __init__(self, config: AppConfig, *, force_local: bool = False) -> None:
        self.config = config
        self.persist_dir = Path(config.rag.persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self.embeddings = get_embeddings(config, force_local=force_local)
        # Keep local vs cloud collections separate (different vector dims).
        base = config.rag.collection or "resumes"
        using_local = type(self.embeddings).__name__ == "HashEmbeddings"
        self.collection = f"{base}_local" if using_local else base
        self._store: Chroma | None = None

    @property
    def store(self) -> Chroma:
        if self._store is None:
            self._store = Chroma(
                collection_name=self.collection,
                embedding_function=self.embeddings,
                persist_directory=str(self.persist_dir),
            )
        return self._store

    def _needs_rebuild(self, profiles: list[ResumeProfile]) -> bool:
        if not self.config.rag.rebuild_on_resume_change:
            return False
        try:
            existing = self.store.get(include=["metadatas"])
            metas = existing.get("metadatas") or []
            hashes = {
                (m or {}).get("file_hash")
                for m in metas
                if (m or {}).get("file_hash")
            }
            current = {p.file_hash for p in profiles}
            return hashes != current or not metas
        except Exception:
            return True

    def ingest(self, profiles: list[ResumeProfile], *, force: bool = False) -> int:
        if not force and not self._needs_rebuild(profiles):
            logger.info("Chroma resume index up to date; skipping rebuild")
            return 0

        # Drop and recreate collection for a clean rebuild
        try:
            client = self.store._client  # noqa: SLF001
            client.delete_collection(self.collection)
        except Exception:
            pass
        self._store = None
        # Recreate via property
        _ = self.store

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=800,
            chunk_overlap=120,
            separators=["\n\n", "\n", ". ", " ", ""],
        )
        docs: list[Document] = []
        ids: list[str] = []
        for profile in profiles:
            chunks = splitter.split_text(profile.text)
            for i, chunk in enumerate(chunks):
                doc_id = f"{profile.label}:{profile.file_hash[:8]}:{i}"
                docs.append(
                    Document(
                        page_content=chunk,
                        metadata={
                            "resume_label": profile.label,
                            "source_path": profile.path,
                            "chunk_type": "resume",
                            "file_hash": profile.file_hash,
                            "chunk_index": i,
                        },
                    )
                )
                ids.append(doc_id)

        if docs:
            self.store.add_documents(docs, ids=ids)
        logger.info("Ingested %d resume chunks into Chroma", len(docs))
        return len(docs)

    def score_job(self, job: JobPosting) -> tuple[float, list[RagEvidence]]:
        query = f"{job.title}\n{job.company}\n{job.location}\n{job.raw_text}"[:4000]
        try:
            results = self.store.similarity_search_with_score(
                query, k=self.config.rag.top_k
            )
        except Exception as exc:
            logger.warning("Chroma query failed: %s", exc)
            return 0.0, []

        evidence: list[RagEvidence] = []
        sims: list[float] = []
        for doc, distance in results:
            # Chroma/langchain often returns distance (lower better). Convert to similarity.
            sim = float(1.0 / (1.0 + float(distance)))
            sims.append(sim)
            meta = doc.metadata or {}
            evidence.append(
                RagEvidence(
                    text=doc.page_content[:500],
                    score=sim,
                    resume_label=str(meta.get("resume_label", "")),
                    metadata=dict(meta),
                )
            )
        if not sims:
            return 0.0, []
        # Prefer max; also consider mean lightly
        rag_score = 0.7 * max(sims) + 0.3 * (sum(sims) / len(sims))
        return rag_score, evidence
