"""
Standalone Application Runner with Live Vehicle Location Tracking.
Safely integrates the Live Tracking blueprint with the existing Flask app
WITHOUT MODIFYING ANY EXISTING FILES.
"""

import os
import sys
import logging

# Ensure current directory is in sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from dotenv import load_dotenv

load_dotenv()

# Step 1: Ensure tracking tables exist in MySQL
try:
    from setup_tracking_db import create_tracking_tables

    create_tracking_tables()
except Exception as exc:
    print(f"⚠️ Warning: Could not auto-initialize tracking tables: {exc}")

# Step 2: Import existing Flask application from app.py
from app import app

# Step 3: Register the Live Tracking Blueprint if not already registered
from tracking_routes import tracking_bp

if "live_tracking" not in app.blueprints:
    app.register_blueprint(tracking_bp)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug_mode = app.config.get("DEBUG", False)

    use_ssl = "--ssl" in sys.argv or os.getenv("SSL", "").lower() in ("1", "true")
    ssl_context = None

    if use_ssl:
        cert_file = os.path.join(BASE_DIR, "cert.pem")
        key_file = os.path.join(BASE_DIR, "key.pem")
        if os.path.exists(cert_file) and os.path.exists(key_file):
            ssl_context = (cert_file, key_file)
            protocol = "https"
        else:
            ssl_context = "adhoc"
            protocol = "https"
    else:
        protocol = "http"

    print("\n" + "=" * 70)
    print("      BUSINESS VEHICLE BOOKING + REAL-TIME GPS TRACKING")
    print("=" * 70)
    print(f" Main Website:             {protocol}://localhost:{port}/")
    print(f" Live Tracking Hub:        {protocol}://localhost:{port}/live-tracking/")
    print(f" Owner GPS Broadcaster:    {protocol}://localhost:{port}/live-tracking/owner")
    print(f" Customer Live Tracker:    {protocol}://localhost:{port}/live-tracking/customer")
    print("=" * 70)
    print(" Mobile & Two-Device GPS Testing Guide:")
    print("   * Localhost Testing: Open two browser windows on desktop")
    print("     (Owner in Window 1 broadcasts, Customer in Window 2 tracks)")
    print("   * Mobile Device (LAN): Mobile browsers require a Secure Context for GPS.")
    print("     Option A: In mobile Chrome, navigate to:")
    print(f"       chrome://flags/#unsafely-treat-insecure-origin-as-secure")
    print(f"       and add http://<YOUR_PC_IP>:{port} then relaunch.")
    print("     Option B: Use a secure tunnel (e.g. 'npx localtunnel --port 5000'")
    print("       or 'ngrok http 5000') for instant HTTPS on any mobile device.")
    print("=" * 70 + "\n")

    app.run(
        host="0.0.0.0",
        port=port,
        debug=debug_mode,
        use_reloader=False,
        threaded=True,
        ssl_context=ssl_context,
    )
