from app.limits import DailyQuota, WindowLimiter


def test_window_limiter_blocks_after_the_limit():
    lim = WindowLimiter(limit=2, window_s=60)
    assert [lim.allow("a") for _ in range(3)] == [True, True, False]
    assert lim.allow("b") is True


def test_window_limiter_table_is_hard_bounded():
    lim = WindowLimiter(limit=2, window_s=60, max_keys=100)
    for i in range(1000):
        lim.allow(f"client-{i}")
    assert len(lim) == 100


def test_blocked_clients_do_not_grow_the_table():
    lim = WindowLimiter(limit=1, window_s=60, max_keys=100)
    lim.allow("a")
    for _ in range(50):
        assert lim.allow("a") is False
    assert len(lim) == 1


def test_daily_quota_per_client_and_service():
    q = DailyQuota(total=3, per_client=2, max_keys=100)
    assert [q.reserve("a") for _ in range(3)] == [None, None, "client"]
    assert q.reserve("b") is None
    assert q.reserve("c") == "service"
    q.refund("b")
    assert q.reserve("c") is None


def test_daily_quota_table_is_hard_bounded():
    q = DailyQuota(total=10_000, per_client=1, max_keys=50)
    for i in range(500):
        q.reserve(f"client-{i}")
    assert len(q._by_client) == 50
