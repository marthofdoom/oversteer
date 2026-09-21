"""The uinput force-feedback request ioctls, done by hand.

python-evdev's UInput.begin_upload()/begin_erase() set a non-existent
`effect_id` attribute instead of the struct's `request_id`, so the kernel
rejects the request. These helpers take the request id from the EV_UINPUT
event and drive the ioctls with the right struct.
"""

import ctypes
import fcntl

from evdev import ff

_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord('U') << 8) | nr


UI_BEGIN_FF_UPLOAD = _ioc(_IOC_READ | _IOC_WRITE, 200, ctypes.sizeof(ff.UInputUpload))
UI_END_FF_UPLOAD = _ioc(_IOC_WRITE, 201, ctypes.sizeof(ff.UInputUpload))
UI_BEGIN_FF_ERASE = _ioc(_IOC_READ | _IOC_WRITE, 202, ctypes.sizeof(ff.UInputErase))
UI_END_FF_ERASE = _ioc(_IOC_WRITE, 203, ctypes.sizeof(ff.UInputErase))


def begin_upload(fd, request_id):
    upload = ff.UInputUpload()
    upload.request_id = request_id
    fcntl.ioctl(fd, UI_BEGIN_FF_UPLOAD, upload)
    return upload


def end_upload(fd, upload):
    fcntl.ioctl(fd, UI_END_FF_UPLOAD, upload)


def begin_erase(fd, request_id):
    erase = ff.UInputErase()
    erase.request_id = request_id
    fcntl.ioctl(fd, UI_BEGIN_FF_ERASE, erase)
    return erase


def end_erase(fd, erase):
    fcntl.ioctl(fd, UI_END_FF_ERASE, erase)


# evdev side (the real wheel): python-evdev's upload_effect() holds the GIL
# during an ioctl that can block, which would stall every other proxy thread.
# fcntl.ioctl releases it.
EVIOCSFF = (_IOC_WRITE << 30) | (ctypes.sizeof(ff.Effect) << 16) | (ord('E') << 8) | 0x80
EVIOCRMFF = (_IOC_WRITE << 30) | (4 << 16) | (ord('E') << 8) | 0x81


def upload_effect(fd, effect):
    """Upload (id == -1) or update an effect on an evdev device; returns its id."""
    fcntl.ioctl(fd, EVIOCSFF, effect)
    return effect.id


def erase_effect(fd, effect_id):
    fcntl.ioctl(fd, EVIOCRMFF, effect_id)
