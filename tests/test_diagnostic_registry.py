from concurrent.futures import ThreadPoolExecutor

from localplane.agent.diagnostic_registry import DiagnosticRegistry


def test_ticket_lifecycle_and_evicted_replay():
    registry = DiagnosticRegistry()
    first = registry.issue()
    assert first is not None and len(first) == 32
    assert registry.status(first) == "pending"
    assert registry.issue() is None
    assert registry.begin(first)
    assert not registry.begin(first)
    registry.complete(first, False)
    registry.complete(first, 1)
    assert registry.status(first) == "pending"
    registry.complete(first, True)
    assert registry.status(first) == "completed"
    assert not registry.begin(first)
    for _ in range(33):
        ticket = registry.issue()
        assert ticket and registry.begin(ticket)
        registry.complete(ticket, True)
    assert registry.status(first) == "unknown"
    assert not registry.begin(first)
    assert not registry.begin("a" * 32)


def test_many_fresh_tickets_do_not_saturate():
    registry = DiagnosticRegistry()
    seen = set()
    for _ in range(12000):
        ticket = registry.issue()
        assert ticket is not None and ticket not in seen
        seen.add(ticket)
        assert registry.begin(ticket)
        registry.complete(ticket, True)
    assert len(registry._entries) <= 32


def test_competing_begins_only_one_executes():
    registry = DiagnosticRegistry()
    ticket = registry.issue()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(registry.begin, [ticket] * 64))
    assert results.count(True) == 1
    assert registry.issue() is None
    assert registry.status(ticket) == "pending"
