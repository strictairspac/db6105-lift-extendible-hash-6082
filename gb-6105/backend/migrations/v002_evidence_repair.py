from sqlalchemy import inspect, text

from src.models import audit, cases, evidence, reviews, runs, users  # noqa: F401  Register the tables.
from src.models.base import metadata


def upgrade(connection):
    # 新库直接按声明式模型建全量表；旧库则增量补列、补表，保证幂等。
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    missing_tables = [table for table in metadata.sorted_tables if table.name not in existing_tables]
    if missing_tables:
        metadata.create_all(connection, tables=missing_tables)

    run_columns = {column["name"] for column in inspector.get_columns("runs")}
    if "evidence_state" not in run_columns:
        connection.execute(text(
            "ALTER TABLE runs ADD COLUMN evidence_state VARCHAR(16) "
            "NOT NULL DEFAULT 'intact'"
        ))
