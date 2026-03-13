"""
Shared Flask-SocketIO instance.

Import `socketio` from here wherever you need to emit events or register
handlers.  The instance is wired to the Flask app inside create_app().
"""

from flask_socketio import SocketIO

socketio = SocketIO()
