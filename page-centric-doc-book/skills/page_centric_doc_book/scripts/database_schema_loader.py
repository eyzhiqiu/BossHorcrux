from __future__ import annotations

import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Callable

import pymysql
from pymysql.err import MySQLError
from pymysql.cursors import DictCursor

_SYSTEM_SCHEMAS = {"information_schema", "mysql", "performance_schema", "sys"}
_MYSQL_SOURCE = "mysql:information_schema"


def build_mysql_connection_kwargs(connection_config: dict[str, object] | None = None) -> dict[str, object]:
    config = dict(connection_config or {})
    host = str(
        config.get("host")
        or os.getenv("PAGE_DOC_BOOK_DB_HOST")
        or os.getenv("MYSQL_HOST")
        or os.getenv("MYSQL_IP")
        or os.getenv("MYSQLIP")
        or ""
    ).strip()
    user = str(
        config.get("user")
        or os.getenv("PAGE_DOC_BOOK_DB_USER")
        or os.getenv("MYSQL_USER")
        or os.getenv("MYSQL_USER_NAME")
        or os.getenv("MYSQLUSERNAME")
        or ""
    ).strip()
    password = config.get("password")
    if password is None:
        password = (
            os.getenv("PAGE_DOC_BOOK_DB_PASSWORD")
            or os.getenv("MYSQL_PASSWORD")
            or os.getenv("MYSQL_USER_PASS")
            or os.getenv("MYSQLUSERPASS")
            or ""
        )
    database = config.get("database")
    if database is None:
        database = os.getenv("PAGE_DOC_BOOK_DB_NAME") or os.getenv("MYSQL_DATABASE") or None
    port_value = (
        config.get("port")
        or os.getenv("PAGE_DOC_BOOK_DB_PORT")
        or os.getenv("MYSQL_PORT")
        or "3306"
    )

    if not host or not user:
        raise RuntimeError("MySQL 连接配置缺失：必须显式提供 host 和 user，禁止回退到本地默认实例。")

    return {
        "host": host,
        "port": int(port_value),
        "user": user,
        "password": password,
        "database": database,
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
        "autocommit": True,
    }


