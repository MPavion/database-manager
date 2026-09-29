from pathlib import Path


SCHEMA_SQL_PATH        = Path(__file__).resolve().parent / "sql" / "schema.sql"
SCHEMA_SQLITE_SQL_PATH = Path(__file__).resolve().parent / "sql" / "schema_sqlite.sql"


def load_schema_sql(backend: str = "postgresql") -> str:
    if backend == "sqlite":
        return SCHEMA_SQLITE_SQL_PATH.read_text(encoding="utf-8")
    return SCHEMA_SQL_PATH.read_text(encoding="utf-8")
