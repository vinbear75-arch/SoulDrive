from typing import Protocol


class GraphStore(Protocol):
    def add_document(
        self,
        source_filename: str,
        title: str,
        summary: str,
        keywords: list[str] | None = None,
        chunk_ids: list[str] | None = None,
        project_id: str | None = None,
    ):
        ...

    def add_entity(self, name: str, entity_type: str, description: str, chunk_ids: list[str] | None = None):
        ...

    def add_relationship(self, source: str, target: str, relation: str, evidence_chunk_ids: list[str] | None = None):
        ...

    def get_all_entities(
        self,
        project_id: str | None = None,
        global_only: bool = False,
    ) -> list[str]:
        ...

    def get_related_entities(self, entity_name: str, limit: int = 20) -> list[tuple[str, str, str]]:
        ...

    def get_subgraph(
        self,
        entity_name: str,
        depth: int = 1,
        project_id: str | None = None,
        global_only: bool = False,
    ) -> list[tuple[str, str, str]]:
        ...

    def recall_chunk_ids(
        self,
        query: str,
        limit: int = 40,
        project_id: str | None = None,
        global_only: bool = False,
    ) -> list[str]:
        ...

    def get_graph_snapshot(self) -> dict:
        ...

    def save_graphrag_index(self, payload: dict, index_name: str = "default") -> None:
        ...

    def load_graphrag_index(self, index_name: str = "default") -> dict | None:
        ...

    def remove_chunk_ids(self, chunk_ids: list[str]) -> None:
        ...

    def remove_project(self, project_id: str, chunk_ids: list[str] | None = None) -> None:
        ...

    def close(self):
        ...
