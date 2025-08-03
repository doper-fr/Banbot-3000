from flask import Flask
from threading import Thread
import os
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create Flask app for keep-alive
app = Flask(__name__)

@app.route('/')
def home():
    return "BanBot 3000 is alive and running!", 200

@app.route('/ping')
def ping():
    return "pong", 200

@app.route('/status')
def status():
    return {
        "status": "online",
        "service": "BanBot 3000",
        "message": "Discord moderation bot is running"
    }, 200

def run_flask():
    """Run the Flask web server"""
    try:
        app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
    except Exception as e:
        logger.error(f"Flask server error: {e}")

def keep_alive():
    """Start the Flask server in a separate thread"""
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()
    logger.info("Flask keep-alive server started on port 5000")

if __name__ == "__main__":
    # Start the keep-alive server
    keep_alive()
    
    # Import and run the Discord bot
    try:
        from bot import run_bot
        logger.info("Starting BanBot 3000...")
        run_bot()
    except ImportError as e:
        logger.error(f"Failed to import bot module: {e}")
    except Exception as e:
        logger.error(f"Error running bot: {e}")

