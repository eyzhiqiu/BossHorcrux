from __future__ import annotations

import argparse
import json
import os
from typing import Any

import pymysql

from .database_schema_loader import MySqlSchemaReader, build_mysql_connection_kwargs


_ENV_CANDIDATES = {
    "host": ["PAGE_DOC_BOOK_DB_HOST", "MYSQL_HOST", "MYSQL_IP", "MYSQLIP"],
    "port": ["PAGE_DOC_BOOK_DB_PORT", "MYSQL_PORT"],
    "user": ["PAGE_DOC_BOOK_DB_USER", "MYSQL_USER", "MYSQL_USER_NAME", "MYSQLUSERNAME"],
    "password": ["PAGE_DOC_BOOK_DB_PASSWORD", "MYSQL_PASSWORD", "MYSQL_USER_PASS", "MYSQLUSERPASS"],
    "database": ["PAGE_DOC_BOOK_DB_NAME", "MYSQL_DATABASE"],
    "schemas": ["PAGE_DOC_BOOK_DB_SCHEMAS", "MYSQL_SCHEMAS", "MYSQL_SCHEMA", "MYSQL_DATABASE", "MYSQL_DB_NAME"],
}


def _build_connection_config(args: argparse.Namespace) -> dict[str, object]:
    config: dict[str, object] = {}
    for key in ("host", "port", "user", "password", "database"):
        value = getattr(args, key)
        if value is not None:
            config[key] = value
    return config


def _build_env_resolution_report(connection_config: dict[str, object] | None = None) -> dict[str, Any]:
    config = dict(connection_config or {})
    report: dict[str, Any] = {}
    for key, env_names in _ENV_CANDIDATES.items():
        override_value = config.get(key if key != "schemas" else "schema_names")
        env_values = {env_name: os.getenv(env_name) for env_name in env_names}
        selected_source = "cli_override" if override_value not in (None, "", []) else ""
        selected_value: Any = override_value if selected_source else None
        if not selected_source:
            for env_name in env_names:
                env_value = env_values[env_name]
                if env_value not in (None, ""):
                    selected_source = env_name
                    selected_value = env_value
                    break
        if not selected_source:
            if key == "port":
                selected_source = "default"
                selected_value = "3306"
            else:
                selected_source = "unresolved"
                selected_value = None
        report[key] = {
            "override_value": override_value,
            "env_values": env_values,
            "selected_source": selected_source,
            "selected_value": selected_value,
        }
    return report


def build_connection_probe_report(
    connection_config: dict[str, object] | None = None,
    schema_names: list[str] | None = None,
    table_name: str | None = None,
) -> dict[str, Any]:
    connection_kwargs = build_mysql_connection_kwargs(connection_config)
    reader = MySqlSchemaReader(connection_config=connection_config, schema_names=schema_names)
    report: dict[str, Any] = {
        "env_resolution": _build_env_resolution_report(
            {
                **dict(connection_config or {}),
                **({"schema_names": schema_names} if schema_names else {}),
            }
        ),
        "connection_kwargs": {
            "host": connection_kwargs["host"],
            "port": connection_kwargs["port"],
            "user": connection_kwargs["user"],
            "password": connection_kwargs["password"],
            "database": connection_kwargs["database"],
            "charset": connection_kwargs["charset"],
            "autocommit": connection_kwargs["autocommit"],
        }
    }

    with pymysql.connect(**connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT DATABASE() AS current_db")
            report["current_database"] = (cursor.fetchone() or {}).get("current_db")

    resolved_schema_names = reader._resolve_schema_names()
    report["resolved_schemas"] = resolved_schema_names

    snapshot = reader.read()
    report["database_count"] = len(snapshot["databases"])
    report["table_count"] = len(snapshot["tables"])
    report["field_count"] = len(snapshot["db_fields"])

    if table_name:
        matched_fields = [field for field in snapshot["db_fields"] if str(field.get("table_id") or "") == table_name]
        matched_table = next((table for table in snapshot["tables"] if str(table.get("table_id") or "") == table_name), None)
        report["table_probe"] = {
            "table_name": table_name,
            "exists": matched_table is not None,
            "field_count": len(matched_fields),
            "index_names": [str(item.get("index_name") or "") for item in (matched_table or {}).get("indexes", [])],
        }

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="测试 page_centric_doc_book skill 中通过环境变量解析的 MySQL 连接参数与连通性。")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--user")
    parser.add_argument("--password")
    parser.add_argument("--database")
    parser.add_argument("--schema", action="append", dest="schemas", default=None)
    parser.add_argument("--table", dest="table_name")
    args = parser.parse_args(argv)

    report = build_connection_probe_report(
        connection_config=_build_connection_config(args),
        schema_names=args.schemas,
        table_name=args.table_name,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
