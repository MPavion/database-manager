from pathlib import Path


SCHEMA_SQL_PATH = Path(__file__).resolve().parent / "sql" / "schema.sql"


def load_schema_sql() -> str:
    return SCHEMA_SQL_PATH.read_text(encoding="utf-8")
