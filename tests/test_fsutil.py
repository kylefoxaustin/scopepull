"""replace_retry / unlink_retry: outlast a Windows Defender hold on a fresh file."""

from __future__ import annotations

import pytest

from scopepull import fsutil


def _busy_then_ok(times: int):
    """An os.replace / Path.unlink stand-in: PermissionError `times` times, then works."""
    calls = {"n": 0}

    def fake(*a, **kw):
        calls["n"] += 1
        if calls["n"] <= times:
            raise PermissionError(
                32, "The process cannot access the file because it is being used by another process"
            )

    return fake, calls


def test_replace_retries_through_a_scanner_hold(tmp_path, monkeypatch):
    src, dst = tmp_path / "a.zip.partial", tmp_path / "a.zip"
    src.write_bytes(b"x")
    real = type(src).replace
    fake, calls = _busy_then_ok(3)

    def flaky(self, target):
        fake()
        return real(self, target)

    monkeypatch.setattr(type(src), "replace", flaky)
    fsutil.replace_retry(src, dst, delays=[0.0, 0.0, 0.0, 0.0])
    assert calls["n"] == 4 and dst.exists() and not src.exists()


def test_replace_gives_up_eventually(tmp_path, monkeypatch):
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_bytes(b"x")
    fake, _ = _busy_then_ok(10**6)
    monkeypatch.setattr(type(src), "replace", lambda self, target: fake())
    with pytest.raises(PermissionError):
        fsutil.replace_retry(src, dst, delays=[0.0, 0.0])


def test_unlink_retries(tmp_path, monkeypatch):
    p = tmp_path / "old.zip"
    p.write_bytes(b"x")
    fake, calls = _busy_then_ok(2)
    real_unlink = type(p).unlink

    def flaky(self, missing_ok=False):
        fake()
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(type(p), "unlink", flaky)
    fsutil.unlink_retry(p, delays=[0.0, 0.0, 0.0])
    assert calls["n"] == 3 and not p.exists()
