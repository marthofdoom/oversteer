"""Latency of the userspace proxy hop: kernel timestamp of a source event vs
kernel timestamp of the forwarded event on the virtual device. Run as a
normal user with /dev/uinput access: python3 tests/bench_latency.py"""
import os, select, statistics, sys, threading, time
from evdev import InputDevice, UInput, AbsInfo, ecodes as e, list_devices
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from oversteer.proxy import ProxySpec, ProxyDevice, ProxyState

def find(name, timeout=3):
    t0 = time.time()
    while time.time() - t0 < timeout:
        for p in list_devices():
            d = InputDevice(p)
            if d.name == name:
                return d
            d.close()
        time.sleep(0.05)
    raise SystemExit("virtual device missing")

def measure(label, load_threads):
    # Use kernel timestamps on both sides: open the source node too.
    src = UInput({e.EV_ABS: [(e.ABS_X, AbsInfo(32768, 0, 65535, 0, 0, 0))]},
                 name='Bench Source', vendor=0x1234, product=0xb0b0, version=1, phys='usb-bench/input0')
    time.sleep(0.2)
    src_reader = InputDevice(src.device.path)
    spec = ProxySpec.from_dict({'id': 'bench', 'name': 'bench', 'identity': {'name': 'Bench Proxy'},
                                'sources': {'w': {'match': {'vendor': '1234', 'product': 'b0b0'}, 'grab': False}},
                                'passthrough': 'w'})
    proxy = ProxyDevice(spec); proxy.start()
    t0 = time.time()
    while proxy.state != ProxyState.RUNNING and time.time() - t0 < 3:
        time.sleep(0.02)
    virt = find('Bench Proxy')
    import subprocess
    burners = [subprocess.Popen([sys.executable, '-c', 'while True: pass']) for _ in range(load_threads)]
    t_src, t_virt = {}, {}
    n, period = 3000, 1 / 500
    next_t = time.monotonic()
    def drain():
        for dev, table in ((src_reader, t_src), (virt, t_virt)):
            while True:
                r, _, _ = select.select([dev.fd], [], [], 0)
                if not r:
                    break
                for ev in dev.read():
                    if ev.type == e.EV_ABS and ev.code == e.ABS_X:
                        table[ev.value] = ev.timestamp()
    for i in range(n):
        src.write(e.EV_ABS, e.ABS_X, 1000 + i); src.syn()
        drain()
        next_t += period
        d = next_t - time.monotonic()
        if d > 0:
            time.sleep(d)
    time.sleep(0.3); drain()
    for b in burners:
        b.kill()
    proxy.stop(); src_reader.close(); src.close()
    lat = [(t_virt[v] - t_src[v]) * 1e6 for v in t_src if v in t_virt]
    lat.sort()
    q = lambda p: lat[min(len(lat) - 1, int(p * len(lat)))]
    print("{:<28} n={:5d}  p50 {:6.0f} µs  p90 {:6.0f} µs  p99 {:6.0f} µs  max {:6.0f} µs  lost {}".format(
        label, len(lat), statistics.median(lat), q(0.90), q(0.99), lat[-1], n - len(lat)))

def measure_ff(label):
    """Round trip of an effect upload: game -> virtual device -> proxy -> real wheel."""
    from oversteer.proxy import uinput_ff
    from evdev import ff
    wheel = UInput({e.EV_ABS: [(e.ABS_X, AbsInfo(32768, 0, 65535, 0, 0, 0))], e.EV_KEY: [e.BTN_TRIGGER],
                    e.EV_FF: [e.FF_CONSTANT, e.FF_GAIN]}, name='Bench Wheel', vendor=0x1234, product=0xb0b1,
                   version=1, max_effects=16, phys='usb-bench/input1')
    time.sleep(0.2)
    spec = ProxySpec.from_dict({'id': 'benchff', 'name': 'benchff', 'identity': {'name': 'Bench FF Proxy'},
                                'sources': {'w': {'match': {'vendor': '1234', 'product': 'b0b1'}}},
                                'passthrough': 'w', 'ff': {'source': 'w'}})
    proxy = ProxyDevice(spec); proxy.start()
    t0 = time.time()
    while proxy.state != ProxyState.RUNNING and time.time() - t0 < 3:
        time.sleep(0.02)
    virt = find('Bench FF Proxy')
    stop = threading.Event()
    def service():
        while not stop.is_set():
            r, _, _ = select.select([wheel.fd], [], [], 0.02)
            if not r:
                continue
            for ev in wheel.read():
                if ev.type == e.EV_UINPUT and ev.code == e.UI_FF_UPLOAD:
                    up = uinput_ff.begin_upload(wheel.fd, ev.value); up.retval = 0; uinput_ff.end_upload(wheel.fd, up)
                elif ev.type == e.EV_UINPUT and ev.code == e.UI_FF_ERASE:
                    er = uinput_ff.begin_erase(wheel.fd, ev.value); er.retval = 0; uinput_ff.end_erase(wheel.fd, er)
    threading.Thread(target=service, daemon=True).start()
    effect = ff.Effect(e.FF_CONSTANT, -1, 0x4000, ff.Trigger(0, 0), ff.Replay(0, 0), ff.EffectType(ff_constant_effect=ff.Constant(level=1000)))
    eid = uinput_ff.upload_effect(virt.fd, effect)
    lat = []
    for i in range(1000):
        effect.id = eid; effect.u.ff_constant_effect.level = 1000 + i
        t = time.perf_counter(); uinput_ff.upload_effect(virt.fd, effect); lat.append((time.perf_counter() - t) * 1e6)
        time.sleep(0.002)
    lat.sort(); q = lambda p: lat[min(len(lat) - 1, int(p * len(lat)))]
    print("{:<28} n={:5d}  p50 {:6.0f} µs  p90 {:6.0f} µs  p99 {:6.0f} µs  max {:6.0f} µs  (includes the fake wheel's own servicing thread)".format(
        label, len(lat), statistics.median(lat), q(0.90), q(0.99), lat[-1]))
    stop.set(); proxy.stop(); wheel.close()


if __name__ == '__main__':
    measure("input hop, idle", 0)
    measure("input hop, 8 busy processes", 8)
    measure_ff("FFB update round trip, idle")
