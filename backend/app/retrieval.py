from __future__ import annotations

import hashlib
import logging
import math
from typing import Any

from elasticsearch import Elasticsearch

from .database import Database
from .settings import Settings


logger = logging.getLogger("agent.workflow")
VECTOR_DIMS = 64


def local_text_vector(text: str) -> list[float]:
    """Create a deterministic local vector without an external embedding service."""
    vector = [0.0] * VECTOR_DIMS
    normalized = "".join(text.lower().split())
    tokens = list(normalized)
    tokens.extend(normalized[index:index + 2] for index in range(len(normalized) - 1))
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
        bucket = int.from_bytes(digest[:2], "big") % VECTOR_DIMS
        sign = 1.0 if digest[2] % 2 == 0 else -1.0
        vector[bucket] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


class SkuRetriever:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.index_name = settings.sku_index_name
        self.client = (
            Elasticsearch(settings.elasticsearch_url, request_timeout=5)
            if settings.elasticsearch_url
            else None
        )

    @property
    def backend(self) -> str:
        return "elasticsearch_hybrid" if self.client else "local_exact"

    def sync_catalog(self, site_id: int, sku_version: int) -> dict[str, Any]:
        products = self.database.list_sku_snapshot(site_id, sku_version)
        if self.client is None:
            return {
                "site_id": site_id,
                "sku_version": sku_version,
                "indexed_count": 0,
                "backend": "local_exact",
                "index_synced": True,
                "index_error": None,
            }
        if not self.client.indices.exists(index=self.index_name):
            self.client.indices.create(
                index=self.index_name,
                mappings={
                    "properties": {
                        "site_id": {"type": "integer"},
                        "sku_version": {"type": "integer"},
                        "product_id": {"type": "integer"},
                        "sku": {"type": "keyword"},
                        "name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                        "aliases": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                        "search_text": {"type": "text"},
                        "embedding": {
                            "type": "dense_vector",
                            "dims": VECTOR_DIMS,
                            "index": True,
                            "similarity": "cosine",
                        },
                    }
                },
            )
        def index_operations(product: dict[str, Any]) -> list[dict[str, Any]]:
            search_text = " ".join([product["name"], *product["aliases"]])
            return [
                {"index": {"_index": self.index_name, "_id": f"{site_id}:{sku_version}:{product['id']}"}},
                {
                    "site_id": site_id,
                    "sku_version": sku_version,
                    "product_id": product["id"],
                    "sku": product["sku"],
                    "name": product["name"],
                    "aliases": product["aliases"],
                    "search_text": search_text,
                    "embedding": local_text_vector(search_text),
                },
            ]

        operations = [
            operation
            for product in products
            for operation in index_operations(product)
        ]
        if operations:
            response = self.client.bulk(operations=operations, refresh=True)
            if response.get("errors"):
                failed_count = sum(
                    bool(next(iter(item.values())).get("error"))
                    for item in response.get("items", [])
                    if item
                )
                raise RuntimeError(
                    f"Elasticsearch bulk 同步失败 {failed_count} 条"
                )
        logger.info(
            "RAG action=sync_sku_index site_id=%d sku_version=%d indexed=%d",
            site_id, sku_version, len(products),
        )
        return {
            "site_id": site_id,
            "sku_version": sku_version,
            "indexed_count": len(products),
            "backend": self.backend,
            "index_synced": True,
            "index_error": None,
        }

    def close(self) -> None:
        if self.client is not None:
            self.client.close()

    def enrich(
        self, items: list[dict[str, Any]], site_id: int, sku_version: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
        products = self.database.list_sku_snapshot(site_id, sku_version)
        exact_names = {
            name: product
            for product in products
            for name in [product["name"], *product["aliases"]]
        }

        def enrich_item(source_item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            item = dict(source_item)
            query = str(item["raw_product_name"])
            product = exact_names.get(query)
            candidates: list[dict[str, Any]] = []
            mode = "exact"
            if product:
                item.update({
                    "product_id": product["id"],
                    "product_name": product["name"],
                    "unit_price": product["unit_price"],
                    "matched": True,
                })
                candidates = [{
                    "product_id": product["id"],
                    "product_name": product["name"],
                    "score": 1.0,
                }]
            else:
                mode = "hybrid_candidates" if self.client else "local_unmatched"
                candidates = self._hybrid_candidates(query, site_id, sku_version)
            return item, {
                "line_no": item["line_no"],
                "query": query,
                "mode": mode,
                "selected_product_id": item.get("product_id"),
                "candidates": candidates,
            }

        results = list(map(enrich_item, items))
        enriched_items = [item for item, _ in results]
        evidence = [item_evidence for _, item_evidence in results]
        matched = sum(item.get("matched", False) for item in enriched_items)
        return enriched_items, evidence, f"成功匹配 {matched}/{len(items)} 条商品，召回后端 {self.backend}"

    def _hybrid_candidates(
        self, query: str, site_id: int, sku_version: int
    ) -> list[dict[str, Any]]:
        if self.client is None:
            return []
        try:
            response = self.client.search(
                index=self.index_name,
                size=3,
                source=["product_id", "name"],
                query={
                    "script_score": {
                        "query": {
                            "bool": {
                                "filter": [
                                    {"term": {"site_id": site_id}},
                                    {"term": {"sku_version": sku_version}},
                                ],
                                "should": [{
                                    "multi_match": {
                                        "query": query,
                                        "fields": ["name^3", "aliases^3", "search_text"],
                                    }
                                }],
                            }
                        },
                        "script": {
                            "source": "0.65 * _score + 0.35 * (cosineSimilarity(params.vector, 'embedding') + 1.0)",
                            "params": {"vector": local_text_vector(query)},
                        },
                    }
                },
            )
            return [
                {
                    "product_id": hit["_source"]["product_id"],
                    "product_name": hit["_source"]["name"],
                    "score": round(float(hit["_score"]), 4),
                }
                for hit in response["hits"]["hits"]
            ]
        except Exception as exc:
            logger.warning(
                "RAG action=hybrid_retrieve status=fallback reason=%s",
                type(exc).__name__,
            )
            return []
