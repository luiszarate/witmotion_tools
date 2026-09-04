# Tests

    python3 -m unittest discover -s tests -t .

With coverage (needs `pip install coverage`):

    python3 -m coverage run --source=wtvb01 -m unittest discover -s tests -t .
    python3 -m coverage report -m

Nothing here needs the sensor plugged in: `tests/fakes.py` replays captured
frames through a stand-in for `serial.Serial`.
