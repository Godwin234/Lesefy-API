from app import create_app
from app.socket import socketio

app = create_app()

if __name__ == "__main__":
    # Use socketio.run() instead of app.run() so that WebSocket upgrades
    # are handled correctly.  In production, run with a gunicorn + eventlet
    # or gevent worker instead.
    socketio.run(app, debug=True, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
