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

    def __init__(self, app_id, on_activated, on_bound=None):
        """`on_activated(shortcut_id)` runs when a bound key is pressed;
        `on_bound({shortcut_id: trigger description})` whenever we learn
        which keys the desktop has assigned."""
        self.app_id = app_id
        self.on_activated = on_activated
        self.on_bound = on_bound
        self.conn = None
        self.session = None
        self.version = 0
        self.triggers = {}
        self._pending = []           # calls waiting for the session

    def available(self):
        return self.conn is not None and self.version > 0

    def start(self):
        """Connect and open a session. False when there is no portal or it
        has no GlobalShortcuts interface (older desktops)."""
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
        if not in_sandbox():
            # Tell the portal who we are (a Flatpak is identified by its
            # sandbox). Portals before 1.19 lack this; the desktop then
            # names the shortcuts after the process instead.
            try:
                self.conn.call_sync(BUS_NAME, OBJECT_PATH, 'org.freedesktop.host.portal.Registry', 'Register',
                                    GLib.Variant('(sa{sv})', (self.app_id, {})), None,
                                    Gio.DBusCallFlags.NONE, 2000, None)
            except GLib.Error as e:
                logging.debug("global shortcuts: registry: %s", e.message)
        try:
            reply = self.conn.call_sync(BUS_NAME, OBJECT_PATH, 'org.freedesktop.DBus.Properties', 'Get',
                                        GLib.Variant('(ss)', (INTERFACE, 'version')), None,
                                        Gio.DBusCallFlags.NONE, 2000, None)
            self.version = reply.unpack()[0]
        except GLib.Error as e:
            logging.info("global shortcuts: portal unavailable: %s", e.message)
            self.conn = None
            return False
        self.conn.signal_subscribe(BUS_NAME, INTERFACE, 'Activated', OBJECT_PATH, None,
                                   Gio.DBusSignalFlags.NONE, self._activated)
        self.conn.signal_subscribe(BUS_NAME, INTERFACE, 'ShortcutsChanged', OBJECT_PATH, None,
                                   Gio.DBusSignalFlags.NONE, self._changed)
        token = self._token()
        self._request('CreateSession', GLib.Variant('(a{sv})', ({
            'handle_token': GLib.Variant('s', token),
            'session_handle_token': GLib.Variant('s', self._token()),
        },)), token, self._session_created)
        return True

    def bind(self, shortcuts):
        """Declare our shortcuts: [(id, description), ...]. The desktop
        shows its dialog for any it hasn't seen, keeps the keys the user
        chose for the rest, and answers with the current assignments."""
        if self.session is None:
            self._pending.append(lambda: self.bind(shortcuts))
            return
        token = self._token()
        entries = [(sid, {'description': GLib.Variant('s', text)}) for sid, text in shortcuts]
        self._request('BindShortcuts', GLib.Variant('(oa(sa{sv})sa{sv})', (
            self.session, entries, '', {'handle_token': GLib.Variant('s', token)})), token, self._bound)

    def configure(self):
        """Open the desktop's editor for our shortcuts (portal version 2);
        False when the desktop has none, so the caller can explain where
        to find them instead."""
        if self.session is None or self.version < 2:
            return False
        try:
            self.conn.call(BUS_NAME, OBJECT_PATH, INTERFACE, 'ConfigureShortcuts',
                           GLib.Variant('(osa{sv})', (self.session, '', {})), None,
                           Gio.DBusCallFlags.NONE, -1, None, None, None)
        except GLib.Error:
            return False
        return True

    def close(self):
        if self.conn is not None and self.session is not None:
            try:
                self.conn.call_sync(BUS_NAME, self.session, 'org.freedesktop.portal.Session', 'Close',
                                    None, None, Gio.DBusCallFlags.NONE, 1000, None)
            except GLib.Error:
                pass
        self.session = None

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
        self.conn.call(BUS_NAME, OBJECT_PATH, INTERFACE, method, params, None,
                       Gio.DBusCallFlags.NONE, -1, None, called)

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
