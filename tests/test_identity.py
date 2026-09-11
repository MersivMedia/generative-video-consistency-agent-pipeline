"""Tests for viewer identity and screening persistence.

These target the three constraints that a public screening cannot ship without,
and specifically the bug that motivated them: identity was the memory address
of the websocket object, which CPython recycles.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

import identity  # noqa: E402
import ladder  # noqa: E402


def test_id_of_object_is_not_an_identity():
    """The bug this replaced: id() collides catastrophically."""
    class WS:
        pass
    seen = []
    for _ in range(2000):
        w = WS()
        seen.append(id(w))
    distinct = len(set(seen))
    assert distinct < 100, f"expected heavy reuse, saw {distinct} distinct"
    print(f"  PASS id() gave {distinct} distinct values for 2000 objects "
          f"({2000 - distinct} collisions) — unusable as identity")


def test_tokens_are_unique_and_verify():
    toks = [identity.mint() for _ in range(500)]
    assert len(set(toks)) == 500, "tokens collided"
    ids = [identity.verify(t) for t in toks]
    assert all(ids) and len(set(ids)) == 500
    print(f"  PASS 500 tokens, all unique, all verify")


def test_forged_tokens_are_rejected():
    good = identity.mint()
    vid, _, sig = good.rpartition(".")
    cases = {
        "tampered id": f"{vid}x.{sig}",
        "tampered sig": f"{vid}.{'0' * len(sig)}",
        "no signature": vid,
        "empty": "",
        "none": None,
    }
    for label, tok in cases.items():
        assert identity.verify(tok) is None, f"{label} was accepted!"
    assert identity.verify(good) == vid
    print(f"  PASS {len(cases)} forgery attempts rejected, genuine accepted")


def test_one_viewer_one_ballot_across_reconnects():
    """A refresh must replace a vote, not add one."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
    from live import Screening

    s = Screening()
    s.open_vote("b1", [{"id": "A", "label": "a"}, {"id": "B", "label": "b"}], 30)
    tok = identity.mint()
    me = identity.verify(tok)

    s.cast(me, "A")                 # first visit
    s.cast(me, "B")                 # after a refresh, same token
    s.cast(me, "B")                 # second tab, same token
    assert sum(s.tally().values()) == 1, s.tally()
    assert s.tally()["B"] == 1, s.tally()

    other = identity.verify(identity.mint())
    s.cast(other, "A")
    assert sum(s.tally().values()) == 2, s.tally()
    print(f"  PASS one token = one ballot across 3 casts; tally {s.tally()}")


