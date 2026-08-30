from datetime import UTC, datetime

from scopepull.manifest import Manifest


def make(tmp_path):
    return Manifest(tmp_path / "m.db")


def test_roundtrip(tmp_path):
    with make(tmp_path) as m:
        assert not m.is_local("obs/1")
        m.record(
            obs_id="obs/1",
            target="NGC 7635",
            started_at=datetime(2026, 8, 20, tzinfo=UTC),
            frame_count=1200,
            format="fits",
            dir="2026-08-20/ngc-7635__abcd1234",
            files=[("frames/f1.fits", "a" * 64, 100), ("SHA256SUMS", "b" * 64, 50)],
        )
        assert m.is_local("obs/1")
        assert m.local_ids() == {"obs/1"}
        n, total = m.totals()
        assert (n, total) == (1, 150)


def test_record_is_idempotent(tmp_path):
    with make(tmp_path) as m:
        for _ in range(2):
            m.record(
                obs_id="obs/1",
                target="t",
                started_at=None,
                frame_count=1,
                format="fits",
                dir="d",
                files=[("f", "c" * 64, 10)],
            )
        n, total = m.totals()
        assert (n, total) == (1, 10)


def test_forget_cascades(tmp_path):
    with make(tmp_path) as m:
        m.record(
            obs_id="obs/1",
            target="t",
            started_at=None,
            frame_count=1,
            format="fits",
            dir="d",
            files=[("f", "c" * 64, 10)],
        )
        m.forget("obs/1")
        assert not m.is_local("obs/1")
        assert m.totals() == (0, 0)


def test_persists_across_connections(tmp_path):
    with make(tmp_path) as m:
        m.record(
            obs_id="obs/1",
            target="t",
            started_at=None,
            frame_count=1,
            format="fits",
            dir="d",
            files=[],
        )
    with make(tmp_path) as m2:
        assert m2.is_local("obs/1")
