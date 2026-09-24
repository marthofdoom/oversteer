"""Desktop-wide keyboard shortcuts through the XDG GlobalShortcuts portal.

The desktop (KWin on Plasma, Mutter on GNOME) owns the keyboard: it matches
the key combinations and tells us only when one of ours fires, so nothing
here reads keys, and the shortcuts work while a full-screen game has focus.
The user picks the keys in the desktop's own dialog (and later in its
shortcut settings); we only declare what can be bound.

Everything runs on the GLib main loop; callbacks arrive there too.
"""

import logging
import os
import secrets

from gi.repository import Gio, GLib

BUS_NAME = 'org.freedesktop.portal.Desktop'
OBJECT_PATH = '/org/freedesktop/portal/desktop'
INTERFACE = 'org.freedesktop.portal.GlobalShortcuts'
REQUEST_INTERFACE = 'org.freedesktop.portal.Request'


def in_sandbox():
    return os.path.exists('/.flatpak-info')


class GlobalShortcuts:

    def __init__(self, app_id, on_activated, on_bound=None, on_failed=None):
        """`on_activated(shortcut_id)` runs when a bound key is pressed;
        `on_bound({shortcut_id: trigger description})` whenever we learn
        which keys the desktop has assigned; `on_failed(message)` when
        the portal is missing or turns a request down."""
        self.app_id = app_id
        self.on_activated = on_activated
        self.on_bound = on_bound
        self.on_failed = on_failed
        self.conn = None
        self.session = None
        self.version = 0
        self.triggers = {}
        self._pending = []           # calls waiting for the session

    def available(self):
        return self.conn is not None and self.version > 0

    def start(self):
        """Connect and open a session, without blocking the main loop: a
        portal that is slow to start must not freeze the window. False
        only when there is no session bus at all."""
        try:
            # A private connection: a host app must register its id before
            # any other portal call on the connection, and GTK may already
            # have talked to the portal on the shared one.
            address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
            self.conn = Gio.DBusConnection.new_for_address_sync(
                address,
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None, None)
        except GLib.Error as e:
            logging.info("global shortcuts: no session bus: %s", e.message)
            self.conn = None
            return False
        if in_sandbox():
            self._get_version()
        else:
            # Tell the portal who we are (a Flatpak is identified by its
            # sandbox). Portals before 1.19 lack this; the desktop then
            # names the shortcuts after the process instead.
            def registered(conn, result):
                try:
                    conn.call_finish(result)
                except GLib.Error as e:
                    logging.debug("global shortcuts: registry: %s", e.message)
                self._get_version()
            self.conn.call(BUS_NAME, OBJECT_PATH, 'org.freedesktop.host.portal.Registry', 'Register',
                           GLib.Variant('(sa{sv})', (self.app_id, {})), None,
                           Gio.DBusCallFlags.NONE, -1, None, registered)
        return True

    def _get_version(self):
        def got(conn, result):
            try:
                self.version = conn.call_finish(result).unpack()[0]
            except GLib.Error as e:
                logging.info("global shortcuts: portal unavailable: %s", e.message)
                self._fail(None)
                return
            self.conn.signal_subscribe(BUS_NAME, INTERFACE, 'Activated', OBJECT_PATH, None,
                                       Gio.DBusSignalFlags.NONE, self._activated)
            self.conn.signal_subscribe(BUS_NAME, INTERFACE, 'ShortcutsChanged', OBJECT_PATH, None,
                                       Gio.DBusSignalFlags.NONE, self._changed)
            token = self._token()
            self._request('CreateSession', GLib.Variant('(a{sv})', ({
                'handle_token': GLib.Variant('s', token),
                'session_handle_token': GLib.Variant('s', self._token()),
            },)), token, self._session_created)
        self.conn.call(BUS_NAME, OBJECT_PATH, 'org.freedesktop.DBus.Properties', 'Get',
                       GLib.Variant('(ss)', (INTERFACE, 'version')), None,
                       Gio.DBusCallFlags.NONE, -1, None, got)

    def list(self, done):
        """What the desktop already has for us: `done({id: trigger
        description})`. Quiet, unlike bind(): Plasma opens its shortcut
        settings on every bind, so a bind belongs to a user's click."""
        if self.session is None:
            self._pending.append(lambda: self.list(done))
            return
        token = self._token()

        def listed(results):
            self._record(results.get('shortcuts', []))
            done(dict(self.triggers))
        self._request('ListShortcuts', GLib.Variant('(oa{sv})', (
            self.session, {'handle_token': GLib.Variant('s', token)})), token, listed)

    def bind(self, shortcuts):
        """Declare our shortcuts: [(id, description), ...]. The desktop
        shows its dialog (Plasma: its shortcut settings), keeps the keys
        the user chose, forgets any id left out, and answers with the
        current assignments."""
        if self.session is None:
            self._pending.append(lambda: self.bind(shortcuts))
            return
        token = self._token()
        entries = [(sid, {'description': GLib.Variant('s', text)}) for sid, text in shortcuts]
        self._request('BindShortcuts', GLib.Variant('(oa(sa{sv})sa{sv})', (
            self.session, entries, '', {'handle_token': GLib.Variant('s', token)})), token, self._bound)

    def configure(self, on_unsupported):
        """Open the desktop's editor for our shortcuts (portal version 2).
        `on_unsupported()` runs instead when there is none: an old portal,
        or a new portal whose desktop backend lacks the editor."""
        if self.session is None or self.version < 2:
            on_unsupported()
            return

        def done(conn, result):
            try:
                conn.call_finish(result)
            except GLib.Error as e:
                logging.debug("global shortcuts: configure: %s", e.message)
                on_unsupported()
        self.conn.call(BUS_NAME, OBJECT_PATH, INTERFACE, 'ConfigureShortcuts',
                       GLib.Variant('(osa{sv})', (self.session, '', {})), None,
                       Gio.DBusCallFlags.NONE, -1, None, done)

    # -- plumbing --

    @staticmethod
    def _token():
        return 'oversteer_' + secrets.token_hex(6)

    def _request(self, method, params, token, done):
        """Call a portal method that answers through a Request object:
        subscribe to its Response first, so a quick answer isn't missed."""
        sender = self.conn.get_unique_name()[1:].replace('.', '_')
        path = '{}/request/{}/{}'.format(OBJECT_PATH, sender, token)
        sub = {}

        def response(conn, sender_name, obj, iface, signal, args):
            conn.signal_unsubscribe(sub['id'])
            code, results = args.unpack()
            if code != 0:
                logging.info("global shortcuts: %s answered %d", method, code)
                self._fail(method)
                return
            done(results)
        sub['id'] = self.conn.signal_subscribe(BUS_NAME, REQUEST_INTERFACE, 'Response', path, None,
                                               Gio.DBusSignalFlags.NONE, response)

        def called(conn, result):
            try:
                conn.call_finish(result)
            except GLib.Error as e:
                conn.signal_unsubscribe(sub['id'])
                logging.info("global shortcuts: %s failed: %s", method, e.message)
                self._fail(method)
        self.conn.call(BUS_NAME, OBJECT_PATH, INTERFACE, method, params, None,
                       Gio.DBusCallFlags.NONE, -1, None, called)

    def _fail(self, method):
        """No portal (method None), or a request turned down or failed."""
        if method is None:
            self.conn = None
        if self.on_failed is not None:
            self.on_failed(method)

    def _session_created(self, results):
        self.session = results.get('session_handle')
        pending, self._pending = self._pending, []
        for call in pending:
            call()

    def _bound(self, results):
        self._record(results.get('shortcuts', []))

    def _record(self, shortcuts):
        self.triggers = {sid: props.get('trigger_description', '') for sid, props in shortcuts}
        if self.on_bound is not None:
            self.on_bound(dict(self.triggers))

    def _changed(self, conn, sender, obj, iface, signal, args):
        session, shortcuts = args.unpack()
        if session == self.session:
            self._record(shortcuts)

    def _activated(self, conn, sender, obj, iface, signal, args):
        session, shortcut_id = args.unpack()[:2]
        if session == self.session:
            self.on_activated(shortcut_id)