def test_journal_survives_truncated_final_line():
    """A hard kill mid-write must not invalidate the log."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "j.jsonl"
        j = identity.Journal(p)
        j.append("start", at=1.0, title="X")
        j.append("decision", record={"beat": "b1", "winner": "A"})
        j.close()
        with open(p, "a") as fh:          # simulate a partial write
            fh.write('{"kind": "deci')
        recs = list(identity.Journal(p).replay())
        assert len(recs) == 2, recs
        print(f"  PASS replayed {len(recs)} records, ignored the torn line")


def test_journal_replay_rebuilds_canon():
    from live import Screening
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        j = identity.Journal(root / "j.jsonl")
        j.append("start", at=100.0, title="The Signal")
        j.append("decision", record={"beat": "b1", "winner": "A",
                                     "tally": {"A": 3, "B": 2}, "voters": 5,
                                     "at": 8.6})
        j.append("decision", record={"beat": "b2", "winner": "B",
                                     "tally": {"A": 1, "B": 4}, "voters": 5,
                                     "at": 34.0})
        s = Screening()
        counts = identity.restore(s, identity.Journal(root / "j.jsonl"), root)
        assert s.title == "The Signal" and s.started_at == 100.0
        assert counts["decisions"] == 2 and len(s.canon) == 2
        assert s.canon[1]["winner"] == "B"
        print(f"  PASS restored title, clock, and {len(s.canon)} decisions")


def test_missing_segment_files_are_skipped():
    """A journal entry whose media was cleaned up must not enter the playlist."""
    from live import Screening
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "mid").mkdir()
        # one real file, one referenced but absent
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                        "-i", "color=c=black:s=64x64:d=1", "-t", "1",
                        str(root / "mid" / "seg_0000.ts")],
                       check=True, capture_output=True)
        j = identity.Journal(root / "j.jsonl")
        j.append("segment", index=0, seconds=1.0, beat="b1")
        j.append("segment", index=1, seconds=1.0, beat="b1")
        s = Screening()
        counts = identity.restore(s, identity.Journal(root / "j.jsonl"), root)
        assert counts["segments"] == 1 and counts["missing"] == 1, counts
        print(f"  PASS kept {counts['segments']} present, "
              f"skipped {counts['missing']} missing")


def test_ladder_rungs_share_gop_and_declare_peak_bandwidth():
    """Misaligned keyframes make quality switches glitch or stall."""
    assert len({ladder.GOP}) == 1
    widths = [r.width for r in ladder.LADDER]
    assert widths == sorted(widths), "ladder must ascend"
    for r in ladder.LADDER:
        v = int(r.v_bitrate.rstrip("k")) * 1000
        assert r.bandwidth > v, "BANDWIDTH must exceed video bitrate"
    with tempfile.TemporaryDirectory() as d:
        m = ladder.write_master(Path(d))
        body = m.read_text()
        assert body.count("#EXT-X-STREAM-INF") == len(ladder.LADDER)
        first = body.split("BANDWIDTH=")[1].split(",")[0]
        assert int(first) == ladder.LADDER[0].bandwidth, "lowest rung first"
    print(f"  PASS {len(ladder.LADDER)} rungs, GOP {ladder.GOP}, "
          f"lowest-first, peak bandwidth declared")


def test_token_limiter_caps_minting_per_source():
    """Clearing storage in a loop must stop being free."""
    lim = identity.TokenLimiter(per_ip=3)
    toks, fresh_flags = [], []
    for _ in range(10):
        t, fresh = lim.issue("203.0.113.9")
        toks.append(t)
        fresh_flags.append(fresh)
    assert len(set(toks)) == 3, f"expected 3 identities, got {len(set(toks))}"
    assert fresh_flags[:3] == [True, True, True]
    assert not any(fresh_flags[3:]), "cap did not engage"

    # a different source is unaffected
    other, fresh = lim.issue("198.51.100.4")
    assert fresh and other not in toks
    s = lim.stats()
    assert s["sources"] == 2 and s["at_cap"] == 1, s
    print(f"  PASS 10 requests from one IP yielded 3 identities; "
          f"stats {s}")


def test_limiter_recycled_tokens_still_verify():
    """A recycled token must remain a valid identity, not a broken one."""
    lim = identity.TokenLimiter(per_ip=1)
    first, _ = lim.issue("203.0.113.9")
    again, fresh = lim.issue("203.0.113.9")
    assert again == first and not fresh
    assert identity.verify(again) is not None
    print("  PASS recycled token is the same valid identity")


def test_playlists_use_relative_urls():
    """Absolute URLs break prefix mounting and CDN fronting."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        master = ladder.write_master(root).read_text()
        assert "/stream/" not in master, master
        assert "low.m3u8" in master

        class Seg:
            seconds = 5.2
            kind = "shot"
        variant = ladder.write_variant(
            root, ladder.LADDER[0], [Seg(), Seg()], False).read_text()
        assert "\n/segments/" not in variant, variant
        assert "../segments/low/seg_0000.ts" in variant
    print("  PASS master and variant playlists are prefix-independent")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"running {len(tests)} identity/persistence/ladder tests\n")
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as exc:
            failed += 1
            print(f"  FAIL {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
