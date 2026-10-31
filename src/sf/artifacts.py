"""Artifact store: large per-job run outputs (harness logs, prompts, sessions, validation logs) in S3.

Plain S3 only (Garage on evo-x2 in production), configured by `SF_S3_ENDPOINT`, `SF_S3_REGION`,
`SF_S3_BUCKET`, `SF_S3_ACCESS_KEY` and `SF_S3_SECRET_KEY`. A job's files live under
`runs/<run-id>/<stage>/<job-id>/`. Store failures are `git.InfraError`s: the job is requeued.
"""

import os
from pathlib import Path
from typing import Any

import boto3
from boto3.exceptions import Boto3Error
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from sf.git import InfraError

ENV = ("SF_S3_ENDPOINT", "SF_S3_REGION", "SF_S3_BUCKET", "SF_S3_ACCESS_KEY", "SF_S3_SECRET_KEY")


def _settings() -> dict[str, str]:
    missing = [name for name in ENV if not os.environ.get(name)]
    if missing:
        raise InfraError(f"artifact store not configured: set {', '.join(missing)}")
    return {name: os.environ[name] for name in ENV}


def _client() -> tuple[Any, str]:
    s = _settings()
    client = boto3.client(
        "s3", endpoint_url=s["SF_S3_ENDPOINT"], region_name=s["SF_S3_REGION"],
        aws_access_key_id=s["SF_S3_ACCESS_KEY"], aws_secret_access_key=s["SF_S3_SECRET_KEY"],
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
    )
    return client, s["SF_S3_BUCKET"]


def job_prefix(run_id: str, stage: str, job_id: int) -> str:
    return f"runs/{run_id}/{stage}/{job_id}/"


def upload_dir(local: Path, prefix: str) -> list[str]:
    """Upload every file under `local` to `prefix` + its relative path; returns the keys written."""
    client, bucket = _client()
    keys = []
    try:
        for path in sorted(p for p in local.rglob("*") if p.is_file()):
            key = prefix + path.relative_to(local).as_posix()
            client.upload_file(str(path), bucket, key)
            keys.append(key)
    except (Boto3Error, BotoCoreError, ClientError, OSError) as e:
        raise InfraError(f"artifact upload to s3://{bucket}/{prefix} failed: {e}") from e
    return keys


def list_keys(prefix: str) -> list[str]:
    client, bucket = _client()
    keys = []
    try:
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            keys += [o["Key"] for o in page.get("Contents", [])]
    except (BotoCoreError, ClientError) as e:
        raise InfraError(f"artifact listing of s3://{bucket}/{prefix} failed: {e}") from e
    return keys


def download_dir(prefix: str, local: Path) -> list[Path]:
    """Download every object under `prefix` into `local`, keeping relative paths."""
    client, bucket = _client()
    paths = []
    try:
        for key in list_keys(prefix):
            path = local / key[len(prefix):]
            path.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(path))
            paths.append(path)
    except (Boto3Error, BotoCoreError, ClientError) as e:
        raise InfraError(f"artifact download of s3://{bucket}/{prefix} failed: {e}") from e
    return paths


def presigned_url(key: str, expires: int = 3600) -> str:
    """A time-limited GET link for one artifact (for the dashboard)."""
    client, bucket = _client()
    return client.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires)
