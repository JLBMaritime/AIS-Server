from ais_server.nmea import (canonicalise, extract_mmsi_and_type, parse,
                             validate_checksum)


GOOD = "!AIVDM,1,1,,A,13aEOK?P00PD2wVMdLDRhgvL289?,0*26"


def test_checksum_valid():
    assert validate_checksum(GOOD)


def test_checksum_invalid():
    assert not validate_checksum(GOOD[:-2] + "00")


def test_parse_valid():
    p = parse(GOOD)
    assert p is not None
    assert p.sentence_type == "!AIVDM"
    assert p.checksum_ok
    assert p.channel == "A"
    assert p.payload == "13aEOK?P00PD2wVMdLDRhgvL289?"


def test_canonicalise_is_stable():
    assert canonicalise(GOOD + "\r\n") == GOOD


def test_mmsi_extraction():
    mmsi, mtype = extract_mmsi_and_type("13aEOK?P00PD2wVMdLDRhgvL289?")
    assert mtype == 1, "should be position report type 1"
    assert mmsi and mmsi > 0