class MySqlSchemaReader:
    """Read real MySQL schema metadata from information_schema."""

    def __init__(
        self,
        query_executor: Callable[[str, tuple[object, ...]], list[dict[str, object]]] | None = None,
        schema_names: list[str] | tuple[str, ...] | None = None,
        connection_config: dict[str, object] | None = None,
    ) -> None:
        self._query_executor = query_executor or self._query_rows
        self._schema_names = [name.strip() for name in (schema_names or []) if str(name or "").strip()]
        self._connection_config = dict(connection_config or {})

    def read(self) -> dict[str, list[dict[str, object]]]:
        try:
            schema_names = self._resolve_schema_names()
            if not schema_names:
                raise RuntimeError("未解析到可读取的 MySQL schema，已停止生成数据库字典。")

            table_rows = self._query_executor(self._tables_sql(schema_names), tuple(schema_names))
            column_rows = self._query_executor(self._columns_sql(schema_names), tuple(schema_names))
            index_rows = self._query_executor(self._indexes_sql(schema_names), tuple(schema_names))
            return self._build_snapshot(schema_names, table_rows, column_rows, index_rows)
        except MySQLError as exc:
            raise RuntimeError(f"读取真实 MySQL schema 失败：{exc}") from exc

    def _resolve_schema_names(self) -> list[str]:
        if self._schema_names:
            return sorted(dict.fromkeys(self._schema_names))

        configured = (
            os.getenv("PAGE_DOC_BOOK_DB_SCHEMAS")
            or os.getenv("MYSQL_SCHEMAS")
            or os.getenv("MYSQL_SCHEMA")
            or os.getenv("MYSQL_DATABASE")
            or os.getenv("MYSQL_DB_NAME")
        )
        if configured:
            return sorted(dict.fromkeys(part.strip() for part in configured.split(",") if part.strip()))

        current_rows = self._query_executor("SELECT DATABASE() AS current_db", ())
        current_db = str(((current_rows or [{}])[0]).get("current_db") or "").strip()
        if current_db and current_db not in _SYSTEM_SCHEMAS:
            return [current_db]

        schema_rows = self._query_executor(
            """
            SELECT SCHEMA_NAME
            FROM information_schema.SCHEMATA
            WHERE SCHEMA_NAME NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
            ORDER BY SCHEMA_NAME
            """.strip(),
            (),
        )
        return [
            str(row.get("SCHEMA_NAME") or "").strip()
            for row in schema_rows
            if str(row.get("SCHEMA_NAME") or "").strip()
        ]

    def _build_snapshot(
        self,
        schema_names: list[str],
        table_rows: list[dict[str, object]],
        column_rows: list[dict[str, object]],
        index_rows: list[dict[str, object]],
    ) -> dict[str, list[dict[str, object]]]:
        tables: dict[str, dict[str, object]] = {}
        db_fields: list[dict[str, object]] = []
        schema_meta: dict[str, dict[str, set[str]]] = defaultdict(lambda: {"tables": set(), "sources": set()})

        for row in table_rows:
            table_id, schema, table_name = self._table_identity(
                str(row.get("TABLE_SCHEMA") or "").strip(),
                str(row.get("TABLE_NAME") or "").strip(),
            )
            if not table_id:
                continue
            tables[table_id] = {
                "table_id": table_id,
                "schema": schema,
                "table_name": table_name,
                "source_files": [],
                "indexes": [],
            }
            schema_meta[schema]["tables"].add(table_id)

        for row in column_rows:
            table_id, schema, table_name = self._table_identity(
                str(row.get("TABLE_SCHEMA") or "").strip(),
                str(row.get("TABLE_NAME") or "").strip(),
            )
            if not table_id:
                continue
            tables.setdefault(
                table_id,
                {
                    "table_id": table_id,
                    "schema": schema,
                    "table_name": table_name,
                    "source_files": [],
                    "indexes": [],
                },
            )
            schema_meta[schema]["tables"].add(table_id)
            db_fields.append(
                {
                    "field_id": f"{table_id}.{str(row.get('COLUMN_NAME') or '').strip()}",
                    "table_id": table_id,
                    "name": str(row.get("COLUMN_NAME") or "").strip(),
                    "type": str(row.get("COLUMN_TYPE") or "").strip(),
                    "comment": str(row.get("COLUMN_COMMENT") or "").strip(),
                    "nullable": str(row.get("IS_NULLABLE") or "").strip().upper() == "YES",
                    "default": "" if row.get("COLUMN_DEFAULT") is None else str(row.get("COLUMN_DEFAULT")),
                    "source_file": _MYSQL_SOURCE,
                }
            )

        index_map: dict[tuple[str, str], dict[str, object]] = {}
        for row in index_rows:
            table_id, schema, table_name = self._table_identity(
                str(row.get("TABLE_SCHEMA") or "").strip(),
                str(row.get("TABLE_NAME") or "").strip(),
            )
            if not table_id:
                continue
            tables.setdefault(
                table_id,
                {
                    "table_id": table_id,
                    "schema": schema,
                    "table_name": table_name,
                    "source_files": [],
                    "indexes": [],
                },
            )
            schema_meta[schema]["tables"].add(table_id)
            index_name = str(row.get("INDEX_NAME") or "").strip()
            if not index_name:
                continue
            key = (table_id, index_name)
            index_record = index_map.setdefault(
                key,
                {
                    "index_name": index_name,
                    "unique": int(row.get("NON_UNIQUE") or 0) == 0,
                    "columns": [],
                    "source_file": _MYSQL_SOURCE,
                },
            )
            column_name = str(row.get("COLUMN_NAME") or "").strip()
            if column_name:
                index_record["columns"].append(column_name)

        for (table_id, _), index_record in sorted(index_map.items()):
            tables[table_id]["indexes"].append(index_record)

        databases = [
            {
                "database_id": schema,
                "table_ids": sorted(meta["tables"]),
                "source_files": sorted(meta["sources"]),
            }
            for schema, meta in sorted(schema_meta.items())
            if schema in schema_names or meta["tables"]
        ]

        return {
            "databases": databases,
            "tables": [tables[key] for key in sorted(tables)],
            "db_fields": db_fields,
        }

    def _query_rows(self, sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
        connection_kwargs = self._build_connection_kwargs()
        with pymysql.connect(**connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                return list(cursor.fetchall())

    def _build_connection_kwargs(self) -> dict[str, object]:
        return build_mysql_connection_kwargs(self._connection_config)

    def _tables_sql(self, schema_names: list[str]) -> str:
        placeholders = ", ".join(["%s"] * len(schema_names))
        return f"""
            SELECT TABLE_SCHEMA, TABLE_NAME
            FROM information_schema.TABLES
            WHERE TABLE_TYPE = 'BASE TABLE'
              AND TABLE_SCHEMA IN ({placeholders})
            ORDER BY TABLE_SCHEMA, TABLE_NAME
        """.strip()

    def _columns_sql(self, schema_names: list[str]) -> str:
        placeholders = ", ".join(["%s"] * len(schema_names))
        return f"""
            SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_COMMENT
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA IN ({placeholders})
            ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
        """.strip()

    def _indexes_sql(self, schema_names: list[str]) -> str:
        placeholders = ", ".join(["%s"] * len(schema_names))
        return f"""
            SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, NON_UNIQUE, COLUMN_NAME, SEQ_IN_INDEX
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA IN ({placeholders})
            ORDER BY TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
        """.strip()

    def _table_identity(self, schema: str, table_name: str) -> tuple[str, str, str]:
        schema = schema.strip()
        table_name = table_name.strip()
        if not schema or not table_name:
            return "", "", ""
        return f"{schema}.{table_name}", schema, table_name


class DatabaseSchemaLoader:
    """Load real MySQL schema metadata and Go models."""

    def __init__(self, schema_reader: MySqlSchemaReader | None = None) -> None:
        self.schema_reader = schema_reader or MySqlSchemaReader()

    def load(self, backend_path: Path | str) -> dict[str, list[dict[str, object]]]:
        backend_root = Path(backend_path)
        snapshot = dict(self.schema_reader.read())
        snapshot["go_models"] = self._collect_go_models(backend_root)
        return snapshot

    def _collect_go_models(self, backend_root: Path) -> list[dict[str, object]]:
        go_models: list[dict[str, object]] = []
        struct_pattern = re.compile(r"type\s+(?P<name>\w+)\s+struct\s*\{(?P<body>.*?)\}", re.DOTALL)
        for go_file in sorted(backend_root.rglob("*.go")):
            content = go_file.read_text(encoding="utf-8", errors="ignore")
            relative_path = self._relative_path(go_file, backend_root)
            for match in struct_pattern.finditer(content):
                model_name = match.group("name")
                body = match.group("body")
                fields = self._parse_go_fields(body)
                if not fields:
                    continue
                go_models.append(
                    {
                        "model_id": model_name.lower(),
                        "model_name": model_name,
                        "source_file": relative_path,
                        "fields": fields,
                    }
                )
        return go_models

    def _parse_go_fields(self, body: str) -> list[dict[str, str]]:
        fields: list[dict[str, str]] = []
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("//"):
                continue
            tag_match = re.search(r"`([^`]*)`", line)
            if not tag_match:
                continue
            db_tag_match = re.search(r'db:"(?P<column>[^"]+)"', tag_match.group(1))
            if not db_tag_match:
                continue
            parts = line[: tag_match.start()].split()
            if len(parts) < 2:
                continue
            fields.append(
                {
                    "field_name": parts[0],
                    "column": db_tag_match.group("column"),
                    "type": parts[1],
                }
            )
        return fields

    def _relative_path(self, path: Path, root: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()
