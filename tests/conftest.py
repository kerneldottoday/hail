import pytest


@pytest.fixture(autouse=True)
def isolated_hail(tmp_path, monkeypatch):
    """Keep the journal in a temp dir and run as if called by the user, never by an agent."""
    monkeypatch.setenv("HAIL_HOME", str(tmp_path / "hail-home"))
    for name in ("HAIL_THREAD", "HAIL_HOP", "HAIL_SELF", "HAIL_TELEGRAM_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def agents_file(tmp_path, monkeypatch):
    """Point HAIL_CONFIG at a temp file and return a writer for it."""
    path = tmp_path / "agents.toml"
    monkeypatch.setenv("HAIL_CONFIG", str(path))

    def write(text: str):
        path.write_text(text, encoding="utf-8")
        return path

    return write


class FakeProc:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture
def fake_run(monkeypatch):
    """Replace subprocess.run; records calls and returns a configurable FakeProc."""
    calls = []
    state = {"proc": FakeProc(b"hello")}

    def run(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        result = state["proc"]
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr("subprocess.run", run)
    return calls, state
