import os
import re
import secrets
import logging
import hashlib
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, render_template, request, redirect, session, flash, jsonify, url_for
import mysql.connector
from mysql.connector import Error
from mysql.connector.errors import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from email_service import (
    send_booking_notifications,
    send_booking_decision_email,
    send_password_reset_email,
    send_demo_payment_confirmation_email,
    is_mail_configured,
)
from ai_disease_service import is_ai_configured, validate_image_file, analyze_crop_leaf, chat_about_crop

logger = logging.getLogger(__name__)
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
IS_RENDER = bool(os.getenv("RENDER") or os.getenv("RENDER_SERVICE_ID"))
PRODUCTION_ENVIRONMENTS = {"production", "prod"}
IS_PRODUCTION = APP_ENV in PRODUCTION_ENVIRONMENTS or IS_RENDER

logging.basicConfig(
    level=logging.INFO if IS_PRODUCTION else logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def validate_production_configuration():
    if not IS_PRODUCTION:
        return

    missing_variables = []
    if not (os.getenv("SECRET_KEY") or os.getenv("FLASK_SECRET_KEY")):
        missing_variables.append("SECRET_KEY")
    if not (os.getenv("MYSQL_HOST") or os.getenv("DB_HOST")):
        missing_variables.append("MYSQL_HOST")
    if not (os.getenv("MYSQL_PORT") or os.getenv("DB_PORT")):
        missing_variables.append("MYSQL_PORT")
    if not (os.getenv("MYSQL_DATABASE") or os.getenv("DB_NAME") or os.getenv("DB_DATABASE")):
        missing_variables.append("MYSQL_DATABASE")
    if not (os.getenv("MYSQL_USER") or os.getenv("DB_USER")):
        missing_variables.append("MYSQL_USER")
    if not (os.getenv("MYSQL_PASSWORD") or os.getenv("DB_PASSWORD")):
        missing_variables.append("MYSQL_PASSWORD")

    secure_cookie_val = (
        os.getenv("SESSION_COOKIE_SECURE")
        or os.getenv("FLASK_SESSION_COOKIE_SECURE")
        or ""
    ).strip().lower()
    if not secure_cookie_val:
        missing_variables.append("SESSION_COOKIE_SECURE")

    if missing_variables:
        raise RuntimeError(
            "Missing required production environment variable(s): "
            + ", ".join(missing_variables)
        )

    resolved_host = (os.getenv("MYSQL_HOST") or os.getenv("DB_HOST") or "").strip().lower()
    if resolved_host in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError(
            "MYSQL_HOST cannot be localhost or 127.0.0.1 in production. "
            "Configure your external production database host (e.g., Aiven MySQL)."
        )

    if os.getenv("FLASK_DEBUG", "0") == "1":
        raise RuntimeError("FLASK_DEBUG must be 0 in production")

    if secure_cookie_val not in {"1", "true", "yes", "on"}:
        raise RuntimeError("SESSION_COOKIE_SECURE must be enabled in production")


validate_production_configuration()

app = Flask(__name__)

app.secret_key = (
    os.getenv("SECRET_KEY")
    or os.getenv("FLASK_SECRET_KEY")
    or secrets.token_urlsafe(32)
)
app.config.update(
    ENVIRONMENT=APP_ENV,
    DEBUG=os.getenv("FLASK_DEBUG", "0") == "1" and not IS_PRODUCTION,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(
        os.getenv("SESSION_COOKIE_SECURE", os.getenv("FLASK_SESSION_COOKIE_SECURE", "0"))
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    ),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    SESSION_REFRESH_EACH_REQUEST=False,
)

trusted_proxy_hops = int(os.getenv("TRUSTED_PROXY_HOPS", "0"))
if trusted_proxy_hops < 0:
    raise RuntimeError("TRUSTED_PROXY_HOPS must be zero or greater")
if trusted_proxy_hops:
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=trusted_proxy_hops,
        x_proto=trusted_proxy_hops,
        x_host=trusted_proxy_hops,
        x_port=trusted_proxy_hops,
        x_prefix=trusted_proxy_hops,
    )


@app.errorhandler(400)
def handle_bad_request(error):
    return "The request could not be understood.", 400


@app.errorhandler(403)
def handle_forbidden(error):
    return "You are not authorized to access this resource.", 403


@app.errorhandler(404)
def handle_not_found(error):
    return "The requested page was not found.", 404


@app.errorhandler(500)
def handle_server_error(error):
    logger.exception("Unhandled application error")
    return "An unexpected error occurred. Please try again later.", 500


def csrf_token():
    token = session.get("csrf_token")
    if token is None:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": csrf_token}


@app.after_request
def prevent_auth_form_caching(response):
    if request.method == "GET" or request.path in {"/login", "/ownerlogin", "/adminlogin"}:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


@app.before_request
def protect_state_changing_requests():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        submitted_token = (
            request.form.get("csrf_token")
            or request.headers.get("X-CSRFToken")
            or request.headers.get("X-CSRF-Token")
            or ((request.get_json(silent=True) or {}).get("csrf_token") if request.is_json else None)
            or ""
        )
        session_token = session.get("csrf_token", "")
        logger.debug(
            "CSRF validation reached path=%s submitted=%s session=%s "
            "submitted_length=%d session_length=%d",
            request.path,
            "PRESENT" if submitted_token else "MISSING",
            "PRESENT" if session_token else "MISSING",
            len(submitted_token),
            len(session_token),
        )
        if not submitted_token or not session_token or not secrets.compare_digest(
            submitted_token,
            session_token,
        ):
            logger.debug("CSRF validation result=FAIL path=%s", request.path)
            if request.path.startswith("/farmer-ai/"):
                return jsonify({"success": False, "error": "Invalid CSRF token"}), 400
            return "Invalid CSRF token", 400
        logger.debug("CSRF validation result=PASS path=%s", request.path)


def login_required(role):
    session_key = "user_id" if role == "customer" else "owner_id"

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if session_key not in session:
                return redirect("/login" if role == "customer" else "/ownerlogin")
            return view(*args, **kwargs)
        return wrapped

    return decorator


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        is_admin = (
            "admin_id" in session
            or session.get("admin_logged_in")
            or session.get("configured_admin_authenticated")
        )
        if not is_admin:
            if "user_id" in session or "owner_id" in session:
                return "Access denied", 403
            return redirect("/adminlogin")
        try:
            return view(*args, **kwargs)
        except Error:
            if db is not None:
                safe_db_rollback()
            return "Admin data is temporarily unavailable.", 503

    return wrapped


def configured_admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_logged_in") and not session.get("configured_admin_authenticated"):
            return redirect("/admin/login")
        return view(*args, **kwargs)

    return wrapped


def valid_text(value, maximum=255):
    value = (value or "").strip()
    return value if value and len(value) <= maximum else None


def valid_phone(value):
    value = (value or "").strip()
    return value if value.isdigit() and len(value) == 10 else None


def valid_password(value):
    return value if value and len(value) >= 8 and len(value) <= 128 else None


def valid_email(value):
    value = (value or "").strip().lower()
    return value if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value) and len(value) <= 255 else None


def valid_admin_username(value):
    value = (value or "").strip().lower()
    return value if re.fullmatch(r"[a-z0-9_.-]{3,100}", value) else None


