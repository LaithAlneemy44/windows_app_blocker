from playlimiter import enforce


class FakeProc:
    def __init__(self):
        self.killed = False

    def kill(self):
        self.killed = True


def setup(monkeypatch, window_counts):
    """post_close returns the next count from window_counts each call."""
    calls = []
    counts = iter(window_counts)
    monkeypatch.setattr(enforce, "post_close", lambda pid: calls.append(pid) or next(counts, 1))
    proc = FakeProc()
    monkeypatch.setattr(enforce, "still_same_process", lambda pid, exe: None if proc.killed else proc)
    return calls, proc


def test_retries_wm_close_until_a_window_exists_then_stops(monkeypatch):
    calls, proc = setup(monkeypatch, [0, 0, 2])
    closer = enforce.Closer()
    closer.close(1, "game.exe", now=0, grace_s=10)
    closer.step(2)
    closer.step(4)  # window appeared, WM_CLOSE delivered
    closer.step(6)
    closer.step(8)
    assert len(calls) == 3
    assert not proc.killed


def test_kills_only_after_grace(monkeypatch):
    calls, proc = setup(monkeypatch, [3])
    closer = enforce.Closer()
    closer.close(1, "game.exe", now=0, grace_s=60)
    closer.step(59)
    assert not proc.killed
    closer.step(60)
    assert proc.killed and not closer.is_closing(1)
    assert len(calls) == 1  # graceful close was attempted first, exactly once
