"""Silence a harmless Windows-only traceback in the Streamlit console.

When a browser tab closes or reloads, Windows resets the socket and Python's Proactor event loop prints
"ConnectionResetError: [WinError 10054] An existing connection was forcibly closed by the remote host"
from _call_connection_lost. Nothing is wrong (the other side had already gone), but it looks like a crash.

This is Python 3.12's own _call_connection_lost with the socket shutdown wrapped, so an already-reset
connection is ignored and the socket is still closed and released as usual. No effect on other platforms.
"""
import socket
import sys


def install() -> None:
    if sys.platform != "win32":
        return
    from asyncio import proactor_events

    transport = proactor_events._ProactorBasePipeTransport
    if getattr(transport._call_connection_lost, "_quiet", False):  # Streamlit re-runs the script; patch once
        return

    def _call_connection_lost(self, exc):
        if self._called_connection_lost:
            return
        try:
            self._protocol.connection_lost(exc)
        finally:
            try:
                if hasattr(self._sock, "shutdown") and self._sock.fileno() != -1:
                    self._sock.shutdown(socket.SHUT_RDWR)
            except (ConnectionResetError, OSError):
                pass  # the browser already closed it: nothing left to shut down
            self._sock.close()
            self._sock = None
            server = self._server
            if server is not None:
                server._detach()
                self._server = None
            self._called_connection_lost = True

    _call_connection_lost._quiet = True
    transport._call_connection_lost = _call_connection_lost
