"""
Builds the page_index table after each sync.

Each page gets:
  keywords       – up to 20 terms drawn from properties, title, and content
  summary        – compact human-readable summary (≤ 400 chars)
  page_type      – inferred category (task / project / reference / note / page)
  content_preview – first 500 chars of the page's text, ready for quick display
  token_estimate  – rough Claude token count for budgeting

Claude loads the whole table in one call (get_index MCP tool) to build a
complete map of the workspace without fetching every full page.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.db.database import DatabaseManager

from src.core.config import logger
from src.db.change_tracking import property_preview_value

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "this", "that", "these", "those",
    "it", "its", "my", "your", "our", "their", "we", "i", "you", "he",
    "she", "they", "not", "no", "as", "if", "so", "up", "out", "about",
    "into", "than", "then", "when", "where", "who", "which", "what",
    "all", "each", "every", "both", "few", "more", "most", "other",
    "some", "such", "own", "same", "only", "just", "also", "new", "one",
    "two", "use", "used", "using", "get", "set", "add", "page", "item",
}


def _extract_keywords(title: str, summary: str, raw_json: dict) -> list[str]:
    keywords: set[str] = set()

    # 1. Property values — strongest signal (status, type, tags, project, etc.)
    props = (raw_json or {}).get("properties", {})
    if isinstance(props, dict):
        for prop in props.values():
            if not isinstance(prop, dict):
                continue
            val = property_preview_value(prop)
            if not val or len(val) > 80:
                continue
            for part in re.split(r"[,;/|]", val):
                part = part.strip().lower()
                if 2 < len(part) < 40 and part not in _STOPWORDS and not part.isdigit():
                    keywords.add(part)

    # 2. Title words
    for word in re.findall(r"\b[a-zA-Z]{3,}\b", title or ""):
        w = word.lower()
        if w not in _STOPWORDS:
            keywords.add(w)

    # 3. High-frequency content words (top 8)
    all_words = re.findall(r"\b[a-zA-Z]{4,}\b", (summary or "").lower())
    freq = Counter(w for w in all_words if w not in _STOPWORDS)
    for word, _ in freq.most_common(8):
        keywords.add(word)

    return sorted(keywords)[:20]


def _infer_page_type(raw_json: dict) -> str:
    props = (raw_json or {}).get("properties", {})
    if not isinstance(props, dict):
        return "page"

    names = {k.lower() for k in props}

    if any(k in names for k in ("status", "assignee", "due date", "due", "priority", "sprint")):
        return "task"
    if any(k in names for k in ("project", "client", "budget", "deadline", "owner")):
        return "project"
    if any(k in names for k in ("author", "published", "isbn", "source", "reference")):
        return "reference"
    if any(k in names for k in ("attendees", "meeting date", "action items", "minutes")):
        return "note"
    if any(k in names for k in ("tags", "category", "topic")):
        return "reference"

    # If any property has type "status", it's probably a task
    for prop in props.values():
        if isinstance(prop, dict) and prop.get("type") == "status":
            return "task"

    return "page"


def _make_summary(title: str, ai_summary: str, raw_json: dict) -> str:
    if ai_summary and len(ai_summary) > 20:
        return ai_summary[:400].strip()

    # Fall back to title + key property values
    props = (raw_json or {}).get("properties", {})
    parts = [title] if title else []
    if isinstance(props, dict):
        for name, prop in list(props.items())[:8]:
            if not isinstance(prop, dict) or prop.get("type") == "title":
                continue
            val = property_preview_value(prop)
            if val:
                parts.append(f"{name}: {val}")
    return " | ".join(parts)[:400]


def build_index_entry(notion_id: str, title: str,
                       ai_summary: str, raw_json: dict) -> dict:
    keywords       = _extract_keywords(title, ai_summary, raw_json)
    summary        = _make_summary(title, ai_summary, raw_json)
    page_type      = _infer_page_type(raw_json)
    content_preview = (ai_summary or summary or title or "")[:500]
    token_estimate  = len((title or "") + (ai_summary or "")) // 4

    return {
        "notion_id":       notion_id,
        "title":           title,
        "keywords":        keywords,
        "summary":         summary,
        "page_type":       page_type,
        "content_preview": content_preview,
        "token_estimate":  token_estimate,
    }


def rebuild_index(db: "DatabaseManager") -> int:
    """Rebuild the entire page_index table. Returns the number of entries indexed."""
    if not db.is_connected():
        return 0

    try:
        rows = db._execute(
            "SELECT notion_id, title, ai_summary, raw_json FROM workspace_mirror WHERE is_active = TRUE",
            fetch="all",
        )
    except Exception as exc:
        logger.error(f"Index rebuild: failed to fetch pages: {exc}")
        return 0

    count = 0
    for row in rows or []:
        entry = build_index_entry(
            row["notion_id"],
            row.get("title") or "",
            row.get("ai_summary") or "",
            row.get("raw_json") or {},
        )
        try:
            db.upsert_index_entry(**entry)
            count += 1
        except Exception as exc:
            logger.warning(f"Index rebuild: could not index {row['notion_id']}: {exc}")

    # Remove entries for pages that are no longer active
    active_ids = [row["notion_id"] for row in (rows or [])]
    if active_ids:
        db.clear_stale_index(active_ids)

    logger.info(f"Page index rebuilt: {count} entries.")
    return count
