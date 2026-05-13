"""One-shot probe: MinIO health + bucket list + MySQL auth/db check.

Used during the local end-to-end smoke setup to discover the actual
credentials and bucket state without polluting the terminal with
multi-line shell heredocs.
"""

from __future__ import annotations

import sys

import boto3
import pymysql


def probe_minio() -> None:
    print("=== MinIO ===")
    try:
        s3 = boto3.client(
            "s3",
            endpoint_url="http://127.0.0.1:30900",
            aws_access_key_id="minioadmin",
            aws_secret_access_key="minioadmin",
            region_name="us-east-1",
        )
        buckets = [b["Name"] for b in s3.list_buckets()["Buckets"]]
        print(f"OK: minioadmin/minioadmin works; buckets={buckets}")
    except Exception as exc:  # noqa: BLE001 -- diagnostic
        print(f"FAIL: {type(exc).__name__}: {exc}")


def probe_mysql(port: int, label: str) -> None:
    print(f"\n=== MySQL {label} (port {port}) ===")
    candidates = [
        ("lcp", "lcp_dev_pwd"),
        ("lcp", "lcp"),
        ("root", "root"),
        ("root", "rootpw"),
        ("root", "mysql"),
        ("root", ""),
    ]
    for user, pwd in candidates:
        try:
            conn = pymysql.connect(
                host="127.0.0.1",
                port=port,
                user=user,
                password=pwd,
                connect_timeout=3,
            )
            cur = conn.cursor()
            cur.execute("SHOW DATABASES")
            dbs = [row[0] for row in cur.fetchall()]
            has_lcp = "lcp" in dbs
            print(f"OK: {user}/{pwd!r}; databases={dbs}; lcp_db_exists={has_lcp}")
            if has_lcp:
                cur.execute("USE lcp")
                cur.execute("SHOW TABLES")
                tables = [row[0] for row in cur.fetchall()]
                print(f"     tables in lcp: {tables}")
            conn.close()
            return
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {user}/{pwd!r}: {type(exc).__name__}")
    print("ALL CANDIDATES FAILED")


def main() -> int:
    probe_minio()
    probe_mysql(30306, "mysql-cdc")
    probe_mysql(30307, "mysql-target")
    return 0


if __name__ == "__main__":
    sys.exit(main())
