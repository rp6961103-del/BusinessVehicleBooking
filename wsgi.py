from app import app
from tracking_routes import tracking_bp

if tracking_bp.name not in app.blueprints:
    app.register_blueprint(tracking_bp)

if not any(
    rule.endpoint == "live_tracking.booking_tracking_shortcut"
    and rule.rule == "/live-tracking/booking/<int:booking_id>"
    for rule in app.url_map.iter_rules()
):
    raise RuntimeError("The live tracking booking route is not registered.")

__all__ = ["app"]