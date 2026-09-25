"""Cost per packet of the live path: EA SPORTS WRC stages for ten minutes
through Telemetry.handle with a null LED object, the shift learner and
the run tracker, writing through the drive-log thread as the app does.
The design's budget is a mean of 0.3 ms and a 99th percentile of 2 ms
per packet (docs/telemetry-coaching.md, section 4). Not part of pytest
(timing depends on the machine): python3 tests/bench_telemetry.py"""
import os
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from oversteer.shift_learner import ShiftLearner  # noqa: E402
from oversteer.telemetry import Telemetry  # noqa: E402
from oversteer.telemetry_capture import NullLeds  # noqa: E402
from tests.sim import stage_packets  # noqa: E402


def measure(packets, threaded=True):
    with tempfile.TemporaryDirectory() as folder:
        learner = ShiftLearner(os.path.join(folder, 'telemetry.db'), threaded=threaded)
        telemetry = Telemetry(NullLeds(), learner=learner, use_learnt=True)
        costs = []
        for t, data in packets:
            start = time.perf_counter()
            telemetry.handle(1000.0 + t, data, ('127.0.0.1', 0))
            costs.append(time.perf_counter() - start)
        learner.close()
        dropped = learner.log.dropped
    costs.sort()
    return statistics.mean(costs) * 1000, costs[int(len(costs) * 0.99)] * 1000, max(costs) * 1000, dropped


def main():
    packets = stage_packets(10.0)
    mean, p99, worst, dropped = measure(packets)
    print("%d packets: mean %.3f ms, p99 %.3f ms, worst %.1f ms, %d events dropped" % (
        len(packets), mean, p99, worst, dropped))


if __name__ == '__main__':
    main()
