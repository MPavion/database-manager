import copy
import hashlib
import json
import re
from datetime import datetime, timezone


def normalize_timestamp(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat().replace("+00:00", "Z")
    return str(value).strip()


VOLATILE_HASH_KEYS = {
    "url",
    "expiry_time",
    "request_id",
    "public_url",
    "download_url",
    "user_notion_url",
    "internal_resource_uri",
}


def sanitize_for_hash(value):
    if isinstance(value, dict):
        return {
            key: sanitize_for_hash(item)
            for key, item in value.items()
            if key not in VOLATILE_HASH_KEYS
        }
    if isinstance(value, list):
        return [sanitize_for_hash(item) for item in value]
    return value


def compute_content_hash(title: str, ai_summary: str, raw_json: dict, media_paths: list[str]) -> str:
    payload = {
        "title": (title or "").strip(),
        "ai_summary": (ai_summary or "").strip(),
        "raw_json": sanitize_for_hash(raw_json or {}),
        "media_local_paths": sorted(str(path) for path in (media_paths or [])),
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def page_requires_update(existing_record: dict | None, source_updated_at: str, content_hash: str) -> bool:
    if not existing_record:
        return True

    existing_ts = normalize_timestamp(existing_record.get("source_updated_at"))
    new_ts = normalize_timestamp(source_updated_at)
    existing_hash = (existing_record.get("content_hash") or "").strip()
    new_hash = (content_hash or "").strip()

    if existing_record.get("needs_push") and existing_ts == new_ts:
        return False

    return existing_ts != new_ts or existing_hash != new_hash


def normalize_notion_id(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""

    hex_only = re.sub(r"[^0-9a-f]", "", text)
    if len(hex_only) != 32:
        return ""

    return (
        f"{hex_only[:8]}-{hex_only[8:12]}-{hex_only[12:16]}-"
        f"{hex_only[16:20]}-{hex_only[20:32]}"
    )


def extract_notion_ids_from_url(value: str | None) -> list[str]:
    text = str(value or "").strip()
    lowered = text.lower()
    if "notion.so" not in lowered and "notion.site" not in lowered:
        return []

    matches = re.findall(r"([0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", text)
    results: list[str] = []
    for match in matches:
        normalized = normalize_notion_id(match)
        if normalized and normalized not in results:
            results.append(normalized)
    return results


_ARRAY_PROPERTY_TYPES = {"title", "rich_text", "multi_select", "people", "relation", "files"}
_OBJECT_PROPERTY_TYPES = {"select", "status", "date"}


def _coerce_notion_list(value, wrap_single_dict: bool = True) -> list:
    if isinstance(value, list):
        return copy.deepcopy(value)
    if isinstance(value, tuple):
        return [copy.deepcopy(item) for item in value]
    if isinstance(value, dict):
        nested_results = value.get("results")
        if isinstance(nested_results, list):
            return copy.deepcopy(nested_results)
        return [copy.deepcopy(value)] if wrap_single_dict and value else []
    return []


def normalize_workspace_page_json(page: dict | None) -> tuple[dict, list[str]]:
    if not isinstance(page, dict):
        return {}, [f"page payload was {type(page).__name__}; replaced with an empty object"] if page not in (None, {}) else []

    normalized = copy.deepcopy(page)
    issues: list[str] = []
    object_type = str(normalized.get("object") or "").strip().lower()
    is_schema_payload = object_type in {"database", "data_source"}

    properties = normalized.get("properties")
    if properties is None:
        normalized["properties"] = {}
    elif not isinstance(properties, dict):
        issues.append(f"properties expected an object but found {type(properties).__name__}")
        normalized["properties"] = {}

    for property_name, prop in list(normalized.get("properties", {}).items()):
        if not isinstance(prop, dict):
            issues.append(f"property '{property_name}' expected an object but found {type(prop).__name__}")
            normalized["properties"][property_name] = {
                "type": "unknown",
                "value": copy.deepcopy(prop),
            }
            continue

        prop_type = str(prop.get("type") or "").strip().lower()
        current_value = prop.get(prop_type)

        if is_schema_payload:
            if prop_type in {"select", "status", "multi_select"} and current_value is not None and not isinstance(current_value, dict):
                issues.append(f"property '{property_name}.{prop_type}' expected an object but found {type(current_value).__name__}")
                if isinstance(current_value, list):
                    prop[prop_type] = {"options": copy.deepcopy(current_value)}
                else:
                    prop[prop_type] = {}
            continue

        if prop_type in _ARRAY_PROPERTY_TYPES:
            if not isinstance(current_value, list):
                issues.append(f"property '{property_name}.{prop_type}' expected a list but found {type(current_value).__name__}")
                prop[prop_type] = _coerce_notion_list(current_value)
        elif prop_type in _OBJECT_PROPERTY_TYPES:
            if current_value is not None and not isinstance(current_value, dict):
                issues.append(f"property '{property_name}.{prop_type}' expected an object but found {type(current_value).__name__}")
                prop[prop_type] = None

    links = normalized.get("_business_brain_links")
    if links is None:
        links = {}
    elif not isinstance(links, dict):
        issues.append(f"_business_brain_links expected an object but found {type(links).__name__}")
        links = {}
    else:
        links = copy.deepcopy(links)

    for key in ("linked_notion_ids", "linked_database_ids"):
        current_value = links.get(key)
        if current_value is None:
            links[key] = []
        elif not isinstance(current_value, list):
            issues.append(f"_business_brain_links.{key} expected a list but found {type(current_value).__name__}")
            if isinstance(current_value, str) and current_value.strip():
                links[key] = [current_value.strip()]
            else:
                links[key] = _coerce_notion_list(current_value, wrap_single_dict=False)

    for key in ("linked_resources", "linked_database_resources"):
        current_value = links.get(key)
        if current_value is None:
            links[key] = []
        elif not isinstance(current_value, list):
            issues.append(f"_business_brain_links.{key} expected a list but found {type(current_value).__name__}")
            links[key] = _coerce_notion_list(current_value)

    for key in ("internal_resource_uri", "user_notion_url"):
        current_value = links.get(key)
        if current_value is None:
            links[key] = ""
        elif not isinstance(current_value, str):
            issues.append(f"_business_brain_links.{key} expected text but found {type(current_value).__name__}")
            links[key] = str(current_value).strip()

    if links or "_business_brain_links" in normalized:
        normalized["_business_brain_links"] = links

    return normalized, issues


def build_page_link_index(page: dict | None) -> dict:
    page, _ = normalize_workspace_page_json(page)
    page_id = normalize_notion_id(page.get("id"))
    user_url = str(page.get("url") or "").strip()
    linked_ids: list[str] = []
    linked_resource_map: dict[str, dict] = {}

    def add_link(notion_id: str, user_notion_url: str | None = None, source: str = "page_reference"):
        normalized = normalize_notion_id(notion_id)
        if not normalized or normalized == page_id:
            return

        if normalized not in linked_ids:
            linked_ids.append(normalized)

        entry = linked_resource_map.setdefault(
            normalized,
            {
                "notion_id": normalized,
                "internal_resource_uri": f"bb://page/{normalized}",
                "source": source,
            },
        )
        if user_notion_url and not entry.get("user_notion_url"):
            entry["user_notion_url"] = user_notion_url
        if source and not entry.get("source"):
            entry["source"] = source

    def walk(value):
        if isinstance(value, dict):
            if value.get("type") == "relation":
                for item in value.get("relation", []):
                    add_link(item.get("id"), source="relation")

            if value.get("type") == "mention":
                mention = value.get("mention") or {}
                if mention.get("type") == "page":
                    add_link((mention.get("page") or {}).get("id"), source="mention")

            href = value.get("href")
            if isinstance(href, str):
                for found_id in extract_notion_ids_from_url(href):
                    add_link(found_id, href, source="url")

            url_value = value.get("url")
            if isinstance(url_value, str):
                for found_id in extract_notion_ids_from_url(url_value):
                    add_link(found_id, url_value, source="url")

            text_link = (value.get("text") or {}).get("link") or {}
            if isinstance(text_link.get("url"), str):
                link_url = text_link.get("url")
                for found_id in extract_notion_ids_from_url(link_url):
                    add_link(found_id, link_url, source="url")

            for nested in value.values():
                walk(nested)

        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(page.get("properties") or {})

    return {
        "internal_resource_uri": f"bb://page/{page_id}" if page_id else "",
        "user_notion_url": user_url,
        "linked_notion_ids": linked_ids,
        "linked_resources": [linked_resource_map[notion_id] for notion_id in linked_ids],
    }


def build_notion_text_objects(text: str) -> list[dict]:
    clean_text = (text or "").strip()
    if not clean_text:
        return []
    return [{"type": "text", "text": {"content": clean_text}, "plain_text": clean_text}]


def coerce_snapshot_datetime(value) -> datetime:
    if isinstance(value, datetime):
        dt_value = value
    else:
        text_value = str(value or "").strip()
        if not text_value:
            raise ValueError("A snapshot date and time is required.")
        dt_value = datetime.fromisoformat(text_value.replace("Z", "+00:00"))

    if dt_value.tzinfo is None:
        dt_value = dt_value.replace(tzinfo=timezone.utc)
    return dt_value.astimezone(timezone.utc)


def format_compare_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    return str(value)


def empty_property_value(property_type: str):
    defaults = {
        "title": [],
        "rich_text": [],
        "number": None,
        "select": None,
        "multi_select": [],
        "status": None,
        "checkbox": False,
        "url": None,
        "email": None,
        "phone_number": None,
        "date": None,
        "people": [],
        "relation": [],
    }
    return copy.deepcopy(defaults.get(property_type))


def plain_text_from_fragments(fragments) -> str:
    if isinstance(fragments, dict):
        fragments = [fragments]
    elif not isinstance(fragments, list):
        fragments = []

    parts = []
    for item in fragments:
        if isinstance(item, dict):
            text = item.get("plain_text")
            if text is None:
                text = (item.get("text") or {}).get("content", "")
            parts.append(text or "")
        elif item is not None:
            parts.append(str(item))
    return "".join(parts).strip()


def property_preview_value(prop: dict | None) -> str:
    if not isinstance(prop, dict):
        return ""

    prop_type = str(prop.get("type") or "").strip().lower()
    if prop_type == "title":
        return plain_text_from_fragments(prop.get("title", []))
    if prop_type == "rich_text":
        return plain_text_from_fragments(prop.get("rich_text", []))
    if prop_type in {"number", "url", "email", "phone_number"}:
        return "" if prop.get(prop_type) is None else str(prop.get(prop_type))
    if prop_type == "checkbox":
        return "Checked" if prop.get("checkbox") else "Unchecked"
    if prop_type in {"select", "status"}:
        value = prop.get(prop_type)
        return (value.get("name", "") if isinstance(value, dict) else "").strip()
    if prop_type == "multi_select":
        return ", ".join(
            item.get("name", "")
            for item in _coerce_notion_list(prop.get("multi_select"))
            if isinstance(item, dict) and item.get("name")
        )
    if prop_type == "date":
        date_value = prop.get("date") if isinstance(prop.get("date"), dict) else {}
        start = date_value.get("start") or ""
        end = date_value.get("end") or ""
        if start and end:
            return f"{start} → {end}"
        return start
    if prop_type in {"people", "relation"}:
        return ", ".join(
            (item.get("name") or item.get("id", "")).strip()
            for item in _coerce_notion_list(prop.get(prop_type))
            if isinstance(item, dict) and (item.get("name") or item.get("id"))
        )
    return format_compare_value(prop)


def property_push_supported(prop: dict | None) -> bool:
    if not prop:
        return False
    return prop.get("type") in {
        "title",
        "rich_text",
        "number",
        "select",
        "multi_select",
        "status",
        "checkbox",
        "url",
        "email",
        "phone_number",
        "date",
        "people",
        "relation",
    }


def build_local_edit_payload(existing_record: dict, title: str | None = None, ai_summary: str | None = None) -> dict:
    if not existing_record:
        raise ValueError("Existing record is required for a local edit.")

    updated_title = (existing_record.get("title") or "Untitled") if title is None else (title.strip() or "Untitled")
    updated_summary = (existing_record.get("ai_summary") or "") if ai_summary is None else ai_summary.strip()
    raw_json, _ = normalize_workspace_page_json(existing_record.get("raw_json") or {})
    properties = raw_json.setdefault("properties", {}) if isinstance(raw_json, dict) else {}

    for prop in properties.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            prop["title"] = build_notion_text_objects(updated_title)
            break

    for prop in properties.values():
        if isinstance(prop, dict) and prop.get("type") == "rich_text":
            prop["rich_text"] = build_notion_text_objects(updated_summary)
            break

    media_paths = existing_record.get("media_local_paths") or []
    return {
        "title": updated_title,
        "ai_summary": updated_summary,
        "raw_json": raw_json,
        "media_local_paths": media_paths,
        "content_hash": compute_content_hash(updated_title, updated_summary, raw_json, media_paths),
        "needs_push": True,
    }


def build_bulk_replace_payload(
    existing_record: dict,
    find_text: str,
    replace_text: str,
    fields: list[str] | None = None,
    case_sensitive: bool = False,
) -> dict | None:
    if not existing_record:
        raise ValueError("Existing record is required for a bulk replace.")

    needle = str(find_text or "")
    if not needle:
        return None

    enabled_fields = [field for field in (fields or ["title", "ai_summary"]) if field in {"title", "ai_summary"}]
    if not enabled_fields:
        enabled_fields = ["title", "ai_summary"]

    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(re.escape(needle), flags)
    changed_fields: list[str] = []
    match_count = 0

    updated_title = existing_record.get("title") or "Untitled"
    updated_summary = existing_record.get("ai_summary") or ""

    if "title" in enabled_fields:
        updated_title, replacements = pattern.subn(replace_text, updated_title)
        if replacements:
            changed_fields.append("title")
            match_count += replacements

    if "ai_summary" in enabled_fields:
        updated_summary, replacements = pattern.subn(replace_text, updated_summary)
        if replacements:
            changed_fields.append("ai_summary")
            match_count += replacements

    if match_count == 0:
        return None

    payload = build_local_edit_payload(existing_record, title=updated_title, ai_summary=updated_summary)
    payload["match_count"] = match_count
    payload["changed_fields"] = changed_fields
    payload["counts"] = {
        "title": 1 if "title" in changed_fields else 0,
        "ai_summary": 1 if "ai_summary" in changed_fields else 0,
    }
    return payload
