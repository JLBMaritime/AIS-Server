import time

from ais_server.reorder import ReorderBuffer


def test_items_are_held_then_released_in_order():
    rb = ReorderBuffer(hold_ms=100, max_queue=1000)
    rb.push("b", ts=2.0)
    rb.push("a", ts=1.0)
    rb.push("c", ts=3.0)

    assert rb.pop_ready(now=1.0) == []              # nothing ready yet
    ready = rb.pop_ready(now=10.0)
    assert ready == ["a", "b", "c"], "must emerge chronologically sorted"


def test_queue_cap_drops_oldest():
    rb = ReorderBuffer(hold_ms=1000, max_queue=3)
    for i in range(5):
        rb.push(f"x{i}", ts=float(i))
    assert rb.stats()["dropped"] == 2
    assert rb.stats()["queue_size"] == 3
