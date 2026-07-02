from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional, Tuple


IndexKey = Tuple[str, str]


@dataclass(frozen=True)
class MongoIndex:
    collection: Optional[str]
    name: str
    keys: tuple[IndexKey, ...]
    raw: dict[str, Any]


@dataclass(frozen=True)
class SuggestedMongoIndex:
    collection: Optional[str]
    keys: tuple[IndexKey, ...]
    example: str


@dataclass(frozen=True)
class MongoIndexReview:
    status: str
    detail: str
    matched_indexes: tuple[str, ...]
    suggested_keys: tuple[IndexKey, ...]


def parse_mongo_indexes(raw_text: str, default_collection: Optional[str] = None) -> list[MongoIndex]:
    raw_text = raw_text.strip()
    if not raw_text:
        return []

    data = _load_json(raw_text)
    return _indexes_from_data(data, default_collection)


def suggested_index_from_example(example: Optional[str]) -> Optional[SuggestedMongoIndex]:
    if not example or "createIndex" not in example:
        return None

    collection = None
    collection_match = re.search(r'getCollection\("([^"]+)"\)', example)
    if collection_match:
        collection = collection_match.group(1)
    else:
        dotted_match = re.search(r"\bdb\.([A-Za-z_][\w$]*)\.createIndex", example)
        if dotted_match:
            collection = dotted_match.group(1)

    call_start = example.find("createIndex(")
    brace_start = example.find("{", call_start)
    if brace_start == -1:
        return None

    key_spec_text = _extract_balanced_braces(example, brace_start)
    if not key_spec_text:
        return None

    try:
        key_spec = json.loads(key_spec_text)
    except json.JSONDecodeError:
        return None

    keys = _key_tuple(key_spec)
    if not keys:
        return None

    return SuggestedMongoIndex(collection=collection, keys=keys, example=example)


def review_suggested_index(
    suggested: SuggestedMongoIndex,
    existing_indexes: list[MongoIndex],
    namespace: Optional[str] = None,
) -> MongoIndexReview:
    collection = suggested.collection or _collection_from_ns(namespace)
    candidates = [
        index
        for index in existing_indexes
        if _same_collection(collection, index.collection)
    ]

    exact = [index for index in candidates if index.keys == suggested.keys]
    if exact:
        return MongoIndexReview(
            status="Already exists",
            detail="The suggested key pattern exactly matches an existing index.",
            matched_indexes=tuple(index.name for index in exact),
            suggested_keys=suggested.keys,
        )

    field_prefix = [
        index
        for index in candidates
        if _fields(suggested.keys) == _fields(index.keys[: len(suggested.keys)])
    ]
    if field_prefix:
        return MongoIndexReview(
            status="Covered by existing index",
            detail="An existing compound index starts with the suggested fields.",
            matched_indexes=tuple(index.name for index in field_prefix),
            suggested_keys=suggested.keys,
        )

    partial = [
        index
        for index in candidates
        if suggested.keys and index.keys and suggested.keys[0][0] == index.keys[0][0]
    ]
    if partial:
        return MongoIndexReview(
            status="Partial overlap",
            detail="An existing index starts with the same first field, but does not cover the full suggestion.",
            matched_indexes=tuple(index.name for index in partial),
            suggested_keys=suggested.keys,
        )

    return MongoIndexReview(
        status="New candidate",
        detail="No matching existing index was found for this suggested key pattern.",
        matched_indexes=(),
        suggested_keys=suggested.keys,
    )


def mongo_index_rows(indexes: list[MongoIndex]) -> list[dict[str, str]]:
    return [
        {
            "collection": index.collection or "",
            "name": index.name,
            "key_pattern": _format_keys(index.keys),
        }
        for index in indexes
    ]


def duplicate_index_rows(indexes: list[MongoIndex]) -> list[dict[str, str]]:
    grouped: dict[tuple[Optional[str], tuple[IndexKey, ...]], list[MongoIndex]] = {}
    for index in indexes:
        grouped.setdefault((index.collection, index.keys), []).append(index)

    rows = []
    for (collection, keys), matches in grouped.items():
        if len(matches) < 2:
            continue
        rows.append(
            {
                "collection": collection or "",
                "key_pattern": _format_keys(keys),
                "indexes": ", ".join(index.name for index in matches),
            }
        )
    return rows


