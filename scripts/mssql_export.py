#!/usr/bin/env python3
"""
MSSQL database exporter using pymssql.

Exports schema (CREATE TABLE, indexes) and data (INSERT statements) to stdout.
Designed as a replacement for mssql-scripter, which is archived and broken
on Ubuntu 22.04+ due to its bundled .NET runtime requiring OpenSSL 1.1.

Usage:
    python3 mssql_export.py <host> <port> <username> <password> <database>
"""

import sys
import pymssql

BATCH_SIZE = 1000  # Rows per INSERT batch


def quote_name(name):
    return f"[{name}]"


def sql_literal(value, col_type):
    """Convert a Python value to a SQL literal string."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, bytes):
        return "0x" + value.hex()
    # Dates, datetimes, strings — escape single quotes
    s = str(value).replace("'", "''")
    return f"'{s}'"


def get_tables(cursor):
    cursor.execute("""
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_NAME
    """)
    return [row[0] for row in cursor.fetchall()]


def get_columns(cursor, table):
    cursor.execute("""
        SELECT
            c.COLUMN_NAME,
            c.DATA_TYPE,
            c.CHARACTER_MAXIMUM_LENGTH,
            c.NUMERIC_PRECISION,
            c.NUMERIC_SCALE,
            c.IS_NULLABLE,
            c.COLUMN_DEFAULT,
            c.ORDINAL_POSITION
        FROM INFORMATION_SCHEMA.COLUMNS c
        WHERE c.TABLE_NAME = %s
        ORDER BY c.ORDINAL_POSITION
    """, (table,))
    return cursor.fetchall()


def get_primary_keys(cursor, table):
    cursor.execute("""
        SELECT c.COLUMN_NAME
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN INFORMATION_SCHEMA.CONSTRAINT_COLUMN_USAGE c
            ON tc.CONSTRAINT_NAME = c.CONSTRAINT_NAME
        WHERE tc.TABLE_NAME = %s
          AND tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
        ORDER BY c.COLUMN_NAME
    """, (table,))
    return [row[0] for row in cursor.fetchall()]


def get_indexes(cursor, table):
    cursor.execute("""
        SELECT
            i.name AS index_name,
            i.is_unique,
            STRING_AGG(c.name, ', ') WITHIN GROUP (ORDER BY ic.key_ordinal) AS columns
        FROM sys.indexes i
        JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
        JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
        WHERE OBJECT_NAME(i.object_id) = %s
          AND i.is_primary_key = 0
          AND i.type > 0
        GROUP BY i.name, i.is_unique
        ORDER BY i.name
    """, (table,))
    return cursor.fetchall()


def col_type_sql(col):
    col_name, data_type, char_max, num_prec, num_scale, is_nullable, col_default, _ = col
    dt = data_type.upper()

    if dt in ("CHAR", "VARCHAR", "NCHAR", "NVARCHAR"):
        if char_max is None or char_max == -1:
            type_str = f"{dt}(MAX)"
        else:
            type_str = f"{dt}({char_max})"
    elif dt in ("DECIMAL", "NUMERIC"):
        type_str = f"{dt}({num_prec},{num_scale})"
    elif dt in ("FLOAT", "REAL"):
        type_str = f"{dt}({num_prec})" if num_prec else dt
    else:
        type_str = dt

    nullable = "NULL" if is_nullable == "YES" else "NOT NULL"
    default = f" DEFAULT {col_default}" if col_default else ""
    return f"    {quote_name(col_name)} {type_str}{default} {nullable}"


def export_schema(cursor, table, print_fn):
    columns = get_columns(cursor, table)
    pks = get_primary_keys(cursor, table)

    print_fn(f"IF OBJECT_ID(N'{table}', N'U') IS NOT NULL DROP TABLE {quote_name(table)};")
    print_fn(f"CREATE TABLE {quote_name(table)} (")

    col_defs = [col_type_sql(c) for c in columns]

    if pks:
        pk_cols = ", ".join(quote_name(pk) for pk in pks)
        col_defs.append(f"    CONSTRAINT {quote_name('PK_' + table)} PRIMARY KEY ({pk_cols})")

    print_fn(",\n".join(col_defs))
    print_fn(");")
    print_fn("GO")
    print_fn("")


def export_indexes(cursor, table, print_fn):
    indexes = get_indexes(cursor, table)
    for idx_name, is_unique, cols in indexes:
        unique = "UNIQUE " if is_unique else ""
        print_fn(f"CREATE {unique}INDEX {quote_name(idx_name)} ON {quote_name(table)} ({cols});")
        print_fn("GO")
    if indexes:
        print_fn("")


def get_col_types(cursor, table):
    """Return a dict of column_name -> data_type for use in literal quoting."""
    columns = get_columns(cursor, table)
    return {c[0]: c[1].upper() for c in columns}, [c[0] for c in columns]


def export_data(cursor, table, print_fn):
    col_types, col_names = get_col_types(cursor, table)
    quoted_cols = ", ".join(quote_name(c) for c in col_names)

    # Use a separate cursor for data to avoid result set conflicts
    cursor.execute(f"SELECT {quoted_cols} FROM {quote_name(table)}")

    rows = cursor.fetchmany(BATCH_SIZE)
    if not rows:
        return

    print_fn(f"SET IDENTITY_INSERT {quote_name(table)} ON;")

    while rows:
        for row in rows:
            values = ", ".join(
                sql_literal(val, col_types[col_names[i]])
                for i, val in enumerate(row)
            )
            print_fn(f"INSERT INTO {quote_name(table)} ({quoted_cols}) VALUES ({values});")
        rows = cursor.fetchmany(BATCH_SIZE)

    print_fn(f"SET IDENTITY_INSERT {quote_name(table)} OFF;")
    print_fn("GO")
    print_fn("")


def main():
    if len(sys.argv) != 6:
        print(f"Usage: {sys.argv[0]} <host> <port> <username> <password> <database>",
              file=sys.stderr)
        sys.exit(1)

    host, port, username, password, database = sys.argv[1:6]

    try:
        conn = pymssql.connect(
            server=host,
            port=int(port),
            user=username,
            password=password,
            database=database,
            charset="UTF-8",
            tds_version="7.4",
        )
    except Exception as e:
        print(f"ERROR: Could not connect to MSSQL: {e}", file=sys.stderr)
        sys.exit(1)

    # Use line-buffered stdout so gzip pipe sees data as it flows
    out = sys.stdout

    def emit(line):
        out.write(line + "\n")

    cursor = conn.cursor()

    emit("-- EVE SDE MSSQL Export")
    emit("-- Generated by mssql_export.py")
    emit("")
    emit("SET NOCOUNT ON;")
    emit("GO")
    emit("")

    tables = get_tables(cursor)
    if not tables:
        print("ERROR: No tables found in database.", file=sys.stderr)
        sys.exit(1)

    print(f"Exporting {len(tables)} tables...", file=sys.stderr)

    # Phase 1: schema
    emit("-- ============================================================")
    emit("-- SCHEMA")
    emit("-- ============================================================")
    emit("")
    for table in tables:
        export_schema(cursor, table, emit)

    # Phase 2: indexes
    emit("-- ============================================================")
    emit("-- INDEXES")
    emit("-- ============================================================")
    emit("")
    for table in tables:
        export_indexes(cursor, table, emit)

    # Phase 3: data
    emit("-- ============================================================")
    emit("-- DATA")
    emit("-- ============================================================")
    emit("")
    for i, table in enumerate(tables, 1):
        print(f"  [{i}/{len(tables)}] {table}", file=sys.stderr)
        data_cursor = conn.cursor()
        export_data(data_cursor, table, emit)
        data_cursor.close()

    cursor.close()
    conn.close()

    print("Export complete.", file=sys.stderr)


if __name__ == "__main__":
    main()
