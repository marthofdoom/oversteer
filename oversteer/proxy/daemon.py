"""Entry point of the oversteer-proxy system service.

Runs the enabled proxies from the system spec directory until stopped.
Deliberately imports nothing from the GUI so a copy of the `oversteer.proxy`
package plus `oversteer.wheel_ids` is all the service needs (the installer
places such a copy in /usr/local/lib/oversteer-proxy).
"""

import logging
import os
import signal
import sys
import time

from .calibration import CALIBRATION_DIR
from .manager import ProxyManager, BUILTIN_DIR, SYSTEM_DIR, STATUS_FILE


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    manager = ProxyManager(dirs=[BUILTIN_DIR, SYSTEM_DIR], status_file=STATUS_FILE)
    try:        # the app hands calibrations over here (calibration.py); write-only for users, files checked by owner
        os.makedirs(CALIBRATION_DIR, exist_ok=True)
        os.chmod(CALIBRATION_DIR, 0o1733)
    except OSError as e:
        logging.warning("calibration directory: %s", e)
    manager.start()
    stop = {'now': False}

    def handler(signum, frame):
        stop['now'] = True
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    from .manager import STATUS_HEARTBEAT
    last = time.time()
    while not stop['now']:
        time.sleep(0.5)
        if time.time() - last >= STATUS_HEARTBEAT:
            manager.write_status()
            last = time.time()
    manager.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
