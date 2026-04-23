from ais_server.dedup import Deduper


def test_dedup_detects_duplicate_regardless_of_whitespace():
    d = Deduper(ttl_seconds=10)
    s1 = "!AIVDM,1,1,,A,13aEOK?P00PD2wVMdLDRhgvL289?,0*26"
    s2 = s1 + "\r\n"          # same sentence, trailing whitespace
    ok1, ts1 = d.check(s1, arrival_ts=100.0)
    ok2, ts2 = d.check(s2, arrival_ts=105.0)
    assert ok1 is True  and ts1 == 100.0
    assert ok2 is False and ts2 == 100.0, "duplicate must inherit first ts"


def test_dedup_distinguishes_different_sentences():
    d = Deduper()
    a = "!AIVDM,1,1,,A,13aEOK?P00PD2wVMdLDRhgvL289?,0*26"
    b = "!AIVDM,1,1,,A,15M67FC000G?ufbE`C3m8i<04`Ol,0*6A"
    assert d.check(a)[0] is True
    assert d.check(b)[0] is True
    assert d.check(a)[0] is False
