from flask_socketio import SocketIO

from app import create_app
from terminal import terminal_bp, init_terminal_socketio

app = create_app()

# Register terminal blueprint
app.register_blueprint(terminal_bp)

# Initialize SocketIO with gevent for WebSocket support
socketio = SocketIO(
    app,
    async_mode="gevent",
    cors_allowed_origins="*",  # Adjust for production if needed
    ping_timeout=60,
    ping_interval=25,
)

# Initialize terminal SocketIO handlers
init_terminal_socketio(socketio)

# For gunicorn with gevent worker
if __name__ == "__main__":
    socketio.run(app, debug=True, port=8005)
