from __future__ import annotations

import gzip
import io
import tarfile
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from reuse_radar.clients.arxiv import (
    ArxivAccessDeniedError,
    ArxivClient,
    ArxivError,
    ArxivSourceError,
    unpack_source,
)
from reuse_radar.clients.inspire import SlidingWindowRateLimiter

from .conftest import FakeClock

MAIN_TEX = b"\\documentclass{article}\\begin{document}Hi\\end{document}"


def _tarball(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(buf.getvalue())


# --- unpacking --------------------------------------------------------------------------------


def test_unpack_tarball_keeps_only_tex() -> None:
    payload = _tarball({"./main.tex": MAIN_TEX, "sec/a.tex": b"A", "fig.pdf": b"%PDF-1.4"})
    source = unpack_source("2401.00001", payload)
    assert source.kind == "latex"
    assert sorted(source.tex_files) == ["main.tex", "sec/a.tex"]


def test_unpack_single_gzipped_tex() -> None:
    source = unpack_source("2401.00001", gzip.compress(MAIN_TEX))
    assert source.tex_files == {"main.tex": MAIN_TEX.decode()}


def test_unpack_pdf_only() -> None:
    assert unpack_source("2401.00001", b"%PDF-1.5 ...").kind == "pdf_only"
    assert unpack_source("2401.00001", gzip.compress(b"%PDF-1.5")).kind == "pdf_only"


def test_unpack_latin1_source() -> None:
    latin1 = "\\documentclass{article}\\begin{document}Caf\xe9\\end{document}".encode("latin-1")
    source = unpack_source("2401.00001", gzip.compress(latin1))
    assert "Caf\xe9" in source.tex_files["main.tex"]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"<html>not a payload</html>", "unrecognised"),
        (_tarball({"fig.pdf": b"%PDF"}), "no .tex files"),
        (_tarball({"../escape.tex": b"x"}), "unsafe path"),
        (gzip.compress(b"plain text, not latex"), "not recognisable LaTeX"),
    ],
)
def test_unpack_rejects_bad_payloads(payload: bytes, message: str) -> None:
    with pytest.raises(ArxivSourceError, match=message):
        unpack_source("2401.00001", payload)


# --- client -----------------------------------------------------------------------------------

Handler = Callable[[httpx.Request], httpx.Response]
MakeArxiv = Callable[[Handler], tuple[ArxivClient, list[httpx.Request]]]


@pytest.fixture
def make_client(tmp_path: Path, clock: FakeClock) -> MakeArxiv:
    def _make(handler: Handler) -> tuple[ArxivClient, list[httpx.Request]]:
        seen: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        client = ArxivClient(
            contact_email="test@example.org",
            cache_dir=tmp_path,
            http=httpx.Client(transport=httpx.MockTransport(record), follow_redirects=True),
            limiter=SlidingWindowRateLimiter(
                max_requests=1, window_s=3.0, min_interval_s=3.0, clock=clock, sleep=clock.sleep
            ),
            sleep=clock.sleep,
        )
        return client, seen

    return _make


def _redirecting_ok(request: httpx.Request) -> httpx.Response:
    """Serve the tarball at /src/<id>; mimic export.arxiv.org's /e-print/ -> /src/ redirect."""
    if request.url.path.startswith("/e-print/"):
        return httpx.Response(301, headers={"Location": request.url.path.replace("e-print", "src")})
    return httpx.Response(200, content=_tarball({"main.tex": MAIN_TEX}))


def test_download_requests_src_once_and_caches(make_client: MakeArxiv) -> None:
    client, seen = make_client(_redirecting_ok)
    first = client.get_source("2401.00001")
    second = client.get_source("2401.00001")
    assert first == second and first.kind == "latex"
    assert [r.url.path for r in seen] == ["/src/2401.00001"]  # one request, then cached
    assert seen[0].url.host == "export.arxiv.org"
    assert "mailto:test@example.org" in seen[0].headers["User-Agent"]


def test_requests_are_spaced_three_seconds(make_client: MakeArxiv, clock: FakeClock) -> None:
    client, _ = make_client(lambda r: httpx.Response(200, content=_tarball({"m.tex": MAIN_TEX})))
    client.get_source("2401.00001")
    client.get_source("2401.00002")
    client.get_source("2401.00003")
    assert clock.sleeps == [3.0, 3.0]


def test_403_stops_immediately(make_client: MakeArxiv) -> None:
    client, seen = make_client(lambda r: httpx.Response(403))
    with pytest.raises(ArxivAccessDeniedError):
        client.get_source("2401.00001")
    assert len(seen) == 1


def test_404_is_a_per_paper_source_error(make_client: MakeArxiv) -> None:
    client, _ = make_client(lambda r: httpx.Response(404))
    with pytest.raises(ArxivSourceError, match="404"):
        client.get_source("2401.00001")


def test_429_backs_off_then_succeeds(make_client: MakeArxiv, clock: FakeClock) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "10"})
        return httpx.Response(200, content=_tarball({"m.tex": MAIN_TEX}))

    client, _ = make_client(handler)
    assert client.get_source("2401.00001").kind == "latex"
    assert 10.0 in clock.sleeps


def test_redirect_off_export_host_rejected(make_client: MakeArxiv) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "export.arxiv.org":
            return httpx.Response(302, headers={"Location": "https://arxiv.org/src/2401.00001"})
        return httpx.Response(200, content=b"%PDF")

    client, _ = make_client(handler)
    with pytest.raises(ArxivError, match="off the export host"):
        client.get_source("2401.00001")


@pytest.mark.parametrize("bad", ["", "not-an-id", "2401.1", "../../etc/passwd"])
def test_invalid_ids_rejected_before_any_request(make_client: MakeArxiv, bad: str) -> None:
    client, seen = make_client(_redirecting_ok)
    with pytest.raises(ValueError):
        client.get_source(bad)
    assert seen == []


def test_old_style_and_versioned_ids_accepted(make_client: MakeArxiv) -> None:
    client, _ = make_client(_redirecting_ok)
    client.get_source("hep-ex/0601001")
    client.get_source("2401.05299v2")
