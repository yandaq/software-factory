import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    sh(path, "git", "init", "-q", "-b", "main")
    sh(path, "git", "config", "user.email", "t@example.com")
    sh(path, "git", "config", "user.name", "t")
    (path / "README.md").write_text("# target\n")
    sh(path, "git", "add", "-A")
    sh(path, "git", "commit", "-qm", "init")
    return path


@pytest.fixture
def factory(tmp_path: Path) -> Path:
    f = tmp_path / "factory"
    f.mkdir()
    shutil.copytree(ROOT / "agents", f / "agents")
    shutil.copytree(ROOT / "templates", f / "templates")
    shutil.copy(ROOT / ".gitignore", f / ".gitignore")
    # Workflow fixtures use fake Pi unless a test explicitly selects a native harness.
    from sf.agents import AGENT_NAMES, load_agent
    from sf.dashboard import set_agent_config

    for name in AGENT_NAMES:
        agent = load_agent(f / "agents", name)
        assert set_agent_config(f / "agents", name, agent.model, agent.thinking, "pi") is None
    init_repo(f)
    return f


@pytest.fixture
def target(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "target")


@pytest.fixture
def fake_pi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    script = tmp_path / "script.json"
    monkeypatch.setenv("SF_PI", str(ROOT / "tests" / "fake_pi.py"))
    monkeypatch.setenv("FAKE_PI_SCRIPT", str(script))

    def configure(steps: dict) -> Path:
        script.write_text(json.dumps(steps))
        return script

    return configure


def calls(script: Path) -> list[dict]:
    p = script.with_suffix(".calls.jsonl")
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub agents-default downloads; returns the URLs requested."""
    urls: list[str] = []

    def fake_download(url: str) -> str:
        urls.append(url)
        return f"# downloaded from {url}\n"

    monkeypatch.setattr("sf.target.download", fake_download)
    return urls


@pytest.fixture
def fake_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    script = tmp_path / "native-script.json"
    for variable in ("SF_CLAUDE", "SF_CODEX"):
        monkeypatch.setenv(variable, str(ROOT / "tests" / "fake_harness.py"))
    monkeypatch.setenv("FAKE_HARNESS_SCRIPT", str(script))

    def configure(steps: dict) -> Path:
        script.write_text(json.dumps(steps))
        return script

    return configure


def _start_pg_ctl(tmp: Path) -> tuple[str, list[str]] | None:
    """A throwaway local cluster on a unix socket: (admin url, stop command), or None without initdb."""
    if not (shutil.which("initdb") and shutil.which("pg_ctl")):
        return None
    data, sock = tmp / "pgdata", tmp / "sock"
    sock.mkdir()
    subprocess.run(["initdb", "-D", str(data), "-U", "postgres", "-A", "trust"], check=True, capture_output=True)
    opts = f"-c listen_addresses='' -k {sock} -F"
    subprocess.run(["pg_ctl", "-D", str(data), "-o", opts, "-w", "-l", str(tmp / "pg.log"), "start"],
                   check=True, capture_output=True)
    return f"postgresql://postgres@/postgres?host={sock}", ["pg_ctl", "-D", str(data), "-m", "immediate", "stop"]


def _start_docker() -> tuple[str, list[str]] | None:
    if not shutil.which("docker"):
        return None
    cid = subprocess.run(["docker", "run", "-d", "--rm", "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-p",
                          "127.0.0.1::5432", "postgres:16"], check=True, capture_output=True, text=True).stdout.strip()
    port = subprocess.run(["docker", "port", cid, "5432"], check=True, capture_output=True,
                          text=True).stdout.split(":")[-1].strip()
    return f"postgresql://postgres@127.0.0.1:{port}/postgres", ["docker", "stop", cid]


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory: pytest.TempPathFactory):
    """Admin URL of a Postgres server: SF_TEST_DATABASE_URL, else a temp pg_ctl cluster, else docker."""
    import os
    import time

    import psycopg

    if url := os.environ.get("SF_TEST_DATABASE_URL"):
        yield url
        return
    errors, stop = [], None
    for start in (lambda: _start_pg_ctl(tmp_path_factory.mktemp("pg")), _start_docker):
        try:
            if started := start():
                url, stop = started
                break
        except (subprocess.CalledProcessError, OSError) as e:
            errors.append(f"{e}: {getattr(e, 'stderr', '')}")
    else:
        pytest.skip(f"no Postgres: set SF_TEST_DATABASE_URL, or install initdb/pg_ctl or docker ({errors})")
    for _ in range(60):  # docker's server accepts connections a little after the container starts
        try:
            psycopg.connect(url).close()
            break
        except psycopg.OperationalError:
            time.sleep(0.5)
    yield url
    subprocess.run(stop, check=False, capture_output=True)


@pytest.fixture
def db_url(pg_server: str):
    """A fresh, empty database for one test, dropped afterwards."""
    import uuid

    import psycopg
    from psycopg.conninfo import make_conninfo

    name = f"sf_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    yield make_conninfo(pg_server, dbname=name)
    with psycopg.connect(pg_server, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
def db(db_url: str):
    """A migrated store connection on an isolated database."""
    from sf import store

    conn = store.connect(db_url)
    store.migrate(conn)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def sf_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the per-job clone cache out of ~/.cache."""
    monkeypatch.setenv("SF_CACHE_DIR", str(tmp_path / "sf-cache"))
    return tmp_path / "sf-cache"


@pytest.fixture
def remote(tmp_path: Path, target: Path) -> Path:
    """A bare repository acting as the target's `origin`, seeded from it."""
    bare = tmp_path / "remote.git"
    sh(tmp_path, "git", "clone", "-q", "--bare", str(target), str(bare))
    sh(target, "git", "remote", "add", "origin", str(bare))
    return bare


@pytest.fixture(scope="session")
def s3_server():
    """A local moto S3 server; tests never reach the real artifact store."""
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture(autouse=True)
def s3(s3_server: str, monkeypatch: pytest.MonkeyPatch):
    """Point SF_S3_* at a fresh bucket on the moto server; returns a boto3 client and the bucket."""
    import uuid

    import boto3

    bucket = f"sf-test-{uuid.uuid4().hex[:12]}"
    env = {"SF_S3_ENDPOINT": s3_server, "SF_S3_REGION": "garage", "SF_S3_BUCKET": bucket,
           "SF_S3_ACCESS_KEY": "test", "SF_S3_SECRET_KEY": "test"}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    client = boto3.client("s3", endpoint_url=s3_server, region_name="garage", aws_access_key_id="test",
                          aws_secret_access_key="test")
    client.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "garage"})
    return client, bucket