def redundant_index_rows(indexes: list[MongoIndex]) -> list[dict[str, str]]:
    rows = []
    for shorter in indexes:
        if _fields(shorter.keys) == ("_id",):
            continue
        for longer in indexes:
            if shorter == longer or not _same_collection(shorter.collection, longer.collection):
                continue
            if len(shorter.keys) >= len(longer.keys):
                continue
            if _fields(shorter.keys) == _fields(longer.keys[: len(shorter.keys)]):
                rows.append(
                    {
                        "collection": shorter.collection or "",
                        "covered_index": shorter.name,
                        "covering_index": longer.name,
                        "covered_pattern": _format_keys(shorter.keys),
                        "covering_pattern": _format_keys(longer.keys),
                    }
                )
                break
    return rows


def review_row(title: str, review: MongoIndexReview) -> dict[str, str]:
    return {
        "suggestion": title,
        "suggested_key_pattern": _format_keys(review.suggested_keys),
        "status": review.status,
        "matched_indexes": ", ".join(review.matched_indexes),
        "detail": review.detail,
    }


def _load_json(raw_text: str) -> Any:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass

    for open_char, close_char in (("[", "]"), ("{", "}")):
        start = raw_text.find(open_char)
        end = raw_text.rfind(close_char)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw_text[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise ValueError(
        "Paste valid JSON from getIndexes(), for example JSON.stringify(db.collection.getIndexes())."
    )


def _indexes_from_data(data: Any, default_collection: Optional[str]) -> list[MongoIndex]:
    if isinstance(data, list):
        return [_index_from_dict(item, default_collection) for item in data if isinstance(item, dict)]

    if isinstance(data, dict):
        if isinstance(data.get("indexes"), list):
            collection = data.get("collection") or data.get("collectionName") or default_collection
            return _indexes_from_data(data["indexes"], str(collection) if collection else None)
        if isinstance(data.get("key"), dict):
            return [_index_from_dict(data, default_collection)]

        indexes: list[MongoIndex] = []
        for collection, value in data.items():
            if isinstance(value, list):
                indexes.extend(_indexes_from_data(value, str(collection)))
        return indexes

    return []


def _index_from_dict(index_doc: dict[str, Any], default_collection: Optional[str]) -> MongoIndex:
    collection = (
        index_doc.get("collection")
        or index_doc.get("collectionName")
        or _collection_from_ns(index_doc.get("ns"))
        or default_collection
    )
    key_doc = index_doc.get("key") if isinstance(index_doc.get("key"), dict) else {}
    keys = _key_tuple(key_doc)
    name = str(index_doc.get("name") or _format_keys(keys) or "<unnamed>")
    return MongoIndex(
        collection=str(collection) if collection else None,
        name=name,
        keys=keys,
        raw=index_doc,
    )


def _key_tuple(key_doc: dict[str, Any]) -> tuple[IndexKey, ...]:
    keys: list[IndexKey] = []
    for field, direction in key_doc.items():
        keys.append((str(field), _normalize_direction(direction)))
    return tuple(keys)


def _normalize_direction(direction: Any) -> str:
    if isinstance(direction, float) and direction.is_integer():
        direction = int(direction)
    return str(direction)


def _collection_from_ns(namespace: Any) -> Optional[str]:
    if not isinstance(namespace, str) or "." not in namespace:
        return None
    return namespace.split(".", 1)[1]


def _same_collection(left: Optional[str], right: Optional[str]) -> bool:
    if not left or left == "<collection>":
        return True
    if not right:
        return False
    return left == right


def _fields(keys: tuple[IndexKey, ...]) -> tuple[str, ...]:
    return tuple(field for field, _direction in keys)


def _format_keys(keys: tuple[IndexKey, ...]) -> str:
    return ", ".join(f"{field}: {direction}" for field, direction in keys)


def _extract_balanced_braces(text: str, start: int) -> Optional[str]:
    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None
