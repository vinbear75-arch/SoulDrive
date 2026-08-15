from typing import Protocol


class GraphStore(Protocol):
    def add_entity(self, name: str, entity_type: str, description: str, chunk_ids: list[str] | None = None):
        ...

    def add_relationship(self, source: str, target: str, relation: str, evidence_chunk_ids: list[str] | None = None):
        ...

    def get_all_entities(self) -> list[str]:
        ...

    def get_related_entities(self, entity_name: str, limit: int = 20) -> list[tuple[str, str, str]]:
        ...

    def get_subgraph(self, entity_name: str, depth: int = 1) -> list[tuple[str, str, str]]:
        ...

    def recall_chunk_ids(self, query: str, limit: int = 40) -> list[str]:
        ...

    def remove_chunk_ids(self, chunk_ids: list[str]) -> None:
        ...

    def close(self):
        ...