def valid_rent(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount > 0 and amount <= Decimal("99999999.99") else None

# =========================================================
# LANGUAGE SUPPORT
# =========================================================

SUPPORTED_LANGUAGES = ("en", "te", "ta", "hi")

TRANSLATIONS = {

    # =====================================================
    # ENGLISH
    # =====================================================

    "en": {

        "language": "Language",
        "english": "English",
        "telugu": "Telugu",
        "hindi": "Hindi",

        "vehicle_booking": "Business Vehicle Booking",
        "customer": "Customer",
        "customer_login": "Customer Login",

        "book_vehicle": "Book a Vehicle",
        "login": "Login",
        "register": "Register",
        "logout": "Logout",
        "welcome": "Welcome",
        "back_home": "Back to Home",

        "mobile_number": "Mobile Number",
        "customer_name": "Customer Name",

        "how_it_works": "How It Works",
        "choose_vehicle": "Choose Vehicle",
        "select_date": "Select Date",
        "confirm": "Confirm",

        "login_mobile": "Login using your mobile number.",
        "select_suitable_vehicle": "Select a suitable vehicle.",
        "choose_booking_date": "Choose your booking date.",
        "confirm_your_booking": "Confirm your vehicle booking.",

        "vehicle_types": "Vehicle Types",

        "tractor": "Tractor",
        "mini_truck": "Mini Truck",
        "pickup": "Pickup",
        "lorry": "Lorry",

        "tractor_desc": "Suitable for agricultural and goods transportation.",
        "mini_truck_desc": "Suitable for small and medium loads.",
        "pickup_desc": "Convenient for local transportation.",
        "lorry_desc": "Suitable for transporting larger loads.",

        "simple_fast_easy": "Simple • Fast • Easy",
        "transportation_message": "Book the right vehicle for your transportation needs.",
        "footer_message": "Simple transportation service for everyone.",

        "available_vehicles": "Available Vehicles",
        "my_bookings": "My Bookings",
        "vehicle_type": "Vehicle Type",
        "location": "Location",
        "from": "From",
        "to": "To",
        "rent": "Rent",
        "contact": "Contact",
        "book_now": "Book Now",

        "no_vehicles": "No Vehicles Available",
        "no_vehicles_message": "There are currently no vehicles available for booking.",

        "booking_date": "Booking Date",
        "confirm_booking": "Confirm Booking",

        # BOOKING STATUS
        "status": "Status",
        "accepted": "Accepted",
        "rejected": "Rejected",
        "pending": "Pending",
        "no_bookings": "No Bookings",
        "no_bookings_message": "You have not made any vehicle bookings yet.",

        # FARMER AI ASSISTANT
        "farmer_ai": "Farmer AI",
        "farmer_ai_assistant": "Farmer AI Crop Disease Assistant",
        "upload_leaf_image": "Upload Leaf Image",
        "analyze_leaf": "Analyze Leaf",
        "possible_disease": "Possible Disease",
        "crop": "Crop",
        "confidence": "Confidence",
        "symptoms": "Symptoms",
        "possible_causes": "Possible Causes",
        "treatment_management": "Treatment / Management",
        "prevention": "Prevention",
        "ask_ai_assistant": "Ask AI Assistant",
        "upload_another_image": "Upload another image",
        "image_unclear": "Image unclear",
        "consult_expert": "Please consult an agricultural expert",
        "analysis_failed": "Analysis failed",
        "ai_service_unavailable": "AI service unavailable"
    },


    # =====================================================
    # TELUGU
    # =====================================================

    "te": {

        "language": "భాష",
        "english": "English",
        "telugu": "తెలుగు",
        "hindi": "హిందీ",

        "vehicle_booking": "వాహన బుకింగ్",
        "customer": "కస్టమర్",
        "customer_login": "కస్టమర్ లాగిన్",

        "book_vehicle": "వాహనం బుక్ చేయండి",
        "login": "లాగిన్",
        "register": "నమోదు",
        "logout": "లాగ్ అవుట్",
        "welcome": "స్వాగతం",
        "back_home": "హోమ్‌కు తిరిగి వెళ్లండి",

        "mobile_number": "మొబైల్ నంబర్",
        "customer_name": "కస్టమర్ పేరు",

        "how_it_works": "ఇది ఎలా పనిచేస్తుంది",
        "choose_vehicle": "వాహనాన్ని ఎంచుకోండి",
        "select_date": "తేదీని ఎంచుకోండి",
        "confirm": "నిర్ధారించండి",

        "login_mobile": "మీ మొబైల్ నంబర్‌తో లాగిన్ అవ్వండి.",
        "select_suitable_vehicle": "అనువైన వాహనాన్ని ఎంచుకోండి.",
        "choose_booking_date": "మీ బుకింగ్ తేదీని ఎంచుకోండి.",
        "confirm_your_booking": "మీ వాహన బుకింగ్‌ను నిర్ధారించండి.",

        "vehicle_types": "వాహనాల రకాలు",

        "tractor": "ట్రాక్టర్",
        "mini_truck": "మినీ ట్రక్",
        "pickup": "పికప్",
        "lorry": "లారీ",

        "tractor_desc": "వ్యవసాయం మరియు సరుకు రవాణాకు అనుకూలమైనది.",
        "mini_truck_desc": "చిన్న మరియు మధ్యస్థ సరుకులకు అనుకూలమైనది.",
        "pickup_desc": "స్థానిక రవాణాకు అనుకూలమైనది.",
        "lorry_desc": "పెద్ద సరుకుల రవాణాకు అనుకూలమైనది.",

        "simple_fast_easy": "సులభం • వేగవంతం • సౌకర్యవంతం",
        "transportation_message": "మీ రవాణా అవసరాలకు సరైన వాహనాన్ని బుక్ చేసుకోండి.",
        "footer_message": "అందరికీ సులభమైన రవాణా సేవ.",

        "available_vehicles": "అందుబాటులో ఉన్న వాహనాలు",
        "my_bookings": "నా బుకింగ్స్",
        "vehicle_type": "వాహనం రకం",
        "location": "ప్రాంతం",
        "from": "ఎక్కడి నుండి",
        "to": "ఎక్కడికి",
        "rent": "అద్దె",
        "contact": "సంప్రదించండి",
        "book_now": "ఇప్పుడే బుక్ చేయండి",

        "no_vehicles": "వాహనాలు అందుబాటులో లేవు",
        "no_vehicles_message": "ప్రస్తుతం బుకింగ్ కోసం వాహనాలు అందుబాటులో లేవు.",

        "booking_date": "బుకింగ్ తేదీ",
        "confirm_booking": "బుకింగ్ నిర్ధారించండి",

        # BOOKING STATUS
        "status": "స్థితి",
        "accepted": "ఆమోదించబడింది",
        "rejected": "తిరస్కరించబడింది",
        "pending": "పెండింగ్‌లో ఉంది",
        "no_bookings": "బుకింగ్స్ లేవు",
        "no_bookings_message": "మీరు ఇంకా ఏ వాహనాన్ని బుక్ చేయలేదు.",

        # FARMER AI ASSISTANT
        "farmer_ai": "రైతు ఏఐ",
        "farmer_ai_assistant": "రైతు ఏఐ పంట వ్యాధి సహాయకుడు",
        "upload_leaf_image": "ఆకు చిత్రాన్ని అప్‌లోడ్ చేయండి",
        "analyze_leaf": "ఆకును విశ్లేషించండి",
        "possible_disease": "సాధ్యమయ్యే వ్యాధి",
        "crop": "పంట",
        "confidence": "విశ్వసనీయత",
        "symptoms": "లక్షణాలు",
        "possible_causes": "సాధ్యమైన కారణాలు",
        "treatment_management": "చికిత్స / యాజమాన్యం",
        "prevention": "నివారణ చిట్కాలు",
        "ask_ai_assistant": "ఏఐ సహాయకుడిని అడగండి",
        "upload_another_image": "మరొక చిత్రాన్ని అప్‌లోడ్ చేయండి",
        "image_unclear": "చిత్రం స్పష్టంగా లేదు",
        "consult_expert": "దయచేసి వ్యవసాయ నిపుణుడిని సంప్రదించండి",
        "analysis_failed": "విశ్లేషణ విఫలమైంది",
        "ai_service_unavailable": "ఏఐ సేవ అందుబాటులో లేదు"
    },


    # =====================================================
    # HINDI
    # =====================================================

    "hi": {

        "language": "भाषा",
        "english": "English",
        "telugu": "तेलुगु",
        "tamil": "तमिल",
        "hindi": "हिन्दी",

        "vehicle_booking": "वाहन बुकिंग",
        "customer": "ग्राहक",
        "customer_login": "ग्राहक लॉगिन",

        "book_vehicle": "वाहन बुक करें",
        "login": "लॉगिन",
        "register": "पंजीकरण",
        "logout": "लॉग आउट",
        "welcome": "स्वागत है",
        "back_home": "होम पर वापस जाएँ",

        "mobile_number": "मोबाइल नंबर",
        "customer_name": "ग्राहक का नाम",

        "how_it_works": "यह कैसे काम करता है",
        "choose_vehicle": "वाहन चुनें",
        "select_date": "तारीख चुनें",
        "confirm": "पुष्टि करें",

        "login_mobile": "अपने मोबाइल नंबर से लॉगिन करें।",
        "select_suitable_vehicle": "उपयुक्त वाहन चुनें।",
        "choose_booking_date": "अपनी बुकिंग की तारीख चुनें।",
        "confirm_your_booking": "अपने वाहन की बुकिंग की पुष्टि करें।",

        "vehicle_types": "वाहनों के प्रकार",

        "tractor": "ट्रैक्टर",
        "mini_truck": "मिनी ट्रक",
        "pickup": "पिकअप",
        "lorry": "लॉरी",

        "tractor_desc": "कृषि और सामान के परिवहन के लिए उपयुक्त।",
        "mini_truck_desc": "छोटे और मध्यम सामान के लिए उपयुक्त।",
        "pickup_desc": "स्थानीय परिवहन के लिए सुविधाजनक।",
        "lorry_desc": "बड़े सामान के परिवहन के लिए उपयुक्त।",

        "simple_fast_easy": "सरल • तेज़ • आसान",
        "transportation_message": "अपनी परिवहन आवश्यकताओं के लिए सही वाहन बुक करें।",
        "footer_message": "सभी के लिए सरल परिवहन सेवा।",

        "available_vehicles": "उपलब्ध वाहन",
        "my_bookings": "मेरी बुकिंग",
        "vehicle_type": "वाहन का प्रकार",
        "location": "स्थान",
        "from": "कहाँ से",
        "to": "कहाँ तक",
        "rent": "किराया",
        "contact": "संपर्क",
        "book_now": "अभी बुक करें",

        "no_vehicles": "कोई वाहन उपलब्ध नहीं है",
        "no_vehicles_message": "फिलहाल बुकिंग के लिए कोई वाहन उपलब्ध नहीं है।",

        "booking_date": "बुकिंग की तारीख",
        "confirm_booking": "बुकिंग की पुष्टि करें",

        # BOOKING STATUS
        "status": "स्थिति",
        "accepted": "स्वीकृत",
        "rejected": "अस्वीकृत",
        "pending": "लंबित",
        "no_bookings": "कोई बुकिंग नहीं",
        "no_bookings_message": "आपने अभी तक कोई वाहन बुक नहीं किया है।",

        # FARMER AI ASSISTANT
        "farmer_ai": "किसान एआई",
        "farmer_ai_assistant": "किसान एआई फसल रोग सहायक",
        "upload_leaf_image": "पत्ती की तस्वीर अपलोड करें",
        "analyze_leaf": "पत्ती का विश्लेषण करें",
        "possible_disease": "संभावित रोग",
        "crop": "फसल",
        "confidence": "विश्वसनीयता",
        "symptoms": "लक्षण",
        "possible_causes": "संभावित कारण",
        "treatment_management": "उपचार / प्रबंधन",
        "prevention": "रोकथाम के उपाय",
        "ask_ai_assistant": "एआई सहायक से पूछें",
        "upload_another_image": "दूसरी तस्वीर अपलोड करें",
        "image_unclear": "तस्वीर स्पष्ट नहीं है",
        "consult_expert": "कृपया किसी कृषि विशेषज्ञ से सलाह लें",
        "analysis_failed": "विश्लेषण विफल रहा",
        "ai_service_unavailable": "एआई सेवा उपलब्ध नहीं है"
    },

    # =====================================================
    # TAMIL
    # =====================================================

    "ta": {
        "language": "மொழி",
        "english": "English",
        "telugu": "తెలుగు",
        "tamil": "தமிழ்",
        "hindi": "हिन्दी",
        "vehicle_booking": "வணிக வாகன முன்பதிவு",
        "customer": "வாடிக்கையாளர்",
        "customer_login": "வாடிக்கையாளர் உள்நுழைவு",
        "book_vehicle": "வாகனத்தை முன்பதிவு செய்க",
        "login": "உள்நுழைவு",
        "register": "பதிவு",
        "logout": "வெளியேறு",
        "welcome": "வரவேற்கிறோம்",
        "back_home": "முகப்புக்குத் திரும்பு",
        "mobile_number": "கைபேசி எண்",
        "customer_name": "வாடிக்கையாளர் பெயர்",
        "available_vehicles": "கிடைக்கும் வாகனங்கள்",
        "my_bookings": "எனது முன்பதிவுகள்",
        "vehicle_type": "வாகன வகை",
        "location": "இடம்",
        "from": "இருந்து",
        "to": "வரை",
        "rent": "வாடகை",
        "contact": "தொடர்பு",
        "book_now": "இப்போது முன்பதிவு செய்க",
        "no_vehicles": "வாகனங்கள் கிடைக்கவில்லை",
        "no_vehicles_message": "தற்போது முன்பதிவுக்கு வாகனங்கள் இல்லை.",
        "booking_date": "முன்பதிவு தேதி",
        "confirm_booking": "முன்பதிவை உறுதிப்படுத்து",
        "status": "நிலை",
        "accepted": "ஏற்கப்பட்டது",
        "rejected": "நிராகரிக்கப்பட்டது",
        "pending": "நிலுவையில்",
        "no_bookings": "முன்பதிவுகள் இல்லை",
        "no_bookings_message": "நீங்கள் இன்னும் எந்த வாகனத்தையும் முன்பதிவு செய்யவில்லை.",
        "farmer_ai": "விவசாயி AI",
        "farmer_ai_assistant": "விவசாய பயிர் நோய் உதவியாளர்",
        "upload_leaf_image": "இலை படத்தை பதிவேற்றவும்",
        "analyze_leaf": "இலையை பகுப்பாய்வு செய்க",
        "possible_disease": "சாத்தியமான நோய்",
        "crop": "பயிர்",
        "confidence": "நம்பகத்தன்மை",
        "symptoms": "அறிகுறிகள்",
        "possible_causes": "சாத்தியமான காரணங்கள்",
        "treatment_management": "சிகிச்சை / மேலாண்மை",
        "prevention": "தடுப்பு",
        "ask_ai_assistant": "AI உதவியாளரிடம் கேளுங்கள்",
        "analysis_failed": "பகுப்பாய்வு தோல்வியடைந்தது",
        "ai_service_unavailable": "AI சேவை கிடைக்கவில்லை",
        "footer_message": "அனைவருக்கும் எளிய போக்குவரத்து சேவை."
    }

}
# =========================================================
# MAKE TRANSLATIONS AVAILABLE TO HTML
# =========================================================

@app.context_processor
def inject_language():

    language = session.get("language", "en")

    return {
        "t": TRANSLATIONS.get(
            language,
            TRANSLATIONS["en"]
        ),
        "current_language": language
    }
# =========================================================
# CHANGE LANGUAGE
# =========================================================

@app.route("/set_language/<language>", methods=["POST"], endpoint="change_language")
def change_language(language):
    """Change the UI language while preserving the existing CSRF-protected POST flow."""
    language = (language or "").strip().lower()
    if language not in SUPPORTED_LANGUAGES:
        language = "en"

    session["language"] = language

    # Referrer is only used as a local navigation convenience.  Never redirect
    # to an arbitrary external URL supplied by a client.
    referrer = request.referrer or "/"
    if referrer.startswith("/") and not referrer.startswith("//"):
        return redirect(referrer)
    return redirect("/")
# =========================================================
# MYSQL DATABASE CONNECTION
# =========================================================

db = None
cursor = None
payment_table_initialized = False
MAX_DB_RETRIES = 3

def get_db_config():
    host = os.getenv("MYSQL_HOST") or os.getenv("DB_HOST")
    port = os.getenv("MYSQL_PORT") or os.getenv("DB_PORT")
    user = os.getenv("MYSQL_USER") or os.getenv("DB_USER")
    password = os.getenv("MYSQL_PASSWORD") or os.getenv("DB_PASSWORD")
    database = os.getenv("MYSQL_DATABASE") or os.getenv("DB_NAME") or os.getenv("DB_DATABASE")

    if not IS_PRODUCTION:
        host = host or "localhost"
        port = port or "3306"
        user = user or "root"
        database = database or "vehicle_booking"

    config = {
        "host": host,
        "port": int(port or "3306"),
        "user": user,
        "password": password,
        "database": database,
    }

    # SSL / TLS configuration for cloud MySQL (e.g. Aiven)
    ssl_ca = os.getenv("MYSQL_SSL_CA") or os.getenv("DB_SSL_CA")
    ssl_disabled_val = os.getenv("MYSQL_SSL_DISABLED", "").strip().lower()
    if ssl_disabled_val in {"1", "true", "yes"}:
        config["ssl_disabled"] = True
    else:
        config["ssl_disabled"] = False
        if ssl_ca and os.path.isfile(ssl_ca):
            config["ssl_ca"] = ssl_ca
            config["ssl_verify_cert"] = True
            if os.getenv("MYSQL_SSL_VERIFY_IDENTITY", "").strip().lower() in {"1", "true", "yes"}:
                config["ssl_verify_identity"] = True

    return config

db_config = get_db_config()

def _is_lost_connection(error):
    return getattr(error, "errno", None) in {2006, 2013}


def _reset_db_connection():
    """Discard a possibly broken connection without raising a second database error."""
    global db, cursor
    old_db = db
    db = None
    cursor = None
    if old_db is not None:
        try:
            old_db.close()
        except Error:
            logger.debug("Ignoring error while closing broken database connection", exc_info=True)


class ResilientCursor:
    """Retry cursor operations after MySQL reports a dropped connection."""

    def __init__(self, mysql_cursor):
        self._cursor = mysql_cursor

    def execute(self, operation, params=None, *args, **kwargs):
        global cursor
        for attempt in range(MAX_DB_RETRIES):
            try:
                return self._cursor.execute(operation, params, *args, **kwargs)
            except Error as error:
                if not _is_lost_connection(error) or attempt == MAX_DB_RETRIES - 1:
                    raise
                logger.warning(
                    "MySQL connection lost during query; reconnecting (attempt %d/%d)",
                    attempt + 1,
                    MAX_DB_RETRIES - 1,
                )
                _reset_db_connection()
                if ensure_db_connection() is None:
                    raise
                self._cursor = db.cursor()
                cursor = self

    def __getattr__(self, name):
        return getattr(self._cursor, name)


def ensure_db_connection():
    """Ensure the shared connector and cursor are connected and healthy."""
    global db, cursor
    if not db_config.get("password"):
        if IS_PRODUCTION:
            logger.error("Database connection refused: MYSQL_PASSWORD is missing in production")
        return None
    try:
        if db is None or not db.is_connected():
            _reset_db_connection()
            connect_kwargs = {k: v for k, v in db_config.items() if v is not None}
            db = mysql.connector.connect(**connect_kwargs)
            cursor = ResilientCursor(db.cursor())
            logger.info("Database connection established to database: %s", db_config.get("database"))
        else:
            db.ping(reconnect=True, attempts=3, delay=1)
            if cursor is None:
                cursor = ResilientCursor(db.cursor())
    except Error as error:
        logger.error("Database connection is unavailable: %s (%s)", type(error).__name__, str(error))
        _reset_db_connection()
    return db


def safe_db_rollback():
    """Rollback only on a live connection, then discard dead state if necessary."""
    if db is None:
        return
    try:
        if db.is_connected():
            db.rollback()
        else:
            _reset_db_connection()
    except Error:
        logger.debug("Database rollback failed; discarding connection", exc_info=True)
        _reset_db_connection()

# Initialize at startup if password is set
ensure_db_connection()


def ensure_payment_table():
    """Ensure the demo payments table exists without altering the existing schema."""
    global payment_table_initialized
    if payment_table_initialized:
        return True
    if ensure_db_connection() is None:
        return False

    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                booking_id INT UNSIGNED NOT NULL,
                user_id INT UNSIGNED NOT NULL,
                amount DECIMAL(10, 2) NOT NULL,
                currency VARCHAR(10) NOT NULL DEFAULT 'INR',
                payment_method VARCHAR(50) NOT NULL,
                transaction_reference VARCHAR(100) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'Paid',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                paid_at TIMESTAMP NULL DEFAULT NULL,
                PRIMARY KEY (id),
                UNIQUE KEY uq_payments_booking (booking_id),
                UNIQUE KEY uq_payments_transaction_reference (transaction_reference),
                KEY idx_payments_user_id (user_id)
            ) ENGINE=InnoDB
            """
        )
        db.commit()
        payment_table_initialized = True
        return True
    except Error:
        safe_db_rollback()
        logger.exception("Could not create the payments table")
        return False


ensure_payment_table()


@app.before_request
def verify_db_connectivity():
    """Ensure connection is alive before each request."""
    if request.path.startswith("/static/"):
        return None
    ensure_db_connection()
    ensure_payment_table()




# =========================================================
# HOME PAGE
# =========================================================

@app.route("/")
def account_choice():

    return render_template("account_choice.html")


@app.route("/home")
def home():

    return render_template("index.html")


# =========================================================
# CUSTOMER REGISTRATION
# =========================================================

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        name = valid_text(request.form.get("name"), 100)
        phone = valid_phone(request.form.get("phone"))
        email = valid_email(request.form.get("email")) if request.form.get("email") else None
        password = valid_password(request.form.get("password"))
        confirm_password = request.form.get("confirm_password", "")

        if not name or not phone or not password or password != confirm_password:
            return render_template(
                "register.html",
                error="Enter valid registration details and matching passwords.",
            ), 400
        if request.form.get("email") and not email:
            return render_template(
                "register.html",
                error="Enter a valid email address.",
            ), 400

        if email:
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cursor.fetchone():
                return render_template(
                    "register.html",
                    error=(
                        "This email address is already registered. "
                        "Please use a different email or sign in."
                    ),
                ), 409

        cursor.execute("SELECT id FROM users WHERE phone = %s", (phone,))
        if cursor.fetchone():
            return render_template(
                "register.html",
                error=(
                    "This mobile number is already registered. "
                    "Please use a different number or sign in."
                ),
            ), 409

        try:
            cursor.execute(
                "INSERT INTO users (name, phone, email, password_hash) "
                "VALUES (%s, %s, %s, %s)",
                (name, phone, email, generate_password_hash(password)),
            )
            db.commit()
        except IntegrityError as error:
            safe_db_rollback()
            if getattr(error, "errno", None) != 1062:
                logger.exception("Customer registration insert failed")
                raise

            cursor.execute(
                "SELECT phone, email FROM users "
                "WHERE phone = %s OR email = %s LIMIT 1",
                (phone, email),
            )
            existing_user = cursor.fetchone()
            if existing_user and existing_user[1] and email and existing_user[1].lower() == email.lower():
                duplicate_message = (
                    "This email address is already registered. "
                    "Please use a different email or sign in."
                )
            elif existing_user and existing_user[0] == phone:
                duplicate_message = (
                    "This mobile number is already registered. "
                    "Please use a different number or sign in."
                )
            else:
                logger.error("Customer registration encountered an unknown duplicate constraint")
                raise

            return render_template("register.html", error=duplicate_message), 409

        return redirect("/login")

    csrf_token()
    return render_template("register.html")


# =========================================================
# CUSTOMER LOGIN
# =========================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        phone = valid_phone(request.form.get("phone"))
        password = valid_password(request.form.get("password"))

        if not phone or not password:

            return """
            <h2>Invalid Mobile Number ❌</h2>

            <p>Enter a valid mobile number and password.</p>

            <a href="/login">
            Try Again
            </a>
            """

        sql = """
        SELECT id, name, phone, email, password_hash, is_active
        FROM users
        WHERE phone = %s
        """

        cursor.execute(
            sql,
            (phone,)
        )

        user = cursor.fetchone()
        user_active = bool(user[5]) if user and len(user) > 5 else bool(user)
        password_hash = user[4] if user and len(user) > 4 else (user[3] if user else None)
        password_valid = False
        if password_hash:
            try:
                password_valid = check_password_hash(password_hash, password)
            except (ValueError, TypeError):
                logger.exception("Invalid password hash for customer phone ending %s", phone[-2:])

        if user and user_active and password_valid:

            logger.info("Customer login successful for phone ending %s", phone[-4:])
            session.clear()
            session.permanent = True
            session["user_id"] = user[0]
            session["user_name"] = user[1]
            session["user_phone"] = user[2]
            if user and len(user) > 4:
                session["user_email"] = user[3]
            else:
                cursor.execute("SELECT email FROM users WHERE id = %s", (user[0],))
                session["user_email"] = (cursor.fetchone() or (None,))[0]

            return redirect("/vehicles")

        else:

            if not user:
                logger.warning(
                    "Customer login failed: account not found in database '%s' for phone ending %s",
                    db_config.get("database"),
                    phone[-4:],
                )
            elif not user_active:
                logger.warning("Customer login failed: account inactive for customer ID %s", user[0])
            elif not password_hash:
                logger.warning("Customer login failed: password hash missing for customer ID %s", user[0])
            elif not password_valid:
                logger.warning("Customer login failed: password mismatch for customer ID %s", user[0])

            return f"""
            <h2>Invalid Customer Login</h2>

            <p>The mobile number or password is incorrect.</p>

            <a href="/login">
            Try Again
            </a>
            """

    csrf_token()
    return render_template("login.html")


# =========================================================
# CUSTOMER PASSWORD RESET
# =========================================================

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    generic_message = (
        "If an account exists for that email, a password reset link has been sent. "
        "Please check your inbox."
    )

    if request.method == "POST":
        email = valid_email(request.form.get("email"))
        if email:
            try:
                cursor.execute(
                    "SELECT id, email FROM users "
                    "WHERE LOWER(email) = LOWER(%s) AND is_active = TRUE",
                    (email,),
                )
                user = cursor.fetchone()
                if user:
                    reset_token = secrets.token_urlsafe(32)
                    token_hash = hashlib.sha256(reset_token.encode("utf-8")).hexdigest()
                    expires_at = datetime.utcnow() + timedelta(minutes=30)
                    cursor.execute(
                        "UPDATE password_reset_tokens SET used_at = UTC_TIMESTAMP() "
                        "WHERE user_id = %s AND used_at IS NULL",
                        (user[0],),
                    )
                    cursor.execute(
                        "INSERT INTO password_reset_tokens "
                        "(user_id, token_hash, expires_at) VALUES (%s, %s, %s)",
                        (user[0], token_hash, expires_at),
                    )
                    db.commit()
                    reset_url = url_for("reset_password", token=reset_token, _external=True)
                    try:
                        send_password_reset_email(user[1], reset_url)
                    except Exception:
                        logger.exception("Password reset email failed")
                else:
                    logger.info("Password reset requested for an unknown customer email")
            except Error:
                safe_db_rollback()
                logger.exception("Password reset request could not be stored")

        flash(generic_message, "info")
        return redirect("/forgot-password")

    csrf_token()
    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not valid_password(password):
            flash("Password must be 8 to 128 characters.", "error")
            return render_template("reset_password.html", token=token), 400
        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template("reset_password.html", token=token), 400

        try:
            cursor.execute(
                "SELECT id, user_id FROM password_reset_tokens "
                "WHERE token_hash = %s AND used_at IS NULL "
                "AND expires_at > UTC_TIMESTAMP()",
                (token_hash,),
            )
            reset_record = cursor.fetchone()
            if not reset_record:
                safe_db_rollback()
                flash("This password reset link is invalid or expired.", "error")
                return redirect("/forgot-password")

            cursor.execute(
                "UPDATE users SET password_hash = %s "
                "WHERE id = %s AND is_active = TRUE",
                (generate_password_hash(password), reset_record[1]),
            )
            if cursor.rowcount != 1:
                safe_db_rollback()
                flash("This password reset link is invalid or expired.", "error")
                return redirect("/forgot-password")

            cursor.execute(
                "UPDATE password_reset_tokens SET used_at = UTC_TIMESTAMP() "
                "WHERE id = %s AND used_at IS NULL",
                (reset_record[0],),
            )
            if cursor.rowcount != 1:
                safe_db_rollback()
                flash("This password reset link is invalid or expired.", "error")
                return redirect("/forgot-password")
            db.commit()
        except Error:
            safe_db_rollback()
            logger.exception("Password reset could not be completed")
            flash("Password reset could not be completed. Please try again.", "error")
            return redirect("/forgot-password")

        flash("Your password has been reset. You can now log in.", "success")
        return redirect("/login")

    cursor.execute(
        "SELECT id FROM password_reset_tokens "
        "WHERE token_hash = %s AND used_at IS NULL "
        "AND expires_at > UTC_TIMESTAMP()",
        (token_hash,),
    )
    if cursor.fetchone() is None:
        return "This password reset link is invalid or expired.", 400

    csrf_token()
    return render_template("reset_password.html", token=token)


# =========================================================
# CUSTOMER LOGOUT
# =========================================================

@app.route("/logout", methods=["POST"])
def logout():

    session.clear()

    return redirect("/")


# =========================================================
# OWNER REGISTRATION
# =========================================================

@app.route("/ownerregister", methods=["GET", "POST"])
def ownerregister():

    if request.method == "POST":

        owner_name = valid_text(request.form.get("owner_name"), 100)
        phone = valid_phone(request.form.get("phone"))
        email = valid_email(request.form.get("email"))
        password = valid_password(request.form.get("password"))

        if not owner_name or not phone or not email or not password:
            return "Invalid owner registration details", 400

        check_sql = """
        SELECT id
        FROM owners
        WHERE email = %s
        """

        cursor.execute(
            check_sql,
            (email,)
        )

        existing_owner = cursor.fetchone()

        if existing_owner:

            return """
            <h2>Email Already Registered ❌</h2>

            <p>
            This owner email is already registered.
            </p>

            <a href="/ownerlogin">
            Go to Owner Login
            </a>

            <br><br>

            <a href="/ownerregister">
            Register Another Owner
            </a>
            """

        sql = """
        INSERT INTO owners
        (owner_name, phone, email, password_hash)
        VALUES (%s, %s, %s, %s)
        """

        values = (
            owner_name,
            phone,
            email,
            generate_password_hash(password)
        )

        cursor.execute(
            sql,
            values
        )

        db.commit()

        return """
        <h2>Owner Registration Successful! ✅</h2>

        <p>
        Your owner account has been created.
        </p>

        <a href="/ownerlogin">
        Login as Owner
        </a>
        """

    return render_template("owner_register.html")


# =========================================================
# OWNER LOGIN
# =========================================================

@app.route("/ownerlogin", methods=["GET", "POST"])
def ownerlogin():

    if request.method == "POST":

        email = valid_email(request.form.get("email"))
        password = request.form.get("password", "")

        if not email or not valid_password(password):
            return "Invalid owner login details", 400

        sql = """
        SELECT id, owner_name, phone, email, password_hash
        FROM owners
        WHERE email = %s
        AND is_active = TRUE
        """

        cursor.execute(
            sql,
            (email,)
        )

        owner = cursor.fetchone()

        valid_owner = bool(owner and owner[4] and check_password_hash(owner[4], password))

        if owner and valid_owner:

            session.clear()
            session.permanent = True
            session["owner_id"] = owner[0]
            session["owner_name"] = owner[1]
            session["owner_phone"] = owner[2]
            session["owner_email"] = owner[3]

            return redirect("/owner_dashboard")

        else:

            return """
            <h2>Invalid Owner Login ❌</h2>

            <p>
            Email or password is incorrect.
            </p>

            <a href="/ownerlogin">
            Try Again
            </a>

            <br><br>

            <a href="/ownerregister">
            Register as Owner
            </a>
            """

    csrf_token()
    return render_template("owner_login.html")


# =========================================================
# ADMIN LOGIN
# =========================================================

@app.route("/adminlogin", methods=["GET", "POST"])
def adminlogin():
    if request.method == "POST":
        username = valid_admin_username(request.form.get("username"))
        password = request.form.get("password", "")

        if not username or not valid_password(password):
            return render_template(
                "admin_login.html",
                error="Enter a valid username and password.",
            ), 400

        cursor.execute(
            "SELECT id, username, password_hash FROM admins "
            "WHERE username = %s AND is_active = TRUE",
            (username,),
        )
        admin = cursor.fetchone()

        if admin and check_password_hash(admin[2], password):
            session.clear()
            session.permanent = True
            session["admin_id"] = admin[0]
            session["admin_username"] = admin[1]
            return redirect("/admin_dashboard")

        return render_template(
            "admin_login.html",
            error="Invalid admin login.",
        ), 200

    csrf_token()
    return render_template("admin_login.html")


@app.route("/admin_logout", methods=["POST"])
@admin_required
def admin_logout():
    session.clear()
    return redirect("/adminlogin")


# =========================================================
# ADMIN READ-ONLY DASHBOARD
# =========================================================

def admin_count(sql):
    cursor.execute(sql)
    return cursor.fetchone()[0]


@app.route("/admin_dashboard")
@admin_required
def admin_dashboard():
    total_customers = admin_count("SELECT COUNT(*) FROM users")
    total_owners = admin_count("SELECT COUNT(*) FROM owners")
    total_vehicles = admin_count("SELECT COUNT(*) FROM vehicles")
    total_bookings = admin_count("SELECT COUNT(*) FROM bookings")
    pending_bookings = admin_count(
        "SELECT COUNT(*) FROM bookings WHERE LOWER(TRIM(status)) = 'pending'"
    )
    active_vehicles = admin_count(
        "SELECT COUNT(*) FROM vehicles v "
        "INNER JOIN owners o ON o.id = v.owner_id "
        "WHERE v.is_active = TRUE AND o.is_active = TRUE "
        "AND EXISTS (SELECT 1 FROM vehicle_routes vr WHERE vr.vehicle_id = v.id)"
    )

    cursor.execute(
        "SELECT b.id, b.customer_name, v.vehicle_name, b.booking_date, b.status "
        "FROM bookings b "
        "INNER JOIN vehicles v ON v.id = b.vehicle_id "
        "ORDER BY b.id DESC LIMIT 10"
    )
    recent_bookings = cursor.fetchall()

    return render_template(
        "admin.html",
        page="dashboard",
        total_customers=total_customers,
        total_owners=total_owners,
        total_vehicles=total_vehicles,
        total_bookings=total_bookings,
        pending_bookings=pending_bookings,
        active_vehicles=active_vehicles,
        recent_bookings=recent_bookings,
    )


# =========================================================
# CONFIGURED ADMIN LOGIN AND DASHBOARD
# =========================================================

@app.route("/admin/login", methods=["GET", "POST"])
def configured_admin_login():
    if session.get("admin_logged_in") or session.get("configured_admin_authenticated"):
        return redirect("/admin/dashboard")

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        configured_username = (os.getenv("ADMIN_USERNAME") or "admin").strip()
        configured_password = os.getenv("ADMIN_PASSWORD") or "change_this_password"

        if (
            username
            and configured_username
            and configured_password
            and secrets.compare_digest(username.lower(), configured_username.lower())
            and secrets.compare_digest(password, configured_password)
        ):
            # Isolate admin session by removing customer/owner keys
            for key in (
                "user_id", "user_name", "user_phone", "user_email",
                "owner_id", "owner_name", "owner_phone", "owner_email"
            ):
                session.pop(key, None)

            session.permanent = True
            session["admin_logged_in"] = True
            session["admin_username"] = configured_username
            session["configured_admin_authenticated"] = True
            session["configured_admin_username"] = configured_username
            return redirect("/admin/dashboard")

        return render_template(
            "admin_login.html",
            login_action=url_for("configured_admin_login"),
            error="Invalid admin username or password.",
        ), 200

    csrf_token()
    return render_template(
        "admin_login.html",
        login_action=url_for("configured_admin_login"),
    )


@app.route("/admin/dashboard")
@configured_admin_required
def configured_admin_dashboard():
    ensure_db_connection()
    metrics = {
        "total_customers": 0,
        "total_owners": 0,
        "total_vehicles": 0,
        "total_bookings": 0,
        "pending_bookings": 0,
        "accepted_bookings": 0,
        "rejected_bookings": 0,
        "total_ratings": 0,
        "total_payments": 0,
    }
    recent_bookings = []
    customers = []
    owners = []
    vehicles = []
    payments = []
    ratings = []
    error_message = None

    try:
        metric_queries = {
            "total_customers": "SELECT COUNT(*) FROM users",
            "total_owners": "SELECT COUNT(*) FROM owners",
            "total_vehicles": "SELECT COUNT(*) FROM vehicles",
            "total_bookings": "SELECT COUNT(*) FROM bookings",
            "pending_bookings": "SELECT COUNT(*) FROM bookings WHERE LOWER(TRIM(status)) = 'pending'",
            "accepted_bookings": "SELECT COUNT(*) FROM bookings WHERE LOWER(TRIM(status)) = 'accepted'",
            "rejected_bookings": "SELECT COUNT(*) FROM bookings WHERE LOWER(TRIM(status)) = 'rejected'",
            "total_ratings": "SELECT COUNT(*) FROM ratings",
            "total_payments": "SELECT COUNT(*) FROM payments",
        }
        for name, query in metric_queries.items():
            cursor.execute(query)
            row = cursor.fetchone()
            metrics[name] = row[0] if row else 0

        # Recent Bookings with Route Information
        cursor.execute(
            """
            SELECT
                b.id,
                COALESCE(b.customer_name, 'Customer') AS customer_name,
                COALESCE(v.vehicle_name, 'Unknown Vehicle') AS vehicle_name,
                b.booking_date,
                vr.from_location,
                vr.to_location,
                b.status
            FROM bookings b
            LEFT JOIN vehicles v ON v.id = b.vehicle_id
            LEFT JOIN vehicle_routes vr ON vr.id = b.route_id
            ORDER BY b.id DESC
            LIMIT 15
            """
        )
        raw_bookings = cursor.fetchall() or []
        for b in raw_bookings:
            b_id, cust, veh, b_date, from_loc, to_loc, status = b[0], b[1], b[2], b[3], b[4], b[5], b[6]
            if from_loc and to_loc:
                route_display = f"{from_loc} → {to_loc}"
            elif from_loc:
                route_display = f"From {from_loc}"
            elif to_loc:
                route_display = f"To {to_loc}"
            else:
                route_display = "—"

            recent_bookings.append({
                "id": b_id,
                "customer": cust,
                "vehicle": veh,
                "booking_date": b_date,
                "route": route_display,
                "status": status or "Pending",
            })

        # Registered Customers (passwords never queried or displayed)
        cursor.execute(
            "SELECT id, name, phone, COALESCE(email, '—') AS email, is_active "
            "FROM users ORDER BY id DESC LIMIT 50"
        )
        raw_customers = cursor.fetchall() or []
        customers = [
            {
                "id": c[0],
                "name": c[1] or "Unknown",
                "phone": c[2] or "—",
                "email": c[3] or "—",
                "is_active": bool(c[4]),
            }
            for c in raw_customers
        ]

        # Registered Vehicle Owners (passwords never queried or displayed)
        cursor.execute(
            "SELECT id, owner_name, phone, COALESCE(email, '—') AS email, is_active "
            "FROM owners ORDER BY id DESC LIMIT 50"
        )
        raw_owners = cursor.fetchall() or []
        owners = [
            {
                "id": o[0],
                "name": o[1] or "Unknown",
                "phone": o[2] or "—",
                "email": o[3] or "—",
                "is_active": bool(o[4]),
            }
            for o in raw_owners
        ]

        # Registered Vehicles
        cursor.execute(
            """
            SELECT
                v.id,
                v.vehicle_name,
                COALESCE(v.vehicle_type, 'General') AS vehicle_type,
                COALESCE(o.owner_name, 'Unknown Owner') AS owner_name,
                COALESCE(v.location, '—') AS location,
                COALESCE(v.contact_number, '—') AS contact_number,
                COALESCE(v.status, 'Available') AS status,
                v.is_active
            FROM vehicles v
            LEFT JOIN owners o ON v.owner_id = o.id
            ORDER BY v.id DESC
            LIMIT 50
            """
        )
        raw_vehicles = cursor.fetchall() or []
        vehicles = [
            {
                "id": v[0],
                "name": v[1],
                "type": v[2],
                "owner": v[3],
                "location": v[4],
                "contact": v[5],
                "status": v[6],
                "is_active": bool(v[7]),
            }
            for v in raw_vehicles
        ]

        # Processed Payments (no sensitive credentials)
        cursor.execute(
            """
            SELECT
                p.id,
                p.booking_id,
                p.amount,
                COALESCE(p.currency, 'INR') AS currency,
                COALESCE(p.payment_method, 'Card') AS payment_method,
                COALESCE(p.status, 'Completed') AS status,
                COALESCE(p.paid_at, p.created_at) AS payment_date
            FROM payments p
            ORDER BY p.id DESC
            LIMIT 50
            """
        )
        raw_payments = cursor.fetchall() or []
        payments = [
            {
                "id": p[0],
                "booking_id": p[1],
                "amount": f"{p[2]:.2f}" if p[2] is not None else "0.00",
                "currency": p[3],
                "method": p[4],
                "status": p[5],
                "date": p[6].strftime("%Y-%m-%d %H:%M") if p[6] and hasattr(p[6], "strftime") else (str(p[6]) if p[6] else "—"),
            }
            for p in raw_payments
        ]

        # Customer Ratings & Reviews
        cursor.execute(
            """
            SELECT
                r.id,
                r.booking_id,
                COALESCE(b.customer_name, r.customer_phone, 'Customer') AS customer_name,
                COALESCE(v.vehicle_name, 'Vehicle') AS vehicle_name,
                COALESCE(r.rating, 5) AS rating,
                COALESCE(r.review, '—') AS review,
                r.created_at
            FROM ratings r
            LEFT JOIN vehicles v ON r.vehicle_id = v.id
            LEFT JOIN bookings b ON r.booking_id = b.id
            ORDER BY r.id DESC
            LIMIT 50
            """
        )
        raw_ratings = cursor.fetchall() or []
        ratings = [
            {
                "id": r[0],
                "booking_id": r[1],
                "customer": r[2],
                "vehicle": r[3],
                "rating": r[4],
                "review": r[5],
                "date": r[6].strftime("%Y-%m-%d") if r[6] and hasattr(r[6], "strftime") else (str(r[6]) if r[6] else "—"),
            }
            for r in raw_ratings
        ]

    except Error as e:
        logger.error("Admin dashboard database query error: %s", e)
        safe_db_rollback()
        error_message = "Unable to load complete real-time records from database."

    return render_template(
        "admin_dashboard.html",
        metrics=metrics,
        recent_bookings=recent_bookings,
        customers=customers,
        owners=owners,
        vehicles=vehicles,
        payments=payments,
        ratings=ratings,
        error_message=error_message,
    )



@app.route("/admin/logout", methods=["POST"])
@configured_admin_required
def configured_admin_logout():
    session.pop("admin_logged_in", None)
    session.pop("admin_username", None)
    session.pop("configured_admin_authenticated", None)
    session.pop("configured_admin_username", None)
    return redirect("/admin/login")


@app.route("/admin/customers")
@admin_required
def admin_customers():
    search = valid_text(request.args.get("q"), 100) or ""
    pattern = f"%{search}%"
    cursor.execute(
        "SELECT id, name, phone, is_active FROM users "
        "WHERE name LIKE %s OR phone LIKE %s ORDER BY id DESC",
        (pattern, pattern),
    )
    return render_template(
        "admin.html",
        page="customers",
        search=search,
        records=cursor.fetchall(),
    )


@app.route("/admin/owners")
@admin_required
def admin_owners():
    search = valid_text(request.args.get("q"), 100) or ""
    pattern = f"%{search}%"
    cursor.execute(
        "SELECT id, owner_name, phone, email, is_active FROM owners "
        "WHERE owner_name LIKE %s OR phone LIKE %s OR email LIKE %s "
        "ORDER BY id DESC",
        (pattern, pattern, pattern),
    )
    return render_template(
        "admin.html",
        page="owners",
        search=search,
        records=cursor.fetchall(),
    )


@app.route("/admin/vehicles")
@admin_required
def admin_vehicles():
    search = valid_text(request.args.get("q"), 100) or ""
    pattern = f"%{search}%"
    cursor.execute(
        "SELECT v.id, v.vehicle_name, v.vehicle_type, o.owner_name, v.location, v.is_active "
        "FROM vehicles v INNER JOIN owners o ON o.id = v.owner_id "
        "WHERE v.vehicle_name LIKE %s OR v.vehicle_type LIKE %s "
        "OR o.owner_name LIKE %s OR v.location LIKE %s ORDER BY v.id DESC",
        (pattern, pattern, pattern, pattern),
    )
    return render_template(
        "admin.html",
        page="vehicles",
        search=search,
        records=cursor.fetchall(),
    )


@app.route("/admin/bookings")
@admin_required
def admin_bookings():
    search = valid_text(request.args.get("q"), 100) or ""
    status = request.args.get("status", "").strip().title()
    if status not in {"", "Pending", "Accepted", "Rejected"}:
        return "Invalid booking status", 400

    search_pattern = f"%{search}%"
    status_clause = ""
    values = [search_pattern, search_pattern, search_pattern]
    if status:
        status_clause = "AND LOWER(TRIM(b.status)) = LOWER(%s)"
        values.append(status)

    cursor.execute(
        "SELECT b.id, b.customer_name, v.vehicle_name, b.booking_date, b.status "
        "FROM bookings b INNER JOIN vehicles v ON v.id = b.vehicle_id "
        "WHERE (b.customer_name LIKE %s OR b.phone LIKE %s OR v.vehicle_name LIKE %s) "
        + status_clause
        + " ORDER BY b.id DESC",
        tuple(values),
    )
    return render_template(
        "admin.html",
        page="bookings",
        search=search,
        status=status,
        records=cursor.fetchall(),
    )


def admin_status_value(value):
    value = (value or "").strip().lower()
    return value if value in {"active", "inactive"} else None


@app.route("/admin/customers/<int:customer_id>/status", methods=["POST"])
@admin_required
def admin_customer_status(customer_id):
    requested_status = admin_status_value(request.form.get("status"))
    if customer_id <= 0 or requested_status is None:
        return "Invalid customer status", 400

    try:
        cursor.execute("SELECT id FROM users WHERE id = %s", (customer_id,))
        if cursor.fetchone() is None:
            return "Customer not found", 404

        cursor.execute(
            "UPDATE users SET is_active = %s WHERE id = %s",
            (requested_status == "active", customer_id),
        )
        db.commit()
    except Error:
        safe_db_rollback()
        flash("Customer status could not be updated.", "error")
        return redirect("/admin/customers")

    flash(
        "Customer reactivated." if requested_status == "active" else "Customer deactivated.",
        "success",
    )
    return redirect("/admin/customers")


@app.route("/admin/owners/<int:owner_id>/status", methods=["POST"])
@admin_required
def admin_owner_status(owner_id):
    requested_status = admin_status_value(request.form.get("status"))
    if owner_id <= 0 or requested_status is None:
        return "Invalid owner status", 400

    try:
        cursor.execute("SELECT id FROM owners WHERE id = %s", (owner_id,))
        if cursor.fetchone() is None:
            return "Owner not found", 404

        cursor.execute(
            "UPDATE owners SET is_active = %s WHERE id = %s",
            (requested_status == "active", owner_id),
        )
        db.commit()
    except Error:
        safe_db_rollback()
        flash("Owner status could not be updated.", "error")
        return redirect("/admin/owners")

    flash(
        "Owner reactivated." if requested_status == "active" else "Owner deactivated.",
        "success",
    )
    return redirect("/admin/owners")


@app.route("/admin/vehicles/<int:vehicle_id>/status", methods=["POST"])
@admin_required
def admin_vehicle_status(vehicle_id):
    requested_status = admin_status_value(request.form.get("status"))
    if vehicle_id <= 0 or requested_status is None:
        return "Invalid vehicle status", 400

    try:
        cursor.execute("SELECT id FROM vehicles WHERE id = %s", (vehicle_id,))
        if cursor.fetchone() is None:
            return "Vehicle not found", 404

        cursor.execute(
            "UPDATE vehicles SET is_active = %s WHERE id = %s",
            (requested_status == "active", vehicle_id),
        )
        db.commit()
    except Error:
        safe_db_rollback()
        flash("Vehicle status could not be updated.", "error")
        return redirect("/admin/vehicles")

    flash(
        "Vehicle activated." if requested_status == "active" else "Vehicle deactivated.",
        "success",
    )
    return redirect("/admin/vehicles")


@app.route("/admin/bookings/<int:booking_id>/status", methods=["POST"])
@admin_required
def admin_booking_status(booking_id):
    requested_status = (request.form.get("status") or "").strip().title()
    if booking_id <= 0 or requested_status not in {"Pending", "Accepted", "Rejected"}:
        return "Invalid booking status", 400

    try:
        cursor.execute("SELECT status FROM bookings WHERE id = %s", (booking_id,))
        booking = cursor.fetchone()
        if booking is None:
            return "Booking not found", 404

        current_status = (booking[0] or "").strip().title()
        if current_status != requested_status and current_status != "Pending":
            flash("Only pending bookings can change status.", "error")
            return redirect("/admin/bookings")

        cursor.execute(
            "UPDATE bookings SET status = %s WHERE id = %s",
            (requested_status, booking_id),
        )
        db.commit()
    except Error:
        safe_db_rollback()
        flash("Booking status could not be updated.", "error")
        return redirect("/admin/bookings")

    flash("Booking status updated.", "success")
    return redirect("/admin/bookings")


# =========================================================
# ADD VEHICLE
# =========================================================

@app.route("/addvehicle", methods=["GET", "POST"])
@login_required("owner")
def addvehicle():

    if request.method == "POST":

        vehicle_name = valid_text(request.form.get("vehicle_name"), 100)
        vehicle_type = valid_text(request.form.get("vehicle_type"), 100)
        contact_number = valid_phone(request.form.get("contact_number"))
        location = valid_text(request.form.get("location"), 255)

        if not vehicle_name or not vehicle_type or not contact_number or not location:

            return """
            <h2>Invalid Contact Number ❌</h2>

            <p>
            Please enter a valid 10-digit mobile number.
            </p>

            <a href="/addvehicle">
            Try Again
            </a>
            """

        owner_id = session["owner_id"]

        sql = """
        INSERT INTO vehicles
        (
            vehicle_name,
            vehicle_type,
            owner_id,
            contact_number,
            location
        )
        VALUES (%s, %s, %s, %s, %s)
        """

        values = (
            vehicle_name,
            vehicle_type,
            owner_id,
            contact_number,
            location
        )

        cursor.execute(
            sql,
            values
        )

        db.commit()

        return """
        <h2>Vehicle Added Successfully! ✅</h2>

        <p>
        Your vehicle has been added successfully.
        </p>

        <a href="/myvehicles">
        🚚 View My Vehicles
        </a>

        <br><br>

        <a href="/addvehicle">
        ➕ Add Another Vehicle
        </a>

        <br><br>

        <a href="/addroute">
        📍 Add Route and Rent
        </a>

        <br><br>

        <a href="/owner_dashboard">
        🏠 Owner Dashboard
        </a>
        """

    return render_template("add_vehicle.html")


# =========================================================
# MY VEHICLES
# =========================================================

@app.route("/myvehicles")
@login_required("owner")
def myvehicles():

    owner_id = session["owner_id"]

    sql = """
    SELECT
        id,
        vehicle_name,
        vehicle_type,
        contact_number,
        location
    FROM vehicles
    WHERE owner_id = %s
    ORDER BY id DESC
    """

    cursor.execute(
        sql,
        (owner_id,)
    )

    vehicles_data = cursor.fetchall()

    return render_template(
        "my_vehicles.html",
        vehicles=vehicles_data
    )


# =========================================================
# EDIT VEHICLE
# =========================================================

@app.route("/editvehicle/<vehicle_id>", methods=["GET", "POST"])
@login_required("owner")
def editvehicle(vehicle_id):

    if not vehicle_id.isdigit() or int(vehicle_id) <= 0:

        flash("Invalid vehicle ID.", "error")

        return redirect("/myvehicles")

    vehicle_id = int(vehicle_id)

    owner_id = session["owner_id"]

    sql = """
    SELECT
        id,
        vehicle_name,
        vehicle_type,
        contact_number,
        location
    FROM vehicles
    WHERE id = %s
    AND owner_id = %s
    """

    try:

        cursor.execute(
            sql,
            (vehicle_id, owner_id)
        )

    except Error:

        safe_db_rollback()
        flash("We could not load that vehicle. Please try again.", "error")

        return redirect("/myvehicles")

    vehicle = cursor.fetchone()

    if vehicle is None:

        flash("Vehicle not found or you are not allowed to edit it.", "error")

        return redirect("/myvehicles")

    if request.method == "POST":

        vehicle_name = valid_text(request.form.get("vehicle_name"), 100)
        vehicle_type = valid_text(request.form.get("vehicle_type"), 100)
        contact_number = valid_phone(request.form.get("contact_number"))
        location = valid_text(request.form.get("location"), 255)

        if not vehicle_name or not vehicle_type or not contact_number or not location:

            return """
            <h2>Invalid Contact Number ❌</h2>

            <a href="/myvehicles">
            Back to My Vehicles
            </a>
            """

        update_sql = """
        UPDATE vehicles
        SET
            vehicle_name = %s,
            vehicle_type = %s,
            contact_number = %s,
            location = %s
        WHERE id = %s
        AND owner_id = %s
        """

        values = (
            vehicle_name,
            vehicle_type,
            contact_number,
            location,
            vehicle_id,
            owner_id
        )

        try:

            cursor.execute(
                update_sql,
                values
            )

            db.commit()

        except Error:

            safe_db_rollback()
            flash("Vehicle changes could not be saved. Please try again.", "error")

            return redirect("/myvehicles")

        flash("Vehicle updated successfully.", "success")

        return redirect("/myvehicles")

    return render_template(
        "edit_vehicles.html",
        vehicle=vehicle
    )


# =========================================================
# DELETE VEHICLE
# =========================================================

@app.route("/deletevehicle/<vehicle_id>", methods=["POST"])
@login_required("owner")
def deletevehicle(vehicle_id):

    if not vehicle_id.isdigit() or int(vehicle_id) <= 0:

        flash("Invalid vehicle ID.", "error")

        return redirect("/myvehicles")

    vehicle_id = int(vehicle_id)

    owner_id = session["owner_id"]

    check_sql = """
    SELECT id
    FROM vehicles
    WHERE id = %s
    AND owner_id = %s
    """

    try:

        cursor.execute(
            check_sql,
            (vehicle_id, owner_id)
        )

    except Error:

        safe_db_rollback()
        flash("We could not verify that vehicle. Please try again.", "error")

        return redirect("/myvehicles")

    vehicle = cursor.fetchone()

    if vehicle is None:

        flash("Vehicle not found or you are not allowed to delete it.", "error")

        return redirect("/myvehicles")

    booking_sql = """
    SELECT id
    FROM bookings
    WHERE vehicle_id = %s
    LIMIT 1
    """

    try:

        cursor.execute(
            booking_sql,
            (vehicle_id,)
        )

    except Error:

        safe_db_rollback()
        flash("We could not check the vehicle bookings. Please try again.", "error")

        return redirect("/myvehicles")

    existing_booking = cursor.fetchone()

    if existing_booking:

        flash("This vehicle cannot be deleted because it has booking records.", "error")

        return redirect("/myvehicles")

    delete_routes = """
    DELETE FROM vehicle_routes
    WHERE vehicle_id = %s
    """

    delete_vehicle = """
    DELETE FROM vehicles
    WHERE id = %s
    AND owner_id = %s
    """

    try:

        cursor.execute(
            delete_routes,
            (vehicle_id,)
        )

        cursor.execute(
            delete_vehicle,
            (vehicle_id, owner_id)
        )

        db.commit()

    except Error:

        safe_db_rollback()
        flash("Vehicle could not be deleted. Please try again.", "error")

        return redirect("/myvehicles")

    flash("Vehicle deleted successfully.", "success")

    return redirect("/myvehicles")


# =========================================================
# ADD VEHICLE ROUTE AND RENT
# =========================================================

@app.route("/addroute", methods=["GET", "POST"])
@login_required("owner")
def addroute():
    owner_id = session["owner_id"]

    if request.method == "POST":

        vehicle_id = request.form.get("vehicle_id", "")
        from_location = valid_text(request.form.get("from_location"), 255)
        to_location = valid_text(request.form.get("to_location"), 255)
        rent = valid_rent(request.form.get("rent"))

        if not vehicle_id.isdigit() or not from_location or not to_location or rent is None:
            return "Invalid route or rent details", 400

        check_sql = """
        SELECT id
        FROM vehicles
        WHERE id = %s
        AND owner_id = %s
        """

        cursor.execute(
            check_sql,
            (vehicle_id, owner_id)
        )

        vehicle = cursor.fetchone()

        if vehicle is None:

            return """
            <h2>Access Denied ❌</h2>

            <p>
            You can only add routes to your own vehicles.
            </p>

            <a href="/addroute">
            Go Back
            </a>
            """

        sql = """
        INSERT INTO vehicle_routes
        (
            vehicle_id,
            from_location,
            to_location,
            rent
        )
        VALUES (%s, %s, %s, %s)
        """

        values = (
            vehicle_id,
            from_location,
            to_location,
            rent
        )

        cursor.execute(
            sql,
            values
        )

        db.commit()

        return """
        <h2>Route and Rent Added Successfully! ✅</h2>

        <a href="/addroute">
        Add Another Route
        </a>

        <br><br>

        <a href="/vehicles">
        View Vehicles
        </a>
        """

    cursor.execute(
        """
        SELECT
            id,
            vehicle_name,
            vehicle_type
        FROM vehicles
        WHERE owner_id = %s
        """,
        (owner_id,)
    )

    vehicles_data = cursor.fetchall()

    return render_template(
        "add_route.html",
        vehicles=vehicles_data
    )


# =========================================================
# VEHICLES PAGE
# =========================================================

def vehicle_query():
    return """
    SELECT
        v.id,
        v.vehicle_name,
        v.vehicle_type,
        v.owner_id,
        v.contact_number,
        v.location,
        vr.from_location,
        vr.to_location,
        vr.rent,
        vr.id AS route_id
    FROM vehicles v
    INNER JOIN owners o
        ON o.id = v.owner_id
    INNER JOIN vehicle_routes vr
        ON v.id = vr.vehicle_id
    """


def parse_vehicle_filters():
    search = valid_text(request.args.get("q"), 100)
    location = valid_text(request.args.get("location"), 255)
    vehicle_type = valid_text(request.args.get("vehicle_type"), 100)
    minimum_rent = request.args.get("min_rent", "").strip()
    maximum_rent = request.args.get("max_rent", "").strip()

    try:
        minimum_rent = Decimal(minimum_rent) if minimum_rent else None
        maximum_rent = Decimal(maximum_rent) if maximum_rent else None
    except (InvalidOperation, TypeError, ValueError):
        return None, "Rent filters must be valid numbers."

    if (
        (minimum_rent is not None and minimum_rent < 0)
        or (maximum_rent is not None and maximum_rent < 0)
        or (minimum_rent is not None and maximum_rent is not None and minimum_rent > maximum_rent)
    ):
        return None, "Rent filters are invalid."

    return {
        "search": search,
        "location": location,
        "vehicle_type": vehicle_type,
        "minimum_rent": minimum_rent,
        "maximum_rent": maximum_rent,
    }, None


def load_vehicle(vehicle_id=None, filters=None):
    sql = vehicle_query()
    values = []
    conditions = ["v.is_active = TRUE", "o.is_active = TRUE"]

    if vehicle_id is not None:
        conditions.append("v.id = %s")
        values.append(vehicle_id)

    if filters:
        if filters["search"]:
            conditions.append("(v.vehicle_name LIKE %s OR v.vehicle_type LIKE %s OR v.location LIKE %s OR vr.from_location LIKE %s OR vr.to_location LIKE %s)")
            search_value = f"%{filters['search']}%"
            values.extend([search_value] * 5)
        if filters["location"]:
            conditions.append("(v.location LIKE %s OR vr.from_location LIKE %s OR vr.to_location LIKE %s)")
            location_value = f"%{filters['location']}%"
            values.extend([location_value] * 3)
        if filters["vehicle_type"]:
            conditions.append("v.vehicle_type = %s")
            values.append(filters["vehicle_type"])
        if filters["minimum_rent"] is not None:
            conditions.append("vr.rent >= %s")
            values.append(filters["minimum_rent"])
        if filters["maximum_rent"] is not None:
            conditions.append("vr.rent <= %s")
            values.append(filters["maximum_rent"])

    if conditions:
        sql += " WHERE " + " AND ".join(conditions)

    sql += " ORDER BY v.id DESC"
    cursor.execute(sql, tuple(values))
    return cursor.fetchall()


@app.route("/vehicle/<int:vehicle_id>")
@login_required("customer")
def vehicle_details(vehicle_id):
    vehicles_data = load_vehicle(vehicle_id=vehicle_id)
    if not vehicles_data:
        return "Vehicle not found", 404
    return render_template("vehicle_details.html", vehicle=vehicles_data[0], routes=vehicles_data)


@app.route("/vehicles")
@login_required("customer")
def vehicles():
    filters, error = parse_vehicle_filters()

    if error:
        return error, 400

    vehicles_data = load_vehicle(filters=filters)

    return render_template(
        "vehicles.html",
        vehicles=vehicles_data,
        filters=filters,
    )


# =========================================================
# BOOK VEHICLE
# =========================================================

@app.route("/book/<int:vehicle_id>", methods=["GET", "POST"])
@login_required("customer")
def book_vehicle(vehicle_id):
    """Book one specific route for a vehicle.

    A vehicle can have multiple routes, so the route id is required to avoid
    accidentally booking the wrong rent/route combination. When no route is
    explicitly supplied, the first available route is used as the default.
    """
    route_id_raw = request.args.get("route_id") or request.form.get("route_id")

    base_sql = """
    SELECT
        v.id,
        v.vehicle_name,
        v.vehicle_type,
        v.owner_id,
        v.contact_number,
        v.location,
        vr.from_location,
        vr.to_location,
        vr.rent,
        vr.id AS route_id
    FROM vehicles v
    INNER JOIN owners o ON o.id = v.owner_id
    INNER JOIN vehicle_routes vr ON v.id = vr.vehicle_id
    WHERE v.id = %s AND v.is_active = TRUE AND o.is_active = TRUE
    """

    route_id = None

    if route_id_raw is not None and route_id_raw != "":
        if not route_id_raw.isdigit() or int(route_id_raw) <= 0:
            flash("Please select a valid route before booking.", "error")
            return redirect(f"/vehicle/{vehicle_id}")
        route_id = int(route_id_raw)
        sql = base_sql + " AND vr.id = %s ORDER BY vr.id"
        cursor.execute(sql, (vehicle_id, route_id))
        vehicle = cursor.fetchone()
    else:
        sql = base_sql + " ORDER BY vr.id LIMIT 1"
        cursor.execute(sql, (vehicle_id,))
        vehicle = cursor.fetchone()
        if vehicle is not None and len(vehicle) >= 10 and vehicle[9] is not None:
            route_id = int(vehicle[9])
        elif vehicle is not None:
            route_id = 1

    if vehicle is None:
        return "Vehicle route not found", 404

    if valid_rent(vehicle[8]) is None:
        return "Vehicle rent is invalid", 400

    if request.method == "POST":
        customer_name = session["user_name"]
        phone = session["user_phone"]
        booking_date = request.form.get("booking_date", "")

        try:
            requested_date = date.fromisoformat(booking_date)
        except (TypeError, ValueError):
            flash("Please select a valid booking date.", "error")
            return render_template("booking.html", vehicle=vehicle, today=date.today().isoformat()), 400

        if requested_date < date.today():
            flash("Booking date cannot be in the past.", "error")
            return render_template("booking.html", vehicle=vehicle, today=date.today().isoformat()), 400

        check_sql = """
        SELECT id FROM bookings
        WHERE vehicle_id = %s AND booking_date = %s
        AND LOWER(TRIM(status)) != 'rejected'
        """
        cursor.execute(check_sql, (vehicle_id, booking_date))
        if cursor.fetchone():
            flash("This vehicle is already booked for the selected date.", "error")
            return render_template("booking.html", vehicle=vehicle, today=date.today().isoformat()), 409

        insert_sql = """
        INSERT INTO bookings (vehicle_id, route_id, customer_name, phone, booking_date, status)
        VALUES (%s, %s, %s, %s, %s, %s)
        """
        try:
            cursor.execute(insert_sql, (vehicle_id, route_id, customer_name, phone, booking_date, "Pending"))
            db.commit()
            booking_id = cursor.lastrowid
        except Error:
            safe_db_rollback()
            flash("The booking could not be saved. Please try again.", "error")
            return render_template("booking.html", vehicle=vehicle), 503

        cursor.execute("""
            SELECT v.vehicle_name, v.vehicle_type, vr.from_location, vr.to_location,
                   vr.rent, b.customer_name, b.phone, b.booking_date, b.status
            FROM bookings b
            JOIN vehicles v ON b.vehicle_id = v.id
            JOIN vehicle_routes vr ON vr.id = %s AND vr.vehicle_id = v.id
            WHERE b.id = %s
        """, (route_id, booking_id))
        booking = cursor.fetchone()

        cursor.execute(
            "SELECT u.email, o.owner_name, o.email, COALESCE(v.contact_number, o.phone) "
            "FROM bookings b JOIN users u ON u.phone = b.phone "
            "JOIN vehicles v ON v.id = b.vehicle_id JOIN owners o ON o.id = v.owner_id "
            "WHERE b.id = %s", (booking_id,)
        )
        notification_contacts = cursor.fetchone() or (None, None, None, None)
        notification_booking = {
            "booking_id": booking_id,
            "customer_name": booking[5], "customer_phone": booking[6],
            "customer_email": notification_contacts[0],
            "vehicle_name": booking[0], "vehicle_type": booking[1],
            "owner_name": notification_contacts[1], "owner_email": notification_contacts[2],
            "owner_phone": notification_contacts[3], "booking_date": booking[7],
            "rent": booking[4], "status": booking[8], "location": vehicle[5],
            "from_location": booking[2], "to_location": booking[3],
        }
        try:
            notification_results = send_booking_notifications(
                notification_booking,
                include_customer=False,
            )
        except Exception:
            logger.exception("Booking email notification coordinator failed")
            notification_results = {"customer": False, "owner": False}

        if not is_mail_configured():
            email_status = "unconfigured"
        elif not notification_booking.get("customer_email"):
            email_status = "no_email"
        elif notification_results.get("customer"):
            email_status = "sent"
        else:
            email_status = "failed"

        return render_template(
            "booking_success.html",
            booking=(booking_id, booking[0], booking[1], booking[2], booking[3], booking[4],
                     booking[5], booking[6], booking[7], booking[8]),
            notification_results=notification_results,
            email_status=email_status,
            customer_email=notification_booking.get("customer_email"),
        )

    return render_template("booking.html", vehicle=vehicle, today=date.today().isoformat())


# =========================================================
# CUSTOMER BOOKING HISTORY
# =========================================================

@app.route("/mybookings")
@login_required("customer")
def mybookings():

    customer_phone = session["user_phone"]

    sql = """
    SELECT
        b.id,
        b.customer_name,
        b.phone,
        b.booking_date,
        b.status,
        v.vehicle_name,
        v.vehicle_type,
        vr.from_location,
        vr.to_location,
        vr.rent,
        r.rating,
        r.review,
        COALESCE(p.status, 'Pending') AS payment_status,
        p.amount AS payment_amount,
        p.payment_method,
        p.transaction_reference,
        p.paid_at
    FROM bookings b
    INNER JOIN vehicles v
        ON b.vehicle_id = v.id
    LEFT JOIN vehicle_routes vr
        ON b.route_id = vr.id
    LEFT JOIN ratings r
        ON r.booking_id = b.id
    LEFT JOIN payments p
        ON p.booking_id = b.id
    WHERE b.phone = %s
    ORDER BY b.id DESC
    """

    cursor.execute(
        sql,
        (customer_phone,)
    )

    bookings = cursor.fetchall()
    return render_template(
        "my_bookings.html",
        bookings=bookings
    )


# =========================================================
# CUSTOMER RATINGS
# =========================================================

@app.route("/rate/<int:booking_id>", methods=["GET", "POST"])
@login_required("customer")
def rate_booking(booking_id):
    phone = session["user_phone"]
    cursor.execute("""
        SELECT b.id, b.booking_date, b.status, v.vehicle_name, o.owner_name,
               r.rating, r.review
        FROM bookings b
        JOIN vehicles v ON v.id = b.vehicle_id
        JOIN owners o ON o.id = v.owner_id
        LEFT JOIN ratings r ON r.booking_id = b.id
        WHERE b.id = %s AND b.phone = %s
    """, (booking_id, phone))
    booking = cursor.fetchone()
    if not booking:
        return "Booking not found", 404
    if booking[2].strip().lower() != "accepted":
        flash("You can rate a booking after the owner accepts it.", "error")
        return redirect("/mybookings")

    if request.method == "POST":
        try:
            rating = int(request.form.get("rating", "0"))
        except ValueError:
            rating = 0
        review = valid_text(request.form.get("review"), 500) or ""
        if rating < 1 or rating > 5:
            flash("Please select a rating from 1 to 5 stars.", "error")
            return render_template("rate_booking.html", booking=booking), 400
        try:
            cursor.execute("""
                INSERT INTO ratings (booking_id, vehicle_id, customer_phone, owner_id, rating, review)
                SELECT b.id, b.vehicle_id, b.phone, v.owner_id, %s, %s
                FROM bookings b JOIN vehicles v ON v.id = b.vehicle_id
                WHERE b.id = %s AND b.phone = %s
                ON DUPLICATE KEY UPDATE rating=VALUES(rating), review=VALUES(review), updated_at=CURRENT_TIMESTAMP
            """, (rating, review, booking_id, phone))
            db.commit()
            flash("Thank you! Your rating has been saved.", "success")
        except Error:
            safe_db_rollback()
            flash("Your rating could not be saved. Please try again.", "error")
        return redirect("/mybookings")

    return render_template("rate_booking.html", booking=booking)


# =========================================================
# DEMO PAYMENT FLOW
# =========================================================

@app.route("/booking/<int:booking_id>/pay", methods=["GET", "POST"])
@login_required("customer")
def pay_booking(booking_id):
    customer_phone = session.get("user_phone")
    cursor.execute(
        """
        SELECT
            b.id,
            b.customer_name,
            b.phone,
            b.booking_date,
            b.status,
            v.vehicle_name,
            v.vehicle_type,
            vr.from_location,
            vr.to_location,
            vr.rent,
            p.id AS payment_id,
            COALESCE(p.status, 'Pending') AS payment_status,
            p.amount AS payment_amount,
            p.payment_method,
            p.transaction_reference,
            p.paid_at
        FROM bookings b
        INNER JOIN vehicles v ON v.id = b.vehicle_id
        LEFT JOIN vehicle_routes vr ON vr.id = b.route_id
        LEFT JOIN payments p ON p.booking_id = b.id
        WHERE b.id = %s AND b.phone = %s
        """,
        (booking_id, customer_phone),
    )
    booking = cursor.fetchone()
    if not booking:
        return "Booking not found", 404

    booking_status = (booking[4] or "").strip().title()
    payment_status = (booking[11] or "Pending").strip().title()

    if booking_status != "Accepted":
        if booking_status == "Pending":
            flash("This booking is still awaiting owner confirmation.", "info")
        elif booking_status == "Rejected":
            flash("This booking was rejected and cannot be paid.", "info")
        else:
            flash("This booking cannot be paid right now.", "info")
        return redirect("/mybookings")

    if payment_status == "Paid":
        return redirect(f"/booking/{booking_id}/payment-success")

    amount = booking[9]
    if amount is None or valid_rent(amount) is None:
        return "Booking amount is invalid", 400

    if request.method == "POST":
        payment_method = (request.form.get("payment_method") or "").strip()
        if payment_method not in {"Demo Card", "Demo UPI"}:
            flash("Please choose a valid demo payment method.", "error")
            return render_template(
                "payment_checkout.html",
                booking=booking,
                amount=amount,
                payment_method=payment_method,
            ), 400

        if payment_method == "Demo Card":
            cardholder_name = (request.form.get("cardholder_name") or "").strip()
            if not cardholder_name:
                flash("Please enter the cardholder name for the demo card.", "error")
                return render_template(
                    "payment_checkout.html",
                    booking=booking,
                    amount=amount,
                    payment_method=payment_method,
                ), 400
            payment_method_label = payment_method
        else:
            demo_upi_id = (request.form.get("demo_upi_id") or "").strip()
            if not demo_upi_id:
                flash("Please enter the demo UPI ID for payment.", "error")
                return render_template(
                    "payment_checkout.html",
                    booking=booking,
                    amount=amount,
                    payment_method=payment_method,
                ), 400
            payment_method_label = payment_method

        transaction_reference = "DEMO-" + secrets.token_hex(4).upper()
        try:
            cursor.execute(
                "SELECT id FROM payments WHERE booking_id = %s FOR UPDATE",
                (booking_id,),
            )
            if cursor.fetchone() is not None:
                return redirect(f"/booking/{booking_id}/payment-success")

            cursor.execute(
                """
                INSERT INTO payments (
                    booking_id,
                    user_id,
                    amount,
                    currency,
                    payment_method,
                    transaction_reference,
                    status,
                    paid_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                """,
                (
                    booking_id,
                    session["user_id"],
                    Decimal(str(amount)),
                    "INR",
                    payment_method_label,
                    transaction_reference,
                    "Paid",
                ),
            )
            db.commit()
        except Error:
            safe_db_rollback()
            logger.exception("Failed to create demo payment for booking %s", booking_id)
            flash("Demo payment processing failed. Please try again.", "error")
            return redirect("/mybookings")

        payment_email = {
            "booking_id": booking_id,
            "customer_name": booking[1],
            "customer_email": session.get("user_email"),
            "vehicle_name": booking[5],
            "vehicle_type": booking[6],
            "route": f"{booking[7] or 'Not specified'} → {booking[8] or 'Not specified'}",
            "amount": Decimal(str(amount)),
            "payment_method": payment_method_label,
            "transaction_reference": transaction_reference,
            "status": "Paid",
            "booking_date": booking[3],
        }
        email_result = False
        try:
            email_result = send_demo_payment_confirmation_email(payment_email)
        except Exception:
            logger.exception("Demo payment confirmation email failed for booking %s", booking_id)

        if email_result:
            flash("Demo payment completed successfully. A confirmation email has been sent.", "success")
        else:
            flash("Demo payment completed successfully. The confirmation email could not be sent, but payment is recorded.", "warning")
        return redirect(f"/booking/{booking_id}/payment-success")

    return render_template(
        "payment_checkout.html",
        booking=booking,
        amount=amount,
        payment_method="Demo Card",
    )


@app.route("/booking/<int:booking_id>/payment-success")
@login_required("customer")
def booking_payment_success(booking_id):
    customer_phone = session.get("user_phone")
    cursor.execute(
        """
        SELECT
            b.id,
            b.customer_name,
            b.phone,
            b.booking_date,
            b.status,
            v.vehicle_name,
            v.vehicle_type,
            vr.from_location,
            vr.to_location,
            vr.rent,
            p.amount,
            p.payment_method,
            p.transaction_reference,
            p.status,
            p.paid_at
        FROM bookings b
        INNER JOIN vehicles v ON v.id = b.vehicle_id
        LEFT JOIN vehicle_routes vr ON vr.id = b.route_id
        LEFT JOIN payments p ON p.booking_id = b.id
        WHERE b.id = %s AND b.phone = %s
        """,
        (booking_id, customer_phone),
    )
    booking = cursor.fetchone()
    if not booking:
        return "Booking not found", 404

    payment_status = (booking[13] or "").strip().title()
    if payment_status != "Paid":
        flash("No successful demo payment has been recorded for this booking yet.", "info")
        return redirect("/mybookings")

    return render_template("payment_success.html", booking=booking)


@app.route("/booking/<int:booking_id>/receipt")
@login_required("customer")
def booking_receipt(booking_id):
    customer_phone = session.get("user_phone")
    cursor.execute(
        """
        SELECT
            b.id,
            b.customer_name,
            b.phone,
            b.booking_date,
            b.status,
            v.vehicle_name,
            v.vehicle_type,
            vr.from_location,
            vr.to_location,
            vr.rent,
            p.amount,
            p.payment_method,
            p.transaction_reference,
            p.status,
            p.paid_at
        FROM bookings b
        INNER JOIN vehicles v ON v.id = b.vehicle_id
        LEFT JOIN vehicle_routes vr ON vr.id = b.route_id
        LEFT JOIN payments p ON p.booking_id = b.id
        WHERE b.id = %s AND b.phone = %s
        """,
        (booking_id, customer_phone),
    )
    booking = cursor.fetchone()
    if not booking:
        return "Booking not found", 404

    if (booking[13] or "").strip().title() != "Paid":
        return "No payment receipt is available for this booking.", 404

    html = render_template("payment_success.html", booking=booking)
    if request.args.get("download") == "1":
        response = app.make_response(html)
        response.headers["Content-Disposition"] = f"attachment; filename=demo_payment_receipt_booking_{booking_id}.html"
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        return response
    return html


# =========================================================
# OWNER DASHBOARD
# =========================================================

@app.route("/owner_dashboard")
@login_required("owner")
def owner_dashboard():
    owner_id = session["owner_id"]


    # =====================================================
    # GET CUSTOMER BOOKINGS
    # =====================================================

    sql = """
    SELECT
        b.id,
        b.customer_name,
        b.phone,
        u.email,
        b.booking_date,
        b.status,
        v.vehicle_name,
        v.vehicle_type,
        vr.from_location,
        vr.to_location,
        vr.rent,
        v.id,
        v.owner_id,
        COALESCE(p.status, 'Pending') AS payment_status,
        p.amount AS payment_amount,
        p.payment_method,
        p.transaction_reference

    FROM bookings b

    INNER JOIN vehicles v
        ON b.vehicle_id = v.id

    LEFT JOIN users u
        ON u.phone = b.phone

    LEFT JOIN vehicle_routes vr
        ON b.route_id = vr.id

    LEFT JOIN payments p
        ON p.booking_id = b.id

    WHERE v.owner_id = %s

    ORDER BY b.id DESC
    """

    cursor.execute(
        sql,
        (owner_id,)
    )

    bookings = cursor.fetchall()

    logger.warning(
        "Owner dashboard bookings: logged_in_owner_id=%s booking_count=%s "
        "booking_ids=%s vehicle_ids=%s vehicle_owner_ids=%s",
        owner_id,
        len(bookings),
        [booking[0] for booking in bookings],
        [booking[-2] for booking in bookings],
        [booking[-1] for booking in bookings],
    )


    # =====================================================
    # TOTAL BOOKINGS
    # =====================================================

    total_sql = """
    SELECT COUNT(*)
    FROM bookings b

    INNER JOIN vehicles v
        ON b.vehicle_id = v.id

    WHERE v.owner_id = %s
    """

    cursor.execute(
        total_sql,
        (owner_id,)
    )

    total_bookings = cursor.fetchone()[0]


    # =====================================================
    # PENDING BOOKINGS
    # =====================================================

    pending_sql = """
    SELECT COUNT(*)
    FROM bookings b

    INNER JOIN vehicles v
        ON b.vehicle_id = v.id

    WHERE v.owner_id = %s
    AND LOWER(TRIM(b.status)) = 'pending'
    """

    cursor.execute(
        pending_sql,
        (owner_id,)
    )

    pending_bookings = cursor.fetchone()[0]


    # =====================================================
    # ACCEPTED BOOKINGS
    # =====================================================

    accepted_sql = """
    SELECT COUNT(*)
    FROM bookings b

    INNER JOIN vehicles v
        ON b.vehicle_id = v.id

    WHERE v.owner_id = %s
    AND LOWER(TRIM(b.status)) = 'accepted'
    """

    cursor.execute(
        accepted_sql,
        (owner_id,)
    )

    accepted_bookings = cursor.fetchone()[0]


    # =====================================================
    # REJECTED BOOKINGS
    # =====================================================

    rejected_sql = """
    SELECT COUNT(*)
    FROM bookings b

    INNER JOIN vehicles v
        ON b.vehicle_id = v.id

    WHERE v.owner_id = %s
    AND LOWER(TRIM(b.status)) = 'rejected'
    """

    cursor.execute(
        rejected_sql,
        (owner_id,)
    )

    rejected_bookings = cursor.fetchone()[0]

    vehicle_count_sql = """
    SELECT COUNT(*)
    FROM vehicles
    WHERE owner_id = %s
    """

    cursor.execute(vehicle_count_sql, (owner_id,))
    vehicle_count = cursor.fetchone()[0]

    active_vehicle_sql = """
    SELECT COUNT(*)
    FROM vehicles v
    WHERE v.owner_id = %s
    AND EXISTS (
        SELECT 1
        FROM vehicle_routes vr
        WHERE vr.vehicle_id = v.id
    )
    """

    cursor.execute(active_vehicle_sql, (owner_id,))
    active_vehicles = cursor.fetchone()[0]


    # =====================================================
    # SEND DATA TO OWNER DASHBOARD
    # =====================================================

    return render_template(
        "owner_dashboard.html",
        bookings=bookings,
        total_bookings=total_bookings,
        pending_bookings=pending_bookings,
        accepted_bookings=accepted_bookings,
        rejected_bookings=rejected_bookings,
        vehicle_count=vehicle_count,
        active_vehicles=active_vehicles,
    )


# =========================================================
# UPDATE BOOKING STATUS
# =========================================================

@app.route("/update_booking/<int:booking_id>/<status>", methods=["POST"])
@login_required("owner")
def update_booking(booking_id, status):
    # Normalize the URL value so "accepted"/"Accepted" behave consistently.
    status = (status or "").strip().lower()
    status_map = {"accepted": "Accepted", "rejected": "Rejected"}
    status = status_map.get(status)
    if status is None:
        return "Invalid booking status", 400


    owner_id = session["owner_id"]


    update_sql = """
    UPDATE bookings b
    INNER JOIN vehicles v ON v.id = b.vehicle_id
    SET b.status = %s
    WHERE b.id = %s
      AND v.owner_id = %s
      AND LOWER(TRIM(b.status)) = 'pending'
    """
    try:
        cursor.execute(update_sql, (status, booking_id, owner_id))
        if getattr(cursor, "rowcount", 0) != 1:
            safe_db_rollback()
            flash("This booking was not found, is not yours, or is already finalized.", "error")
            return redirect("/owner_dashboard")

        cursor.execute("""
            SELECT b.id, b.customer_name, b.phone, b.booking_date, b.status,
                   v.vehicle_name, v.vehicle_type, v.location,
                   vr.from_location, vr.to_location, vr.rent,
                   u.email, o.owner_name, o.email, COALESCE(v.contact_number, o.phone)
            FROM bookings b
            INNER JOIN vehicles v ON v.id = b.vehicle_id
            INNER JOIN owners o ON o.id = v.owner_id
            LEFT JOIN users u ON u.phone = b.phone
            LEFT JOIN vehicle_routes vr ON vr.id = b.route_id
            WHERE b.id = %s AND v.owner_id = %s
        """, (booking_id, owner_id))
        booking_row = cursor.fetchone()
        db.commit()
    except Error:
        safe_db_rollback()
        logger.exception("Could not update booking status for booking %s", booking_id)
        flash("Booking status could not be updated. Please try again.", "error")
        return redirect("/owner_dashboard")

    notification_booking = {
        "booking_id": booking_row[0], "customer_name": booking_row[1],
        "customer_phone": booking_row[2], "booking_date": booking_row[3],
        "status": booking_row[4], "vehicle_name": booking_row[5],
        "vehicle_type": booking_row[6], "location": booking_row[7],
        "from_location": booking_row[8] or "Not specified",
        "to_location": booking_row[9] or "Not specified", "rent": booking_row[10],
        "customer_email": booking_row[11], "owner_name": booking_row[12],
        "owner_email": booking_row[13], "owner_phone": booking_row[14],
    }
    email_sent = False
    email_enabled = os.getenv("MAIL_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }
    logger.warning(
        "Attempting decision email for booking %s: customer email present=%s",
        booking_id,
        bool(notification_booking.get("customer_email")),
    )
    if notification_booking.get("customer_email"):
        try:
            email_sent = send_booking_decision_email(notification_booking)
        except Exception:
            logger.exception("Booking decision email failed for booking %s", booking_id)
    else:
        logger.warning("Customer email is missing for booking %s", booking_id)
    logger.warning(
        "Decision email result for booking %s: email result=%s",
        booking_id,
        email_sent,
    )
    if email_enabled and not email_sent:
        flash("Booking updated, but the customer email could not be delivered.", "warning")
    else:
        flash(f"Booking {status.lower()} successfully.", "success")
    return redirect("/owner_dashboard")


# =========================================================
# OWNER LOGOUT
# =========================================================

@app.route("/owner_logout", methods=["POST"])
@login_required("owner")
def owner_logout():

    session.clear()

    return redirect("/")


# =========================================================
# FARMER AI CROP DISEASE ASSISTANT
# =========================================================

@app.route("/farmer-ai")
def farmer_ai():
    return render_template(
        "farmer_ai.html",
        is_configured=is_ai_configured(),
        previous_analysis=session.get("leaf_analysis"),
        chat_history=session.get("farmer_chat_history", []),
    )


@app.route("/farmer-ai/analyze", methods=["POST"])
def farmer_ai_analyze():
    if "leaf_image" not in request.files:
        return jsonify({"success": False, "error": "No image file provided."}), 400

    file = request.files["leaf_image"]
    is_valid, err_msg, image_bytes, mime_type = validate_image_file(file)
    if not is_valid:
        return jsonify({"success": False, "error": err_msg}), 400

    language = session.get("language", "en")
    result = analyze_crop_leaf(image_bytes, mime_type, language=language)

    if result.get("success"):
        session["leaf_analysis"] = result
        session["farmer_chat_history"] = []

    return jsonify(result)


@app.route("/farmer-ai/chat", methods=["POST"])
def farmer_ai_chat():
    data = request.get_json(silent=True) or request.form
    user_message = (data.get("message") or "").strip()
    if not user_message:
        return jsonify({"success": False, "error": "Please enter a question."}), 400

    language = session.get("language", "en")
    result = chat_about_crop(
        user_message,
        previous_analysis=session.get("leaf_analysis"),
        chat_history=session.get("farmer_chat_history", []),
        language=language,
    )
    if result.get("success"):
        history = session.get("farmer_chat_history", [])
        history.extend([
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": result.get("reply", "")},
        ])
        session["farmer_chat_history"] = history[-12:]
    return jsonify(result)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=app.config["DEBUG"]
    )