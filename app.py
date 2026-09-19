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
    # Maintain global language in session with cookie fallback
    if "language" not in session:
        cookie_lang = request.cookies.get("language", "").strip().lower()
        if cookie_lang in SUPPORTED_LANGUAGES:
            session["language"] = cookie_lang
        else:
            session["language"] = "en"

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
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if role == "customer":
                if "user_id" not in session:
                    if "owner_id" in session:
                        return redirect("/owner_dashboard")
                    return redirect("/login")
            elif role == "owner":
                if "owner_id" not in session:
                    if "user_id" in session:
                        return redirect("/vehicles")
                    return redirect("/ownerlogin")
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
# LANGUAGE SUPPORT & LOCALIZATION
# =========================================================

SUPPORTED_LANGUAGES = ("en", "te", "ta", "hi")
LANGUAGE_NAMES = {
    "en": "English",
    "te": "తెలుగు",
    "ta": "தமிழ்",
    "hi": "हिन्दी",
}

TRANSLATIONS = {   'en': {
        'admin_login_badge': 'ADMIN LOGIN',
        'username': 'Username',
        'accepted_bookings_desc': 'Confirmed rides',
        'action': 'Action',
        'action_col': 'Action',
        'active_vehicles_desc': 'Ready with active routes',
        'ai_assisted_result_sub': 'AI-assisted result based on the uploaded image.',
        'ai_chat_welcome': 'Hello! Upload a leaf image above, then ask me questions about the result.',
        'ai_disclaimer_note': 'Your image is analyzed only to provide the requested AI assistance. Do not use the result as a guaranteed diagnosis.',
        'ai_disclaimer_short': 'AI results are informational and should be verified with agricultural professionals.',
        'ai_service_not_configured': 'AI service is not configured yet. Add AI_API_KEY and provider settings to your .env file, then restart Flask.',
        'amount': 'Amount',
        'ask_ai_sub': 'Ask about the analyzed leaf, symptoms, prevention or general crop care. The current analysis is kept in this session for follow-up questions.',
        'ask_followup_questions': 'Ask follow-up questions',
        'booking_id_col': 'Booking ID',
        'chip_disease_label': 'What disease is this?',
        'chip_disease_query': 'What disease could this leaf have?',
        'chip_lang_label': 'Explain in my language',
        'chip_lang_query': 'Explain this clearly in simple terms.',
        'chip_prevent_label': 'How can I prevent it?',
        'chip_prevent_query': 'How can I prevent this problem from spreading?',
        'chip_steps_label': 'What should I do now?',
        'chip_steps_query': 'What should I do now to manage this crop issue?',
        'choose_or_drag_photo': 'Choose or drag a leaf photo here',
        'contact_expert': 'When to Contact an Expert',
        'demo_card': 'Demo Card',
        'demo_simulation_notice': 'Simulation only: this demo payment does not process any real card, bank, or UPI data.',
        'demo_upi': 'Demo UPI',
        'demo_upi_id': 'Demo UPI ID',
        'enter_cardholder_name': 'Enter demo cardholder name',
        'enter_demo_upi_id': 'demo@upi',
        'farmer_ai_desc': 'Upload a clear crop-leaf photo and get an AI-assisted explanation of possible disease, visible symptoms, safe management steps and prevention guidance.',
        'file_format_limit': 'JPG, JPEG, PNG or WEBP • Maximum 10 MB',
        'fleet_management': 'Fleet Management',
        'fleet_overview': 'Fleet Overview',
        'id': 'ID',
        'image_based_analysis': 'Image-based analysis',
        'not_specified': 'Not specified',
        'owner_hero_subtitle': 'Manage your vehicles, routes, and respond to customer booking requests.',
        'owner_tagline': 'For Farmers • For Businesses • At Your Service',
        'pay': 'Pay',
        'payment_col': 'Payment',
        'payment_summary': 'Payment Summary',
        'pending_actions_desc': 'Requires confirmation',
        'pending_review_banner': 'booking request(s) need your immediate review and action.',
        'pending_review_hint': 'Review the customer, route, date, and fixed rent below, then click Accept or Reject.',
        'processing': 'Processing...',
        'processing_demo_payment': 'Processing demo payment...',
        'profile': 'Owner Profile',
        'ready_when_you_are': 'Ready when you are',
        'route_col': 'Route',
        'route_pricing': 'Route Based Pricing',
        'route_pricing_desc': 'Transparent and fair rates',
        'secure_desc': 'Your trust is our priority',
        'secure_reliable': 'Secure & Reliable',
        'sidebar_dashboard': 'Dashboard',
        'start_moving': 'Start moving your next load.',
        'support_247': '24/7 Support',
        'support_desc': 'We\'re always here to help',
        'symptoms_explanation': 'Symptoms & Visual Explanation',
        'total_bookings_desc': 'All-time bookings',
        'total_due': 'Total Due',
        'total_vehicles_desc': 'Registered fleet',
        'type_question_placeholder': 'Type your farming question here...',
        'upload_another_image': 'Upload another image',
        'upload_leaf_tip': 'For best results, photograph one leaf in good daylight with the affected area clearly visible.',
        'vehicle': 'Vehicle',
        'vehicle_col': 'Vehicle',
        'view_all_bookings': 'View Fleet',
        'wide_range': 'Wide Vehicle Range',
        'wide_range_desc': 'From small to heavy vehicles',   'language': 'Language',
              'english': 'English',
              'telugu': 'Telugu',
              'tamil': 'Tamil',
              'hindi': 'Hindi',
              'vehicle_booking': 'Business Vehicle Booking',
              'tagline': 'For Farmers • For Businesses • At Your Service',
              'home': 'Home',
              'vehicles': 'Vehicles',
              'available_vehicles': 'Available Vehicles',
              'my_bookings': 'My Bookings',
              'dashboard': 'Dashboard',
              'owner_dashboard': 'Owner Dashboard',
              'admin_dashboard': 'Admin Dashboard',
              'control_center': 'Control Center',
              'security_clearance': 'Security Clearance',
              'farmer_ai': 'Farmer AI',
              'farmer_ai_assistant': 'Farmer AI Crop Disease Assistant',
              'customer': 'Customer',
              'customer_login': 'Customer Login',
              'owner_login': 'Vehicle Owner Login',
              'admin_login': 'Admin Login',
              'book_vehicle': 'Book a Vehicle',
              'login': 'Login',
              'register': 'Register',
              'customer_registration': 'Customer Registration',
              'owner_registration': 'Vehicle Owner Registration',
              'logout': 'Logout',
              'welcome': 'Welcome',
              'back_home': 'Back to Home',
              'back_to_vehicles': 'Back to Vehicles',
              'back_to_dashboard': 'Back to Dashboard',
              'back_to_public': 'Back to Public Site',
              'mobile_number': 'Mobile Number',
              'customer_name': 'Customer Name',
              'owner_name': 'Owner Name',
              'registered_email': 'Registered Email',
              'email_address': 'Email Address',
              'email': 'Email',
              'phone': 'Phone',
              'phone_number': 'Phone Number',
              'password': 'Password',
              'confirm_password': 'Confirm Password',
              'forgot_password': 'Forgot Password?',
              'reset_password': 'Reset Password',
              'set_new_password': 'Set a New Password',
              'new_password': 'New Password',
              'send_reset_link': 'Send Reset Link',
              'back_to_customer_login': 'Back to Customer Login',
              'enter_registered_email': 'Enter your registered email address.',
              'login_mobile': 'Login using your mobile number.',
              'create_account': 'Create Your Account',
              'choose_account_type': 'Choose how you want to use Business Vehicle Booking',
              'for_customers': 'For Farmers & Business Customers',
              'for_owners': 'For Commercial Vehicle Owners',
              'customer_desc': 'Find and book reliable commercial vehicles for your transportation needs.',
              'owner_desc': 'Register your commercial vehicles and receive booking requests from customers.',
              'register_as_customer': 'Register as Customer',
              'customer_sign_in': 'Customer Sign In',
              'register_as_owner': 'Register as Vehicle Owner',
              'owner_sign_in': 'Vehicle Owner Sign In',
              'new_vehicle_owner': 'New vehicle owner?',
              'already_an_owner': 'Already an owner?',
              'already_have_account': 'Already have an account?',
              'new_customer': 'New customer?',
              'sign_in_here': 'Sign in here',
              'register_here': 'Register here',
              'enter_owner_name_placeholder': 'Enter owner or business name',
              'enter_phone_placeholder': 'Enter 10-digit mobile number',
              'enter_owner_email_placeholder': 'owner@example.com (used for login & alerts)',
              'enter_password_placeholder': 'Enter your password (min 8 characters)',
              'enter_admin_username': 'Enter admin username',
              'enter_admin_password': 'Enter admin password',
              'admin_login_desc': 'Sign in to access platform management and real-time operations.',
              'admin_copyright': '© 2026 Business Vehicle Booking • Administrative Systems',
              'heavy_vehicles_title': 'Heavy Vehicles for a Stronger Tomorrow',
              'book_vehicles_ease': 'Book Commercial Vehicles with Ease',
              'hero_subtitle': 'Connecting farmers and business owners with reliable commercial vehicles for a better '
                               'tomorrow.',
              'explore_vehicles': 'Explore Vehicles',
              'try_farmer_ai': 'Try Farmer AI',
              'smart_farming_support': 'Smart farming support',
              'how_it_works': 'How It Works',
              'choose_vehicle': 'Choose Vehicle',
              'select_date': 'Select Date',
              'confirm': 'Confirm',
              'vehicle_types': 'Vehicle Types',
              'tractor': 'Tractor',
              'mini_truck': 'Mini Truck',
              'pickup': 'Pickup',
              'lorry': 'Lorry',
              'goods_auto': 'Goods Auto',
              'tractor_desc': 'Suitable for agricultural and goods transportation.',
              'mini_truck_desc': 'Suitable for small and medium loads.',
              'pickup_desc': 'Convenient for local transportation.',
              'lorry_desc': 'Suitable for transporting larger loads.',
              'goods_auto_desc': 'Ideal for fast local goods and produce transit.',
              'simple_fast_easy': 'Simple • Fast • Easy',
              'transportation_message': 'Book the right vehicle for your transportation needs.',
              'footer_message': 'Simple transportation service for everyone.',
              'vehicle_type': 'Vehicle Type',
              'location': 'Location',
              'from': 'From',
              'to': 'To',
              'rent': 'Rent',
              'contact': 'Contact',
              'book_now': 'Book Now',
              'no_vehicles': 'No Vehicles Available',
              'no_vehicles_message': 'There are currently no vehicles available for booking.',
              'search_placeholder': 'Search name, route, or location',
              'location_placeholder': 'Location or town',
              'all_vehicle_types': 'All Vehicle Types',
              'min_rent': 'Min Rent (₹)',
              'max_rent': 'Max Rent (₹)',
              'apply_filters': 'Apply Filters',
              'reset_filters': 'Reset Filters',
              'view_details': 'View Details',
              'book_this_vehicle': 'Book This Vehicle',
              'available_routes_rent': 'Available Routes & Fixed Rent',
              'base_location': 'Base Location',
              'current_location_label': 'Current / Base Location',
              'capacity_weight': 'Capacity / Load Capacity',
              'availability_status': 'Availability',
              'available_ready': 'Available (Ready for booking)',
              'route_label': 'Route',
              'fixed_route_rent_label': 'Fixed Route Rent',
              'booking_date': 'Booking Date',
              'confirm_booking': 'Confirm Booking',
              'status': 'Status',
              'accepted': 'Accepted',
              'rejected': 'Rejected',
              'pending': 'Pending',
              'paid': 'Paid',
              'pay_now': 'Pay Now',
              'no_bookings': 'No Bookings',
              'no_bookings_message': 'You have not made any vehicle bookings yet.',
              'booking_id': 'Booking ID',
              'customer_booking_requests': 'Customer Booking Requests',
              'accept': 'Accept',
              'reject': 'Reject',
              'no_action_needed': 'No action required',
              'rate_service': 'Rate Service',
              'rate_booking': 'Rate Your Booking',
              'rate_booking_title': '⭐ Rate Your Booking',
              'review_optional': 'Review (optional)',
              'review_placeholder': 'How was the vehicle and owner service?',
              'save_rating': 'Save Rating',
              'rating': 'Rating',
              'booking_success_title': 'Booking Request Submitted',
              'booking_success_desc': 'Your booking is pending owner confirmation.',
              'waiting_owner_confirm': 'Waiting for owner confirmation.',
              'view_my_bookings': 'View My Bookings',
              'browse_vehicles': 'Browse Vehicles',
              'track_bookings_desc': 'Track every route booking and rate accepted services.',
              'booking_confirmed_badge': 'Booking Confirmed',
              'add_vehicle': 'Add Vehicle',
              'add_vehicle_title': '🚚 Add Vehicle',
              'add_route': 'Add Route',
              'add_route_rent': '+ Add Route & Rent',
              'add_route_subtitle': 'Set fixed route pricing for your commercial vehicles so customers can book '
                                    'instantly.',
              'route_details': 'Route Details',
              'rent_details': 'Rent Details',
              'additional_info': 'Additional Information',
              'select_vehicle': 'Select Vehicle',
              'choose_vehicle_prompt': '-- Select Your Vehicle --',
              'from_location': 'Origin / From Location',
              'to_location': 'Destination / To Location',
              'fixed_route_rent': 'Route-Based Fixed Rent (₹)',
              'save_route_rent': 'Save Route & Rent',
              'save_vehicle': 'Save Vehicle',
              'save_changes': 'Save Changes',
              'cancel': 'Cancel',
              'route_added_success': 'Route and rent added successfully! ✅',
              'add_another_route': 'Add Another Route',
              'my_vehicles': 'My Vehicles',
              'edit_vehicle': 'Edit Vehicle',
              'edit_vehicle_title': '✏️ Edit Vehicle',
              'vehicle_name': 'Vehicle Name',
              'contact_number': 'Contact Number',
              'update_vehicle': 'Update Vehicle',
              'delete_vehicle': 'Delete Vehicle',
              'active_vehicles': 'Active Vehicles',
              'total_bookings': 'Total Bookings',
              'total_vehicles': 'Total Vehicles',
              'pending_action': 'Pending Action',
              'accepted_bookings': 'Accepted Bookings',
              'quick_actions_title': 'Quick Management',
              'manage_fleet': 'Manage Fleet',
              'manage_fleet_desc': 'View, edit, or delete existing vehicles',
              'manage_routes_desc': 'Configure fixed route pricing for customers',
              'owner_profile_title': 'Owner Profile',
              'account_status': 'Account Status',
              'active_fleet': 'Active Fleet',
              'no_bookings_title': 'No booking requests yet',
              'no_bookings_desc': 'Customer bookings for your vehicles will appear here in real time.',
              'choose_vehicle_fleet_hint': 'Choose the vehicle from your active registered fleet.',
              'starting_point_hint': 'Starting town, village or loading point.',
              'delivery_point_hint': 'Delivery market, warehouse or destination town.',
              'total_fixed_rent_hint': 'Total fixed rent for this one-way route trip.',
              'route_open_notice': 'Vehicle route will immediately be open for customer bookings.',
              'transparent_pricing_title': 'Transparent Route-Based Pricing',
              'source_placeholder': 'Example: Tadipatri',
              'destination_placeholder': 'Example: Anantapur',
              'rent_placeholder': 'Example: 2500',
              'vehicle_name_example': 'Example: Tata Ace',
              'location_example': 'Example: Kanchipuram',
              'demo_payment': 'Demo Payment',
              'demo_payment_banner': 'DEMO PAYMENT — NO REAL MONEY',
              'secure_demo_payment': 'Secure Demo Payment',
              'demo_payment_disclaimer': 'This is a project demo transaction only. No real money is charged.',
              'amount_paid': 'Amount Paid',
              'payment_method': 'Payment Method',
              'cardholder_name': 'Cardholder Name',
              'upi_id': 'UPI ID',
              'enter_cardholder_placeholder': 'Enter demo cardholder name',
              'enter_upi_placeholder': 'demo@upi',
              'pay_amount_btn': 'Pay Amount',
              'payment_successful': 'Payment Successful',
              'payment_success_desc': 'This demo payment was completed successfully and has been recorded in the '
                                      'system.',
              'transaction_reference': 'Transaction Reference',
              'date': 'Date',
              'view_receipt': 'View Receipt',
              'download_receipt': 'Download Receipt',
              'view_booking': 'View Booking',
              'upload_leaf_image': 'Upload Leaf Image',
              'analyze_leaf': 'Analyze Leaf',
              'drag_leaf_image': 'Choose or drag a leaf photo here',
              'image_format_limit': 'JPG, JPEG, PNG or WEBP • Maximum 10 MB',
              'ai_assistant_intro': 'Upload a clear crop-leaf photo and get an AI-assisted explanation of possible '
                                    'disease, visible symptoms, safe management steps and prevention guidance.',
              'photo_tips': 'For best results, photograph one leaf in good daylight with the affected area clearly '
                            'visible.',
              'analysis_result': 'Analysis Result',
              'ai_assisted_notice': 'AI-assisted result based on the uploaded image.',
              'possible_disease': 'Possible Disease',
              'crop': 'Crop',
              'confidence': 'Confidence',
              'symptoms': 'Symptoms',
              'possible_causes': 'Possible Causes',
              'treatment_management': 'Treatment / Management',
              'prevention': 'Prevention',
              'immediate_steps': 'Immediate Steps',
              'expert_advice': 'Expert Advice',
              'ask_ai_assistant': 'Ask AI Assistant',
              'followup_prompt_desc': 'Ask about the analyzed leaf, symptoms, prevention or general crop care. The '
                                      'current analysis is kept in this session for follow-up questions.',
              'safety_notice': 'Safety notice',
              'safety_notice_desc': 'This AI assistant provides educational guidance. For critical crop decisions or '
                                    'pesticide usage, always verify with local agricultural extension officers.',
              'chat_placeholder': 'Type your farming question here...',
              'send': 'Send',
              'q_disease': 'What disease is this?',
              'q_todo': 'What should I do now?',
              'q_prevent': 'How can I prevent it?',
              'q_telugu': 'Explain in Telugu',
              'q_tamil': 'Explain in Tamil',
              'q_hindi': 'Explain in Hindi',
              'analyzing': 'Analyzing leaf image...',
              'ai_thinking': 'AI is generating guidance...',
              'no_leaf_analyzed_yet': 'Upload a leaf photo above to see AI diagnosis.',
              'admin': 'Admin',
              'customers': 'Customers',
              'owners': 'Owners',
              'payments': 'Payments',
              'ratings_reviews': 'Ratings & Reviews',
              'overview': 'Overview',
              'enterprise_operations': 'Enterprise Operations',
              'live_db_connected': 'Live Database Connected',
              'admin_management_desc': 'Live administrative management, member directories, and operational records.',
              'recent_bookings': 'Recent Bookings',
              'no_bookings_found': 'No bookings found.',
              'no_records_found': 'No records found.',
              'search': 'Search',
              'filter': 'Filter',
              'all_statuses': 'All statuses',
              'active': 'Active',
              'inactive': 'Inactive',
              'activate': 'Activate',
              'deactivate': 'Deactivate',
              'update_status': 'Update Status',
              'read_only_view': 'Read-only administrative view.',
              'registration_success': 'Registration successful! You can now log in.',
              'login_successful': 'Login successful.',
              'logout_successful': 'You have been logged out.',
              'invalid_credentials': 'Email or password is incorrect.',
              'invalid_customer_login': 'Mobile number or password is incorrect.',
              'invalid_owner_login': 'Email or password is incorrect.',
              'invalid_admin_login': 'Invalid admin username or password.',
              'email_already_registered': 'This email address is already registered. Please use a different email or '
                                          'sign in.',
              'mobile_already_registered': 'This mobile number is already registered. Please use a different number or '
                                           'sign in.',
              'valid_email_err': 'Enter a valid email address.',
              'valid_phone_err': 'Enter a valid 10-digit mobile number.',
              'password_length_err': 'Password must be 8 to 128 characters.',
              'passwords_dont_match': 'Passwords do not match.',
              'booking_date_past_err': 'Booking date cannot be in the past.',
              'select_valid_date_err': 'Please select a valid booking date.',
              'select_valid_route_err': 'Please select a valid route before booking.',
              'duplicate_booking_err': 'You already have a booking request for this vehicle on this date.',
              'booking_created_success': 'Booking request submitted successfully! Pending owner approval.',
              'booking_status_updated': 'Booking status updated.',
              'booking_status_update_err': 'Booking status could not be updated. Please try again.',
              'only_pending_can_change': 'Only pending bookings can change status.',
              'select_rating_err': 'Please select a rating from 1 to 5 stars.',
              'rating_saved': 'Thank you! Your rating and review have been saved.',
              'demo_payment_success': 'Demo payment completed successfully. A confirmation email has been sent.',
              'demo_payment_success_no_mail': 'Demo payment completed successfully. The confirmation email could not '
                                              'be sent, but payment is recorded.',
              'demo_payment_failed': 'Demo payment processing failed. Please try again.',
              'valid_payment_method_err': 'Please choose a valid demo payment method.',
              'cardholder_name_err': 'Please enter the cardholder name for the demo card.',
              'upi_id_err': 'Please enter the demo UPI ID for payment.',
              'vehicle_added_success': 'Vehicle added successfully! You can now configure routes & rent.',
              'vehicle_updated_success': 'Vehicle details updated successfully.',
              'vehicle_deleted_success': 'Vehicle removed successfully.',
              'access_denied': 'Access Denied: You can only manage your own vehicles and routes.',
              'invalid_csrf_token': 'Invalid CSRF token. Please refresh and try again.',
              'error_404_title': '404 - Page Not Found',
              'error_404_desc': 'The page you are looking for does not exist or has been moved.',
              'error_500_title': '500 - Server Error',
              'error_500_desc': 'An unexpected error occurred. Please try again later.'},
    'te': {
        'admin_login_badge': 'అడ్మిన్ లాగిన్',
        'username': 'వినియోగదారు పేరు',
        'accepted_bookings_desc': 'ధృవీకరించబడిన బుకింగ్‌లు',
        'action': 'చర్య',
        'action_col': 'చర్య',
        'active_vehicles_desc': 'చురుకైన మార్గాలతో సిద్ధంగా ఉంది',
        'ai_assisted_result_sub': 'అప్‌లోడ్ చేసిన చిత్రం ఆధారంగా AI సహాయక ఫలితం.',
        'ai_chat_welcome': 'నమస్కారం! పైన ఆకు చిత్రాన్ని అప్‌లోడ్ చేసి, ఫలితం గురించి నన్ను ప్రశ్నలు అడగండి.',
        'ai_disclaimer_note': 'అభ్యర్థించిన AI సహాయాన్ని అందించడానికి మాత్రమే మీ చిత్రం విశ్లేషించబడుతుంది. ఫలితాన్ని హామీ ఇవ్వబడిన రోగనిర్ధారణగా ఉపయోగించవద్దు.',
        'ai_disclaimer_short': 'AI ఫలితాలు సమాచార ప్రయోజనాల కోసం మాత్రమే మరియు వ్యవసాయ నిపుణులతో ధృవీకరించబడాలి.',
        'ai_service_not_configured': 'AI సేవ ఇంకా కాన్ఫిగర్ చేయబడలేదు. మీ .env ఫైల్‌కు AI_API_KEYని జోడించి, ఫ్లాస్క్‌ను పునఃప్రారంభించండి.',
        'amount': 'మొత్తం',
        'ask_ai_sub': 'విశ్లేషించబడిన ఆకు, లక్షణాలు, నివారణ లేదా సాధారణ పంట సంరక్షణ గురించి అడగండి.',
        'ask_followup_questions': 'తదుపరి ప్రశ్నలు అడగండి',
        'booking_id_col': 'బుకింగ్ ID',
        'chip_disease_label': 'ఇది ఏ వ్యాధి?',
        'chip_disease_query': 'ఈ ఆకుకు ఏ వ్యాధి ఉండవచ్చు?',
        'chip_lang_label': 'తెలుగులో వివరించండి',
        'chip_lang_query': 'దీనిని తెలుగులో వివరంగా మరియు సరళంగా వివరించండి.',
        'chip_prevent_label': 'దీన్ని ఎలా నివారించగలను?',
        'chip_prevent_query': 'ఈ సమస్య వ్యాపించకుండా ఎలా నివారించగలను?',
        'chip_steps_label': 'ఇప్పుడు నేను ఏమి చేయాలి?',
        'chip_steps_query': 'ఈ పంట సమస్యను పరిష్కరించడానికి ఇప్పుడు నేను ఏమి చేయాలి?',
        'choose_or_drag_photo': 'ఇక్కడ ఆకు ఫోటోను ఎంచుకోండి లేదా లాగండి',
        'contact_expert': 'నిపుణుడిని ఎప్పుడు సంప్రదించాలి',
        'demo_card': 'డెమో కార్డు',
        'demo_simulation_notice': 'సిమ్యులేషన్ మాత్రమే: ఈ డెమో చెల్లింపు ఎటువంటి నిజమైన కార్డ్, బ్యాంక్ లేదా UPI డేటాను ప్రాసెస్ చేయదు.',
        'demo_upi': 'డెమో UPI',
        'demo_upi_id': 'డెమో UPI ID',
        'enter_cardholder_name': 'డెమో కార్డ్ హోల్డర్ పేరును నమోదు చేయండి',
        'enter_demo_upi_id': 'demo@upi',
        'farmer_ai_desc': 'స్పష్టమైన పంట ఆకు ఫోటోను అప్‌లోడ్ చేయండి మరియు వ్యాధి, కనిపించే లక్షణాలు, సురక్షితమైన యాజమాన్య పద్ధతులు మరియు నివారణ మార్గదర్శకాలను పొందండి.',
        'file_format_limit': 'JPG, JPEG, PNG లేదా WEBP • గరిష్టంగా 10 MB',
        'fleet_management': 'వాహనాల నిర్వహణ',
        'fleet_overview': 'వాహనాల సారాంశం',
        'id': 'ID',
        'image_based_analysis': 'చిత్ర ఆధారిత విశ్లేషణ',
        'not_specified': 'పేర్కొనబడలేదు',
        'owner_hero_subtitle': 'మీ వాహనాలు, మార్గాలను నిర్వహించండి మరియు కస్టమర్ బుకింగ్ అభ్యర్థనలకు ప్రతిస్పందించండి.',
        'owner_tagline': 'రైతులకు • వ్యాపారులకు • మీ సేవలో',
        'pay': 'చెల్లించండి',
        'payment_col': 'చెల్లింపు',
        'payment_summary': 'చెల్లింపు సారాంశం',
        'pending_actions_desc': 'నిర్ధారణ అవసరం',
        'pending_review_banner': 'బుకింగ్ అభ్యర్థన(లు) మీ తక్షణ పరిశీలన మరియు చర్య కోసం వేచి ఉన్నాయి.',
        'pending_review_hint': 'క్రింద కస్టమర్, మార్గం, తేదీ మరియు అద్దెను పరిశీలించి, అంగీకరించు లేదా తిరస్కరించు క్లిక్ చేయండి.',
        'processing': 'ప్రాసెస్ అవుతోంది...',
        'processing_demo_payment': 'డెమో చెల్లింపు ప్రాసెస్ అవుతోంది...',
        'profile': 'యజమాని ప్రొఫైల్',
        'ready_when_you_are': 'మీరు సిద్ధంగా ఉన్నప్పుడే మేము సిద్ధం',
        'route_col': 'మార్గం',
        'route_pricing': 'మార్గం ఆధారిత ధర',
        'route_pricing_desc': 'పారదర్శక మరియు న్యాయమైన ధరలు',
        'secure_desc': 'మీ నమ్మకమే మా ప్రాధాన్యత',
        'secure_reliable': 'సురక్షితమైన & విశ్వసనీయమైన',
        'sidebar_dashboard': 'డ్యాష్‌బోర్డ్',
        'start_moving': 'మీ తదుపరి రవాణాను ప్రారంభించండి.',
        'support_247': '24/7 మద్దతు',
        'support_desc': 'సహాయం చేయడానికి మేము ఎల్లప్పుడూ సిద్ధంగా ఉన్నాము',
        'symptoms_explanation': 'లక్షణాలు & దృశ్య వివరణ',
        'total_bookings_desc': 'మొత్తం బుకింగ్‌లు',
        'total_due': 'మొత్తం బకాయి',
        'total_vehicles_desc': 'నమోదిత వాహనాలు',
        'type_question_placeholder': 'మీ వ్యవసాయ సంబంధిత ప్రశ్నను ఇక్కడ టైప్ చేయండి...',
        'upload_another_image': 'మరొక చిత్రాన్ని అప్‌లోడ్ చేయండి',
        'upload_leaf_tip': 'ఉత్తమ ఫలితాల కోసం, ప్రభావిత ప్రాంతం స్పష్టంగా కనిపించేలా మంచి వెలుతురులో ఒక ఆకును ఫోటో తీయండి.',
        'vehicle': 'వాహనం',
        'vehicle_col': 'వాహనం',
        'view_all_bookings': 'వాహనాల జాబితా',
        'wide_range': 'విస్తృత వాహన శ్రేణి',
        'wide_range_desc': 'చిన్న నుండి భారీ వాహనాల వరకు',   'language': 'భాష',
              'english': 'English',
              'telugu': 'తెలుగు',
              'tamil': 'தமிழ்',
              'hindi': 'हिन्दी',
              'vehicle_booking': 'వాహన బుకింగ్',
              'tagline': 'రైతులకు • వ్యాపారులకు • మీ సేవలో',
              'home': 'హోమ్',
              'vehicles': 'వాహనాలు',
              'available_vehicles': 'అందుబాటులో ఉన్న వాహనాలు',
              'my_bookings': 'నా బుకింగ్స్',
              'dashboard': 'డాష్\u200cబోర్డ్',
              'owner_dashboard': 'యజమాని డాష్\u200cబోర్డ్',
              'admin_dashboard': 'అడ్మిన్ డాష్\u200cబోర్డ్',
              'control_center': 'నియంత్రణ కేంద్రం',
              'security_clearance': 'భద్రతా అనుమతి',
              'farmer_ai': 'రైతు AI',
              'farmer_ai_assistant': 'రైతు AI పంట వ్యాధి సహాయకుడు',
              'customer': 'కస్టమర్',
              'customer_login': 'కస్టమర్ లాగిన్',
              'owner_login': 'వాహన యజమాని లాగిన్',
              'admin_login': 'అడ్మిన్ లాగిన్',
              'book_vehicle': 'వాహనం బుక్ చేసుకోండి',
              'login': 'లాగిన్',
              'register': 'రిజిస్టర్',
              'customer_registration': 'కస్టమర్ రిజిస్ట్రేషన్',
              'owner_registration': 'వాహన యజమాని రిజిస్ట్రేషన్',
              'logout': 'లాగ్అవుట్',
              'welcome': 'స్వాగతం',
              'back_home': 'హోమ్\u200cకి తిరిగి వెళ్ళండి',
              'back_to_vehicles': 'వాహనాల జాబితాకు తిరిగి వెళ్ళండి',
              'back_to_dashboard': 'డాష్\u200cబోర్డ్\u200cకు తిరిగి వెళ్ళండి',
              'back_to_public': 'ప్రజా వెబ్\u200cసైట్\u200cకు తిరిగి వెళ్ళండి',
              'mobile_number': 'మొబైల్ నంబర్',
              'customer_name': 'కస్టమర్ పేరు',
              'owner_name': 'యజమాని పేరు',
              'registered_email': 'నమోదిత ఇమెయిల్',
              'email_address': 'ఇమెయిల్ చిరునామా',
              'email': 'ఇమెయిల్',
              'phone': 'ఫోన్',
              'phone_number': 'ఫోన్ నంబర్',
              'password': 'పాస్\u200cవర్డ్',
              'confirm_password': 'పాస్\u200cవర్డ్ నిర్ధారించండి',
              'forgot_password': 'పాస్\u200cవర్డ్ మర్చిపోయారా?',
              'reset_password': 'పాస్\u200cవర్డ్ రీసెట్ చేయండి',
              'set_new_password': 'కొత్త పాస్\u200cవర్డ్\u200cను సెట్ చేయండి',
              'new_password': 'కొత్త పాస్\u200cవర్డ్',
              'send_reset_link': 'రీసెట్ లింక్ పంపండి',
              'back_to_customer_login': 'కస్టమర్ లాగిన్\u200cకి తిరిగి వెళ్ళండి',
              'enter_registered_email': 'మీ నమోదిత ఇమెయిల్ చిరునామాను నమోదు చేయండి.',
              'login_mobile': 'మీ మొబైల్ నంబర్\u200cతో లాగిన్ అవ్వండి.',
              'create_account': 'మీ ఖాతాను సృష్టించండి',
              'choose_account_type': 'మీరు వాహన బుకింగ్\u200cను ఎలా ఉపయోగించాలనుకుంటున్నారో ఎంచుకోండి',
              'for_customers': 'రైతులు & వ్యాపార కస్టమర్ల కోసం',
              'for_owners': 'వాణిజ్య వాహనాల యజమానుల కోసం',
              'customer_desc': 'మీ రవాణా అవసరాల కోసం నమ్మకమైన వాణిజ్య వాహనాలను కనుగొని బుక్ చేసుకోండి.',
              'owner_desc': 'మీ వాణిజ్య వాహనాలను నమోదు చేసుకోండి మరియు కస్టమర్ల నుండి బుకింగ్ అభ్యర్థనలను పొందండి.',
              'register_as_customer': 'కస్టమర్\u200cగా నమోదు చేసుకోండి',
              'customer_sign_in': 'కస్టమర్ సైన్ ఇన్',
              'register_as_owner': 'వాహన యజమానిగా నమోదు చేసుకోండి',
              'owner_sign_in': 'వాహన యజమాని సైన్ ఇన్',
              'new_vehicle_owner': 'కొత్త వాహన యజమానియా?',
              'already_an_owner': 'ఇప్పటికే యజమానిగా ఖాతా ఉందా?',
              'already_have_account': 'ఇప్పటికే ఖాతా ఉందా?',
              'new_customer': 'కొత్త కస్టమరా?',
              'sign_in_here': 'ఇక్కడ సైన్ ఇన్ చేయండి',
              'register_here': 'ఇక్కడ నమోదు చేసుకోండి',
              'enter_owner_name_placeholder': 'యజమాని లేదా వ్యాపార పేరు నమోదు చేయండి',
              'enter_phone_placeholder': '10 అంకెల మొబైల్ నంబర్ నమోదు చేయండి',
              'enter_owner_email_placeholder': 'owner@example.com (లాగిన్ & నోటిఫికేషన్ల కోసం)',
              'enter_password_placeholder': 'పాస్\u200cవర్డ్ నమోదు చేయండి (కనీసం 8 అక్షరాలు)',
              'enter_admin_username': 'అడ్మిన్ యూజర్\u200cనేమ్ నమోదు చేయండి',
              'enter_admin_password': 'అడ్మిన్ పాస్\u200cవర్డ్ నమోదు చేయండి',
              'admin_login_desc': 'నిర్వహణ మరియు ప్రత్యక్ష కార్యకలాపాల కోసం సైన్ ఇన్ చేయండి.',
              'admin_copyright': '© 2026 వాహన బుకింగ్ • అడ్మినిస్ట్రేటివ్ సిస్టమ్స్',
              'heavy_vehicles_title': 'ఉజ్వల భవిష్యత్తు కోసం భారీ వాహనాలు',
              'book_vehicles_ease': 'సులభంగా వాణిజ్య వాహనాలను బుక్ చేసుకోండి',
              'hero_subtitle': 'మెరుగైన రేపటి కోసం రైతులు మరియు వ్యాపారవేత్తలను నమ్మకమైన వాణిజ్య వాహనాలతో కలుపుతోంది.',
              'explore_vehicles': 'వాహనాలను అన్వేషించండి',
              'try_farmer_ai': 'రైతు AI ప్రయత్నించండి',
              'smart_farming_support': 'స్మార్ట్ వ్యవసాయ సహాయం',
              'how_it_works': 'ఇది ఎలా పనిచేస్తుంది',
              'choose_vehicle': 'వాహనాన్ని ఎంచుకోండి',
              'select_date': 'తేదీని ఎంచుకోండి',
              'confirm': 'నిర్ధారించండి',
              'vehicle_types': 'వాహన రకాలు',
              'tractor': 'ట్రాక్టర్',
              'mini_truck': 'మినీ ట్రక్',
              'pickup': 'పికప్',
              'lorry': 'లారీ',
              'goods_auto': 'గూడ్స్ ఆటో',
              'tractor_desc': 'వ్యవసాయ మరియు సరుకుల రవాణాకు అనుకూలమైనది.',
              'mini_truck_desc': 'చిన్న మరియు మధ్య తరహా లోడ్లకు అనుకూలమైనది.',
              'pickup_desc': 'స్థానిక రవాణాకు అత్యంత సౌకర్యవంతమైనది.',
              'lorry_desc': 'భారీ సరుకులను రవాణా చేయడానికి అనువైనది.',
              'goods_auto_desc': 'స్థానిక మార్కెట్ సరుకులకు వేగవంతమైన రవాణా.',
              'simple_fast_easy': 'సులభం • వేగవంతం • సురక్షితం',
              'transportation_message': 'మీ రవాణా అవసరాలకు తగిన వాహనాన్ని తక్షణమే బుక్ చేసుకోండి.',
              'footer_message': 'అందరికీ అందుబాటులో ఉండే సులభమైన రవాణా సేవ.',
              'vehicle_type': 'వాహనం రకం',
              'location': 'స్థానం',
              'from': 'ప్రారంభ స్థలం',
              'to': 'గమ్యస్థానం',
              'rent': 'అద్దె',
              'contact': 'సంప్రదించండి',
              'book_now': 'ఇప్పుడే బుక్ చేయండి',
              'no_vehicles': 'వాహనాలు అందుబాటులో లేవు',
              'no_vehicles_message': 'ప్రస్తుతం బుకింగ్ కోసం వాహనాలు ఏవీ అందుబాటులో లేవు.',
              'search_placeholder': 'పేరు, రూట్ లేదా స్థానం శోధించండి',
              'location_placeholder': 'స్థానం లేదా పట్టణం',
              'all_vehicle_types': 'అన్ని వాహన రకాలు',
              'min_rent': 'కనిష్ట అద్దె (₹)',
              'max_rent': 'గరిష్ట అద్దె (₹)',
              'apply_filters': 'ఫిల్టర్లను వర్తింపజేయండి',
              'reset_filters': 'ఫిల్టర్లను రీసెట్ చేయండి',
              'view_details': 'వివరాలు చూడండి',
              'book_this_vehicle': 'ఈ వాహనాన్ని బుక్ చేయండి',
              'available_routes_rent': 'అందుబాటులో ఉన్న రూట్లు & నిర్ణీత అద్దె',
              'base_location': 'ప్రధాన స్థావరం',
              'current_location_label': 'ప్రస్తుత / బేస్ స్థానం',
              'capacity_weight': 'లోడ్ సామర్థ్యం',
              'availability_status': 'లభ్యత',
              'available_ready': 'అందుబాటులో ఉంది (బుకింగ్\u200cకు సిద్ధం)',
              'route_label': 'రూట్',
              'fixed_route_rent_label': 'నిర్ణీత రూట్ అద్దె',
              'booking_date': 'బుకింగ్ తేదీ',
              'confirm_booking': 'బుకింగ్\u200cను నిర్ధారించండి',
              'status': 'స్థితి',
              'accepted': 'ఆమోదించబడింది',
              'rejected': 'తిరస్కరించబడింది',
              'pending': 'వేచి ఉంది',
              'paid': 'చెల్లించబడింది',
              'pay_now': 'ఇప్పుడే చెల్లించండి',
              'no_bookings': 'బుకింగ్స్ లేవు',
              'no_bookings_message': 'మీరు ఇంకా ఎలాంటి వాహన బుకింగ్\u200cలు చేయలేదు.',
              'booking_id': 'బుకింగ్ ID',
              'customer_booking_requests': 'కస్టమర్ బుకింగ్ అభ్యర్థనలు',
              'accept': 'ఆమోదించండి',
              'reject': 'తిరస్కరించండి',
              'no_action_needed': 'ఎలాంటి చర్య అవసరం లేదు',
              'rate_service': 'సేవకు రేటింగ్ ఇవ్వండి',
              'rate_booking': 'మీ బుకింగ్\u200cను రేట్ చేయండి',
              'rate_booking_title': '⭐ మీ బుకింగ్\u200cను రేట్ చేయండి',
              'review_optional': 'సమీక్ష (ఐచ్ఛికం)',
              'review_placeholder': 'వాహనం మరియు యజమాని సేవ ఎలా ఉంది?',
              'save_rating': 'రేటింగ్ సేవ్ చేయండి',
              'rating': 'రేటింగ్',
              'booking_success_title': 'బుకింగ్ అభ్యర్థన సమర్పించబడింది',
              'booking_success_desc': 'మీ బుకింగ్ యజమాని ఆమోదం కోసం వేచి ఉంది.',
              'waiting_owner_confirm': 'యజమాని నిర్ధారణ కోసం వేచి చూస్తోంది.',
              'view_my_bookings': 'నా బుకింగ్స్ చూడండి',
              'browse_vehicles': 'వాహనాలను బ్రౌజ్ చేయండి',
              'track_bookings_desc': 'ప్రతి రూట్ బుకింగ్\u200cను ట్రాక్ చేయండి మరియు సేవలను రేట్ చేయండి.',
              'booking_confirmed_badge': 'బుకింగ్ నిర్ధారించబడింది',
              'add_vehicle': 'వాహనం జోడించండి',
              'add_vehicle_title': '🚚 వాహనం జోడించండి',
              'add_route': 'రూట్ జోడించండి',
              'add_route_rent': '+ రూట్ & అద్దె జోడించండి',
              'add_route_subtitle': 'కస్టమర్లు సులభంగా బుక్ చేసుకునేలా మీ వాహనాలకు నిర్ణీత రూట్ అద్దెను సెట్ చేయండి.',
              'route_details': 'రూట్ వివరాలు',
              'rent_details': 'అద్దె వివరాలు',
              'additional_info': 'అదనపు సమాచారం',
              'select_vehicle': 'వాహనాన్ని ఎంచుకోండి',
              'choose_vehicle_prompt': '-- మీ వాహనాన్ని ఎంచుకోండి --',
              'from_location': 'ప్రారంభ స్థలం / Origin',
              'to_location': 'గమ్యస్థానం / Destination',
              'fixed_route_rent': 'రూట్ ఆధారిత నిర్ణీత అద్దె (₹)',
              'save_route_rent': 'రూట్ & అద్దెను సేవ్ చేయండి',
              'save_vehicle': 'వాహనాన్ని సేవ్ చేయండి',
              'save_changes': 'మార్పులను సేవ్ చేయండి',
              'cancel': 'రద్దు చేయండి',
              'route_added_success': 'రూట్ మరియు అద్దె విజయవంతంగా జోడించబడింది! ✅',
              'add_another_route': 'మరొక రూట్ జోడించండి',
              'my_vehicles': 'నా వాహనాలు',
              'edit_vehicle': 'వాహనాన్ని సవరించండి',
              'edit_vehicle_title': '✏️ వాహనాన్ని సవరించండి',
              'vehicle_name': 'వాహనం పేరు',
              'contact_number': 'సంప్రదింపు నంబర్',
              'update_vehicle': 'వాహనాన్ని నవీకరించండి',
              'delete_vehicle': 'వాహనాన్ని తొలగించండి',
              'active_vehicles': 'యాక్టివ్ వాహనాలు',
              'total_bookings': 'మొత్తం బుకింగ్స్',
              'total_vehicles': 'మొత్తం వాహనాలు',
              'pending_action': 'చర్య కోసం వేచి ఉన్నవి',
              'accepted_bookings': 'ఆమోదించిన బుకింగ్స్',
              'quick_actions_title': 'త్వరిత నిర్వహణ',
              'manage_fleet': 'ఫ్లీట్ నిర్వహించండి',
              'manage_fleet_desc': 'ఉన్న వాహనాలను వీక్షించండి, సవరించండి లేదా తొలగించండి',
              'manage_routes_desc': 'కస్టమర్ల కోసం నిర్ణీత రూట్ ధరలను సెట్ చేయండి',
              'owner_profile_title': 'యజమాని ప్రొఫైల్',
              'account_status': 'ఖాతా స్థితి',
              'active_fleet': 'యాక్టివ్ ఫ్లీట్',
              'no_bookings_title': 'ఇంకా ఎలాంటి బుకింగ్ అభ్యర్థనలు లేవు',
              'no_bookings_desc': 'మీ వాహనాలకు సంబంధించిన కస్టమర్ బుకింగ్\u200cలు ఇక్కడ కనిపిస్తాయి.',
              'choose_vehicle_fleet_hint': 'మీ నమోదిత ఫ్లీట్ నుండి వాహనాన్ని ఎంచుకోండి.',
              'starting_point_hint': 'ప్రారంభ పట్టణం, గ్రామం లేదా లోడింగ్ పాయింట్.',
              'delivery_point_hint': 'డెలివరీ మార్కెట్, గిడ్డంగి లేదా గమ్యస్థాన పట్టణం.',
              'total_fixed_rent_hint': 'ఈ వన్-వే రూట్ ట్రిప్ కోసం మొత్తం నిర్ణీత అద్దె.',
              'route_open_notice': 'ఈ వాహన రూట్ తక్షణమే కస్టమర్ బుకింగ్\u200cల కోసం అందుబాటులోకి వస్తుంది.',
              'transparent_pricing_title': 'పారదర్శక రూట్ ఆధారిత ధరలు',
              'source_placeholder': 'ఉదాహరణ: తాడిపత్రి',
              'destination_placeholder': 'ఉదాహరణ: అనంతపురం',
              'rent_placeholder': 'ఉదాహరణ: 2500',
              'vehicle_name_example': 'ఉదాహరణ: టాటా ఏస్',
              'location_example': 'ఉదాహరణ: కాంచీపురం',
              'demo_payment': 'డెమో చెల్లింపు',
              'demo_payment_banner': 'డెమో చెల్లింపు — అసలు డబ్బు తీసుకోబడదు',
              'secure_demo_payment': 'సురక్షిత డెమో చెల్లింపు',
              'demo_payment_disclaimer': 'ఇది ప్రాజెక్ట్ డెమో లావాదేవీ మాత్రమే. అసలు డబ్బు వసూలు చేయబడదు.',
              'amount_paid': 'చెల్లించిన మొత్తం',
              'payment_method': 'చెల్లింపు విధానం',
              'cardholder_name': 'కార్డ్ హోల్డర్ పేరు',
              'upi_id': 'UPI ID',
              'enter_cardholder_placeholder': 'డెమో కార్డ్ హోల్డర్ పేరు నమోదు చేయండి',
              'enter_upi_placeholder': 'demo@upi',
              'pay_amount_btn': 'మొత్తాన్ని చెల్లించండి',
              'payment_successful': 'చెల్లింపు విజయవంతమైంది',
              'payment_success_desc': 'ఈ డెమో చెల్లింపు విజయవంతంగా పూర్తయింది మరియు సిస్టమ్\u200cలో నమోదు చేయబడింది.',
              'transaction_reference': 'లావాదేవీ రిఫరెన్స్',
              'date': 'తేదీ',
              'view_receipt': 'రసీదు చూడండి',
              'download_receipt': 'రసీదు డౌన్\u200cలోడ్ చేయండి',
              'view_booking': 'బుకింగ్ చూడండి',
              'upload_leaf_image': 'ఆకు చిత్రాన్ని అప్\u200cలోడ్ చేయండి',
              'analyze_leaf': 'ఆకును విశ్లేషించండి',
              'drag_leaf_image': 'ఆకు ఫోటోను ఇక్కడ ఎంచుకోండి లేదా డ్రాగ్ చేయండి',
              'image_format_limit': 'JPG, JPEG, PNG లేదా WEBP • గరిష్టంగా 10 MB',
              'ai_assistant_intro': 'స్పష్టమైన పంట ఆకు ఫోటోను అప్\u200cలోడ్ చేయండి మరియు సాధ్యమయ్యే వ్యాధి, లక్షణాలు '
                                    'మరియు నివారణ మార్గదర్శకాలను AI ద్వారా తెలుసుకోండి.',
              'photo_tips': 'మంచి ఫలితాల కోసం, మంచి వెలుతురులో వ్యాధి సోకిన భాగాన్ని స్పష్టంగా ఫోటో తీయండి.',
              'analysis_result': 'విశ్లేషణ ఫలితం',
              'ai_assisted_notice': 'అప్\u200cలోడ్ చేసిన చిత్రం ఆధారంగా AI అందించిన ఫలితం.',
              'possible_disease': 'సాధ్యమయ్యే వ్యాధి',
              'crop': 'పంట',
              'confidence': 'ఖచ్చితత్వం',
              'symptoms': 'లక్షణాలు',
              'possible_causes': 'సాధ్యమయ్యే కారణాలు',
              'treatment_management': 'చికిత్స / నిర్వహణ',
              'prevention': 'నివారణ',
              'immediate_steps': 'తక్షణ చర్యలు',
              'expert_advice': 'నిపుణుల సలహా',
              'ask_ai_assistant': 'AI సహాయకుడిని అడగండి',
              'followup_prompt_desc': 'విశ్లేషించిన ఆకు, లక్షణాలు లేదా పంట సంరక్షణ గురించి ప్రశ్నలు అడగండి.',
              'safety_notice': 'భద్రతా సూచన',
              'safety_notice_desc': 'ఈ AI విద్యాపరమైన మార్గదర్శకత్వాన్ని మాత్రమే అందిస్తుంది. క్రిమిసంహారక మందుల '
                                    'వినియోగానికి స్థానిక వ్యవసాయ అధికారులను సంప్రదించండి.',
              'chat_placeholder': 'మీ వ్యవసాయ ప్రశ్నను ఇక్కడ టైప్ చేయండి...',
              'send': 'పంపండి',
              'q_disease': 'ఇది ఏ వ్యాధి?',
              'q_todo': 'నేను ఇప్పుడు ఏమి చేయాలి?',
              'q_prevent': 'దీనిని ఎలా నివారించాలి?',
              'q_telugu': 'తెలుగులో వివరించండి',
              'q_tamil': 'తమిళంలో వివరించండి',
              'q_hindi': 'హిందీలో వివరించండి',
              'analyzing': 'ఆకు చిత్రాన్ని విశ్లేషిస్తోంది...',
              'ai_thinking': 'AI సలహాను రూపొందిస్తోంది...',
              'no_leaf_analyzed_yet': 'AI నిర్ధారణ చూడటానికి పైన ఆకు ఫోటోను అప్\u200cలోడ్ చేయండి.',
              'admin': 'అడ్మిన్',
              'customers': 'కస్టమర్లు',
              'owners': 'యజమానులు',
              'payments': 'చెల్లింపులు',
              'ratings_reviews': 'రేటింగ్\u200cలు & సమీక్షలు',
              'overview': 'అవలోకనం',
              'enterprise_operations': 'నిర్వహణ కార్యకలాపాలు',
              'live_db_connected': 'లైవ్ డేటాబేస్ కనెక్ట్ చేయబడింది',
              'admin_management_desc': 'ప్రత్యక్ష పరిపాలనా నిర్వహణ మరియు రికార్డులు.',
              'recent_bookings': 'ఇటీవలి బుకింగ్స్',
              'no_bookings_found': 'బుకింగ్స్ కనుగొనబడలేదు.',
              'no_records_found': 'రికార్డులు కనుగొనబడలేదు.',
              'search': 'శోధించండి',
              'filter': 'ఫిల్టర్',
              'all_statuses': 'అన్ని స్థితులు',
              'active': 'యాక్టివ్',
              'inactive': 'ఇన్\u200cయాక్టివ్',
              'activate': 'యాక్టివేట్ చేయండి',
              'deactivate': 'డీయాక్టివేట్ చేయండి',
              'update_status': 'స్థితిని నవీకరించండి',
              'read_only_view': 'పరిపాలనా వీక్షణ మాత్రమే.',
              'registration_success': 'నమోదు విజయవంతమైంది! మీరు ఇప్పుడు లాగిన్ అవ్వవచ్చు.',
              'login_successful': 'లాగిన్ విజయవంతమైంది.',
              'logout_successful': 'మీరు లాగ్అవుట్ అయ్యారు.',
              'invalid_credentials': 'ఇమెయిల్ లేదా పాస్\u200cవర్డ్ తప్పు.',
              'invalid_customer_login': 'మొబైల్ నంబర్ లేదా పాస్\u200cవర్డ్ తప్పు.',
              'invalid_owner_login': 'ఇమెయిల్ లేదా పాస్\u200cవర్డ్ తప్పు.',
              'invalid_admin_login': 'అడ్మిన్ యూజర్\u200cనేమ్ లేదా పాస్\u200cవర్డ్ తప్పు.',
              'email_already_registered': 'ఈ ఇమెయిల్ చిరునామా ఇప్పటికే నమోదు చేయబడింది. దయచేసి వేరే ఇమెయిల్\u200cను '
                                          'ఉపయోగించండి లేదా సైన్ ఇన్ చేయండి.',
              'mobile_already_registered': 'ఈ మొబైల్ నంబర్ ఇప్పటికే నమోదు చేయబడింది. దయచేసి వేరే నంబర్\u200cను '
                                           'ఉపయోగించండి లేదా సైన్ ఇన్ చేయండి.',
              'valid_email_err': 'సరైన ఇమెయిల్ చిరునామాను నమోదు చేయండి.',
              'valid_phone_err': 'సరైన 10 అంకెల మొబైల్ నంబర్\u200cను నమోదు చేయండి.',
              'password_length_err': 'పాస్\u200cవర్డ్ 8 నుండి 128 అక్షరాల వరకు ఉండాలి.',
              'passwords_dont_match': 'పాస్\u200cవర్డ్\u200cలు సరిపోలడం లేదు.',
              'booking_date_past_err': 'గత తేదీలలో బుకింగ్ చేయలేరు.',
              'select_valid_date_err': 'దయచేసి సరైన బుకింగ్ తేదీని ఎంచుకోండి.',
              'select_valid_route_err': 'దయచేసి బుకింగ్ చేయడానికి ముందు సరైన రూట్\u200cను ఎంచుకోండి.',
              'duplicate_booking_err': 'ఈ తేదీన ఈ వాహనానికి మీ వద్ద ఇప్పటికే బుకింగ్ అభ్యర్థన ఉంది.',
              'booking_created_success': 'బుకింగ్ అభ్యర్థన విజయవంతంగా సమర్పించబడింది! యజమాని ఆమోదం కోసం వేచి ఉంది.',
              'booking_status_updated': 'బుకింగ్ స్థితి నవీకరించబడింది.',
              'booking_status_update_err': 'బుకింగ్ స్థితిని నవీకరించడం సాధ్యం కాలేదు. దయచేసి మళ్ళీ ప్రయత్నించండి.',
              'only_pending_can_change': 'పెండింగ్\u200cలో ఉన్న బుకింగ్\u200cల స్థితిని మాత్రమే మార్చగలరు.',
              'select_rating_err': 'దయచేసి 1 నుండి 5 నక్షత్రాల మధ్య రేటింగ్\u200cను ఎంచుకోండి.',
              'rating_saved': 'ధన్యవాదాలు! మీ రేటింగ్ మరియు సమీక్ష సేవ్ చేయబడ్డాయి.',
              'demo_payment_success': 'డెమో చెల్లింపు విజయవంతంగా పూర్తయింది. నిర్ధారణ ఇమెయిల్ పంపబడింది.',
              'demo_payment_success_no_mail': 'డెమో చెల్లింపు విజయవంతంగా పూర్తయింది. చెల్లింపు రికార్డ్ చేయబడింది.',
              'demo_payment_failed': 'డెమో చెల్లింపు ప్రాసెసింగ్ విఫలమైంది. దయచేసి మళ్ళీ ప్రయత్నించండి.',
              'valid_payment_method_err': 'దయచేసి సరైన చెల్లింపు పద్ధతిని ఎంచుకోండి.',
              'cardholder_name_err': 'దయచేసి డెమో కార్డు కోసం కార్డుదారుని పేరు నమోదు చేయండి.',
              'upi_id_err': 'దయచేసి చెల్లింపు కోసం డెమో UPI IDని నమోదు చేయండి.',
              'vehicle_added_success': 'వాహనం విజయవంతంగా జోడించబడింది! ఇప్పుడు మీరు రూట్లు మరియు అద్దెను సెట్ '
                                       'చేయవచ్చు.',
              'vehicle_updated_success': 'వాహనం వివరాలు విజయవంతంగా నవీకరించబడ్డాయి.',
              'vehicle_deleted_success': 'వాహనం విజయవంతంగా తొలగించబడింది.',
              'access_denied': 'అనుమతి నిరాకరించబడింది: మీరు మీ స్వంత వాహనాలు మరియు రూట్లను మాత్రమే నిర్వహించగలరు.',
              'invalid_csrf_token': 'చెల్లని CSRF టోకెన్. దయచేసి పేజీని రీఫ్రెష్ చేసి మళ్ళీ ప్రయత్నించండి.',
              'error_404_title': '404 - పేజీ కనుగొనబడలేదు',
              'error_404_desc': 'మీరు వెతుకుతున్న పేజీ ఉనికిలో లేదు లేదా తరలించబడింది.',
              'error_500_title': '500 - సర్వర్ లోపం',
              'error_500_desc': 'అనుకోని లోపం సంభవించింది. దయచేసి కాసేపటి తర్వాత మళ్ళీ ప్రయత్నించండి.'},
    'ta': {
        'admin_login_badge': 'நிர்வாக உள்நுழைவு',
        'username': 'பயனர்பெயர்',
        'accepted_bookings_desc': 'உறுதிப்படுத்தப்பட்ட முன்பதிவுகள்',
        'action': 'நடவடிக்கை',
        'action_col': 'நடவடிக்கை',
        'active_vehicles_desc': 'செயலில் உள்ள வழிகளுடன் தயாராக உள்ளது',
        'ai_assisted_result_sub': 'பதிவேற்றிய படத்தின் அடிப்படையில் AI உதவி முடிவு.',
        'ai_chat_welcome': 'வணக்கம்! மேலே உள்ள இலை படத்தை பதிவேற்றி, முடிவு பற்றி என்னிடம் கேள்விகள் கேளுங்கள்.',
        'ai_disclaimer_note': 'கோரப்பட்ட AI உதவியை வழங்க மட்டுமே உங்கள் படம் பகுப்பாய்வு செய்யப்படுகிறது. முடிவை உத்தரவாதமான நோயறிதலாகப் பயன்படுத்த வேண்டாம்.',
        'ai_disclaimer_short': 'AI முடிவுகள் தகவல் நோக்கங்களுக்காக மட்டுமே மற்றும் விவசாய நிபுணர்களுடன் சரிபார்க்கப்பட வேண்டும்.',
        'ai_service_not_configured': 'AI சேவை இன்னும் கட்டமைக்கப்படவில்லை. உங்கள் .env கோப்பில் AI_API_KEY ஐச் சேர்த்து, பிளாஸ்க்கை மறுதொடக்கம் செய்யவும்.',
        'amount': 'தொகை',
        'ask_ai_sub': 'பகுப்பாய்வு செய்யப்பட்ட இலை, அறிகுறிகள், தடுப்பு அல்லது பொதுவான பயிர் பராமரிப்பு பற்றி கேளுங்கள்.',
        'ask_followup_questions': 'பின்தொடர்தல் கேள்விகளைக் கேளுங்கள்',
        'booking_id_col': 'முன்பதிவு ID',
        'chip_disease_label': 'இது என்ன நோய்?',
        'chip_disease_query': 'இந்த இலையில் என்ன நோய் இருக்கக்கூடும்?',
        'chip_lang_label': 'தமிழில் விளக்குங்கள்',
        'chip_lang_query': 'இதை தமிழில் விரிவாகவும் தெளிவாகவும் விளக்குங்கள்.',
        'chip_prevent_label': 'இதை எவ்வாறு தடுப்பது?',
        'chip_prevent_query': 'இந்த பிரச்சினை பரவாமல் எவ்வாறு தடுப்பது?',
        'chip_steps_label': 'இப்போது நான் என்ன செய்ய வேண்டும்?',
        'chip_steps_query': 'இந்த பயிர் சிக்கலை நிர்வகிக்க இப்போது நான் என்ன செய்ய வேண்டும்?',
        'choose_or_drag_photo': 'இங்கே இலை புகைப்படத்தைத் தேர்ந்தெடுக்கவும் அல்லது இழுக்கவும்',
        'contact_expert': 'நிபுணரை எப்போது தொடர்பு கொள்ள வேண்டும்',
        'demo_card': 'டெமோ அட்டை',
        'demo_simulation_notice': 'உருவகப்படுத்துதல் மட்டுமே: இந்த டெமோ கட்டணம் எந்த உண்மையான அட்டை, வங்கி அல்லது UPI தரவையும் செயலாக்காது.',
        'demo_upi': 'டெமோ UPI',
        'demo_upi_id': 'டெமோ UPI ID',
        'enter_cardholder_name': 'டெமோ அட்டைதாரர் பெயரை உள்ளிடவும்',
        'enter_demo_upi_id': 'demo@upi',
        'farmer_ai_desc': 'தெளிவான பயிர் இலை புகைப்படத்தைப் பதிவேற்றி சாத்தியமான நோய், அறிகுறிகள், பாதுகாப்பான மேலாண்மை படிகள் மற்றும் தடுப்பு வழிகாட்டுதலைப் பெறுங்கள்.',
        'file_format_limit': 'JPG, JPEG, PNG அல்லது WEBP • அதிகபட்சம் 10 MB',
        'fleet_management': 'வாகனக் குழு மேலாண்மை',
        'fleet_overview': 'வாகனக் குழு கண்ணோட்டம்',
        'id': 'ID',
        'image_based_analysis': 'பட அடிப்படையிலான பகுப்பாய்வு',
        'not_specified': 'குறிப்பிடப்படவில்லை',
        'owner_hero_subtitle': 'உங்கள் வாகனங்கள், வழிகளை நிர்வகிக்கவும் வாடிக்கையாளர் முன்பதிவு கோரிக்கைகளுக்கு பதிலளிக்கவும்.',
        'owner_tagline': 'விவசாயிகளுக்கு • வணிகர்களுக்கு • உங்கள் சேவையில்',
        'pay': 'செலுத்துங்கள்',
        'payment_col': 'கட்டணம்',
        'payment_summary': 'கட்டண சுருக்கம்',
        'pending_actions_desc': 'உறுதிப்படுத்தல் தேவை',
        'pending_review_banner': 'முன்பதிவு கோரிக்கை(கள்) உங்கள் உடனடி மதிப்பாய்வு மற்றும் நடவடிக்கை தேவை.',
        'pending_review_hint': 'வாடிக்கையாளர், வழி, தேதி மற்றும் வாடகையை கீழே மதிப்பாய்வு செய்து, ஏற்றுக்கொள் அல்லது நிராகரி என்பதைக் கிளிக் செய்யவும்.',
        'processing': 'செயலாக்குகிறது...',
        'processing_demo_payment': 'டெமோ கட்டணம் செயலாக்கப்படுகிறது...',
        'profile': 'உரிமையாளர் சுயவிவரம்',
        'ready_when_you_are': 'நீங்கள் தயாராக இருக்கும்போது',
        'route_col': 'வழி',
        'route_pricing': 'வழி அடிப்படையிலான விலை',
        'route_pricing_desc': 'வெளிப்படையான மற்றும் நியாயமான கட்டணங்கள்',
        'secure_desc': 'உங்கள் நம்பிக்கையே எங்கள் முன்னுரிமை',
        'secure_reliable': 'பாதுகாப்பானது & நம்பகமானது',
        'sidebar_dashboard': 'டாஷ்போர்டு',
        'start_moving': 'உங்கள் அடுத்த சரக்கு போக்குவரத்தைத் தொடங்குங்கள்.',
        'support_247': '24/7 ஆதரவு',
        'support_desc': 'உதவ நாங்கள் எப்போதும் தயாராக இருக்கிறோம்',
        'symptoms_explanation': 'அறிகுறிகள் & காட்சி விளக்கம்',
        'total_bookings_desc': 'அனைத்து நேர முன்பதிவுகள்',
        'total_due': 'மொத்த நிலுவை',
        'total_vehicles_desc': 'பதிவுசெய்யப்பட்ட வாகனங்கள்',
        'type_question_placeholder': 'உங்கள் விவசாய கேள்வியை இங்கே தட்டச்சு செய்யவும்...',
        'upload_another_image': 'மற்றொரு படத்தைப் பதிவேற்றவும்',
        'upload_leaf_tip': 'சிறந்த முடிவுகளுக்கு, பாதிக்கப்பட்ட பகுதி தெளிவாகத் தெரியும் வகையில் நல்ல பகல் வெளிச்சத்தில் ஒரு இலையைப் புகைப்படம் எடுக்கவும்.',
        'vehicle': 'வாகனம்',
        'vehicle_col': 'வாகனம்',
        'view_all_bookings': 'வாகன பட்டியல்',
        'wide_range': 'பரந்த வாகன வரம்பு',
        'wide_range_desc': 'சிறிய வாகனங்கள் முதல் கனரக வாகனங்கள் வரை',   'language': 'மொழி',
              'english': 'English',
              'telugu': 'తెలుగు',
              'tamil': 'தமிழ்',
              'hindi': 'हिन्दी',
              'vehicle_booking': 'வணிக வாகன முன்பதிவு',
              'tagline': 'விவசாயிகளுக்கு • வணிகர்களுக்கு • உங்கள் சேவையில்',
              'home': 'முகப்பு',
              'vehicles': 'வாகனங்கள்',
              'available_vehicles': 'கிடைக்கும் வாகனங்கள்',
              'my_bookings': 'எனது முன்பதிவுகள்',
              'dashboard': 'டாஷ்போர்டு',
              'owner_dashboard': 'உரிமையாளர் டாஷ்போர்டு',
              'admin_dashboard': 'நிர்வாக டாஷ்போர்டு',
              'control_center': 'கட்டுப்பாட்டு மையம்',
              'security_clearance': 'பாதுகாப்பு அனுமதி',
              'farmer_ai': 'விவசாயி AI',
              'farmer_ai_assistant': 'விவசாயி AI பயிர் நோய் உதவியாளர்',
              'customer': 'வாடிக்கையாளர்',
              'customer_login': 'வாடிக்கையாளர் உள்நுழைவு',
              'owner_login': 'வாகன உரிமையாளர் உள்நுழைவு',
              'admin_login': 'நிர்வாக உள்நுழைவு',
              'book_vehicle': 'வாகனம் முன்பதிவு செய்',
              'login': 'உள்நுழை',
              'register': 'பதிவு செய்',
              'customer_registration': 'வாடிக்கையாளர் பதிவு',
              'owner_registration': 'வாகன உரிமையாளர் பதிவு',
              'logout': 'வெளியேறு',
              'welcome': 'வரவேற்கிறோம்',
              'back_home': 'முகப்பிற்கு திரும்பு',
              'back_to_vehicles': 'வாகனங்கள் பட்டியலுக்கு திரும்பு',
              'back_to_dashboard': 'டாஷ்போர்டிற்கு திரும்பு',
              'back_to_public': 'பொது தளத்திற்கு திரும்பு',
              'mobile_number': 'கைபேசி எண்',
              'customer_name': 'வாடிக்கையாளர் பெயர்',
              'owner_name': 'உரிமையாளர் பெயர்',
              'registered_email': 'பதிவுசெய்த மின்னஞ்சல்',
              'email_address': 'மின்னஞ்சல் முகவரி',
              'email': 'மின்னஞ்சல்',
              'phone': 'தொலைபேசி',
              'phone_number': 'தொலைபேசி எண்',
              'password': 'கடவுச்சொல்',
              'confirm_password': 'கடவுச்சொல்லை உறுதிப்படுத்துக',
              'forgot_password': 'கடவுச்சொல் மறந்துவிட்டதா?',
              'reset_password': 'கடவுச்சொல்லை மீட்டமை',
              'set_new_password': 'புதிய கடவுச்சொல்லை அமைக்கவும்',
              'new_password': 'புதிய கடவுச்சொல்',
              'send_reset_link': 'மீட்டமைப்பு இணைப்பை அனுப்புக',
              'back_to_customer_login': 'வாடிக்கையாளர் உள்நுழைவுக்கு திரும்பு',
              'enter_registered_email': 'உங்கள் பதிவுசெய்த மின்னஞ்சலை உள்ளிடவும்.',
              'login_mobile': 'உங்கள் கைபேசி எண்ணைப் பயன்படுத்தி உள்நுழையவும்.',
              'create_account': 'உங்கள் கணக்கை உருவாக்கவும்',
              'choose_account_type': 'வணிக வாகன முன்பதிவை எவ்வாறு பயன்படுத்த விரும்புகிறீர்கள் என்பதைத் '
                                     'தேர்ந்தெடுக்கவும்',
              'for_customers': 'விவசாயிகள் மற்றும் வணிக வாடிக்கையாளர்களுக்கு',
              'for_owners': 'வணிக வாகன உரிமையாளர்களுக்கு',
              'customer_desc': 'உங்கள் போக்குவரத்து தேவைகளுக்கு நம்பகமான வணிக வாகனங்களை கண்டறிந்து முன்பதிவு '
                               'செய்யுங்கள்.',
              'owner_desc': 'உங்கள் வணிக வாகனங்களை பதிவுசெய்து வாடிக்கையாளர்களிடமிருந்து முன்பதிவு கோரிக்கைகளைப் '
                            'பெறுங்கள்.',
              'register_as_customer': 'வாடிக்கையாளராக பதிவு செய்க',
              'customer_sign_in': 'வாடிக்கையாளர் உள்நுழைவு',
              'register_as_owner': 'வாகன உரிமையாளராக பதிவு செய்க',
              'owner_sign_in': 'வாகன உரிமையாளர் உள்நுழைவு',
              'new_vehicle_owner': 'புதிய வாகன உரிமையாளரா?',
              'already_an_owner': 'ஏற்கனவே உரிமையாளர் கணக்கு உள்ளதா?',
              'already_have_account': 'ஏற்கனவே கணக்கு உள்ளதா?',
              'new_customer': 'புதிய வாடிக்கையாளரா?',
              'sign_in_here': 'இங்கே உள்நுழையவும்',
              'register_here': 'இங்கே பதிவு செய்யவும்',
              'enter_owner_name_placeholder': 'உரிமையாளர் அல்லது வணிகப் பெயரை உள்ளிடவும்',
              'enter_phone_placeholder': '10 இலக்க கைபேசி எண்ணை உள்ளிடவும்',
              'enter_owner_email_placeholder': 'owner@example.com (உள்நுழைவு மற்றும் அறிவிப்புகளுக்கு)',
              'enter_password_placeholder': 'கடவுச்சொல்லை உள்ளிடவும் (குறைந்தது 8 எழுத்துக்கள்)',
              'enter_admin_username': 'நிர்வாக பயனர் பெயரை உள்ளிடவும்',
              'enter_admin_password': 'நிர்வாக கடவுச்சொல்லை உள்ளிடவும்',
              'admin_login_desc': 'தள மேலாண்மை மற்றும் செயல்பாடுகளை அணுக உள்நுழையவும்.',
              'admin_copyright': '© 2026 வணிக வாகன முன்பதிவு • நிர்வாக அமைப்புகள்',
              'heavy_vehicles_title': 'வளமான எதிர்காலத்திற்கான கனரக வாகனங்கள்',
              'book_vehicles_ease': 'எளிதாக வணிக வாகனங்களை முன்பதிவு செய்யுங்கள்',
              'hero_subtitle': 'விவசாயிகளையும் வணிகர்களையும் நம்பகமான வாகனங்களுடன் இணைக்கிறோம்.',
              'explore_vehicles': 'வாகனங்களை ஆராயுங்கள்',
              'try_farmer_ai': 'விவசாயி AI-யை முயற்சிக்கவும்',
              'smart_farming_support': 'ஸ்மார்ட் விவசாய உதவி',
              'how_it_works': 'எவ்வாறு செயல்படுகிறது',
              'choose_vehicle': 'வாகனத்தை தேர்வு செய்க',
              'select_date': 'தேதியை தேர்வு செய்க',
              'confirm': 'உறுதிப்படுத்துக',
              'vehicle_types': 'வாகன வகைகள்',
              'tractor': 'டிராக்டர்',
              'mini_truck': 'மினி டிரக்',
              'pickup': 'பிக்கப்',
              'lorry': 'லாரி',
              'goods_auto': 'சரக்கு ஆட்டோ',
              'tractor_desc': 'விவசாய மற்றும் சரக்கு போக்குவரத்திற்கு ஏற்றது.',
              'mini_truck_desc': 'சிறிய மற்றும் நடுத்தர சுமைகளுக்கு ஏற்றது.',
              'pickup_desc': 'உள்ளூர் போக்குவரத்திற்கு வசதியானது.',
              'lorry_desc': 'பெரிய சுமைகளை ஏற்றிச் செல்ல சிறந்தது.',
              'goods_auto_desc': 'விரைவான உள்ளூர் சரக்கு போக்குவரத்திற்கு சிறந்தது.',
              'simple_fast_easy': 'எளிமை • வேகம் • பாதுகாப்பு',
              'transportation_message': 'உங்கள் தேவைக்கேற்ப சரியான வாகனத்தை உடனே முன்பதிவு செய்யுங்கள்.',
              'footer_message': 'அனைவருக்கும் எளிதான போக்குவரத்து சேவை.',
              'vehicle_type': 'வாகன வகை',
              'location': 'இடம்',
              'from': 'புறப்படும் இடம்',
              'to': 'சேருமிடம்',
              'rent': 'வாடகை',
              'contact': 'தொடர்பு',
              'book_now': 'இப்போதே முன்பதிவு செய்க',
              'no_vehicles': 'வாகனங்கள் எதுவும் கிடைக்கவில்லை',
              'no_vehicles_message': 'தற்போது முன்பதிவு செய்ய வாகனங்கள் எதுவும் இல்லை.',
              'search_placeholder': 'பெயர், வழி அல்லது இடத்தை தேடுங்கள்',
              'location_placeholder': 'இடம் அல்லது நகரம்',
              'all_vehicle_types': 'அனைத்து வாகன வகைகள்',
              'min_rent': 'குறைந்தபட்ச வாடகை (₹)',
              'max_rent': 'அதிகபட்ச வாடகை (₹)',
              'apply_filters': 'வடிகட்டிகளைப் பயன்படுத்து',
              'reset_filters': 'வடிகட்டிகளை மீட்டமை',
              'view_details': 'விவரங்களைக் காண்க',
              'book_this_vehicle': 'இந்த வாகனத்தை முன்பதிவு செய்க',
              'available_routes_rent': 'கிடைக்கும் வழிகள் & நிலையான வாடகை',
              'base_location': 'முதன்மை இருப்பிடம்',
              'current_location_label': 'தற்போதைய / முதன்மை இடம்',
              'capacity_weight': 'சுமை திறன்',
              'availability_status': 'கிடைக்கும் நிலை',
              'available_ready': 'கிடைக்கிறது (முன்பதிவுக்கு தயார்)',
              'route_label': 'பயண வழி',
              'fixed_route_rent_label': 'நிலையான வழி வாடகை',
              'booking_date': 'முன்பதிவு தேதி',
              'confirm_booking': 'முன்பதிவை உறுதிப்படுத்துக',
              'status': 'நிலை',
              'accepted': 'ஏற்றுக்கொள்ளப்பட்டது',
              'rejected': 'நிராகரிக்கப்பட்டது',
              'pending': 'நிலுவையில் உள்ளது',
              'paid': 'செலுத்தப்பட்டது',
              'pay_now': 'இப்போது செலுத்தவும்',
              'no_bookings': 'முன்பதிவுகள் இல்லை',
              'no_bookings_message': 'நீங்கள் இன்னும் எந்த வாகனத்தையும் முன்பதிவு செய்யவில்லை.',
              'booking_id': 'முன்பதிவு எண்',
              'customer_booking_requests': 'வாடிக்கையாளர் முன்பதிவு கோரிக்கைகள்',
              'accept': 'ஏற்றுக்கொள்',
              'reject': 'நிராகரி',
              'no_action_needed': 'எந்த நடவடிக்கையும் தேவையில்லை',
              'rate_service': 'மதிப்பீடு செய்க',
              'rate_booking': 'உங்கள் முன்பதிவை மதிப்பிடுங்கள்',
              'rate_booking_title': '⭐ உங்கள் முன்பதிவை மதிப்பிடுங்கள்',
              'review_optional': 'கருத்து (விருப்பத்தேர்வு)',
              'review_placeholder': 'வாகனம் மற்றும் உரிமையாளர் சேவை எவ்வாறு இருந்தது?',
              'save_rating': 'மதிப்பீட்டை சேமி',
              'rating': 'மதிப்பீடு',
              'booking_success_title': 'முன்பதிவு கோரிக்கை சமர்ப்பிக்கப்பட்டது',
              'booking_success_desc': 'உங்கள் முன்பதிவு உரிமையாளர் உறுதிப்படுத்தலுக்கு காத்திருக்கிறது.',
              'waiting_owner_confirm': 'உரிமையாளர் ஒப்புதலுக்காக காத்திருக்கிறது.',
              'view_my_bookings': 'எனது முன்பதிவுகளைக் காண்க',
              'browse_vehicles': 'வாகனங்களை உலாவுங்கள்',
              'track_bookings_desc': 'ஒவ்வொரு முன்பதிவையும் கண்காணித்து சேவையை மதிப்பிடுங்கள்.',
              'booking_confirmed_badge': 'முன்பதிவு உறுதி செய்யப்பட்டது',
              'add_vehicle': 'வாகனம் சேர்க்க',
              'add_vehicle_title': '🚚 வாகனம் சேர்க்க',
              'add_route': 'வழித்தடத்தை சேர்க்க',
              'add_route_rent': '+ வழித்தடம் & வாடகையை சேர்க்க',
              'add_route_subtitle': 'வாடிக்கையாளர்கள் எளிதில் முன்பதிவு செய்ய உங்கள் வாகனங்களுக்கு நிலையான வாடகையை '
                                    'அமைக்கவும்.',
              'route_details': 'வழித்தட விவரங்கள்',
              'rent_details': 'வாடகை விவரங்கள்',
              'additional_info': 'கூடுதல் தகவல்கள்',
              'select_vehicle': 'வாகனத்தை தேர்வு செய்க',
              'choose_vehicle_prompt': '-- உங்கள் வாகனத்தை தேர்வு செய்க --',
              'from_location': 'புறப்படும் இடம் / Origin',
              'to_location': 'சேருமிடம் / Destination',
              'fixed_route_rent': 'வழித்தட நிலையான வாடகை (₹)',
              'save_route_rent': 'வழித்தடம் & வாடகையை சேமிக்க',
              'save_vehicle': 'வாகனத்தை சேமிக்க',
              'save_changes': 'மாற்றங்களைச் சேமிக்கவும்',
              'cancel': 'ரத்து செய்',
              'route_added_success': 'வழித்தடம் மற்றும் வாடகை வெற்றிகரமாக சேர்க்கப்பட்டது! ✅',
              'add_another_route': 'மற்றொரு வழியைச் சேர்க்கவும்',
              'my_vehicles': 'எனது வாகனங்கள்',
              'edit_vehicle': 'வாகனத்தை திருத்து',
              'edit_vehicle_title': '✏️ வாகனத்தை திருத்து',
              'vehicle_name': 'வாகனத்தின் பெயர்',
              'contact_number': 'தொடர்பு எண்',
              'update_vehicle': 'வாகனத்தை புதுப்பிக்கவும்',
              'delete_vehicle': 'வாகனத்தை நீக்கு',
              'active_vehicles': 'செயலில் உள்ள வாகனங்கள்',
              'total_bookings': 'மொத்த முன்பதிவுகள்',
              'total_vehicles': 'மொத்த வாகனங்கள்',
              'pending_action': 'நிலுவை நடவடிக்கைகள்',
              'accepted_bookings': 'ஏற்றுக்கொள்ளப்பட்ட முன்பதிவுகள்',
              'quick_actions_title': 'விரைவு மேலாண்மை',
              'manage_fleet': 'வாகனங்களை நிர்வகிக்க',
              'manage_fleet_desc': 'இருக்கும் வாகனங்களை பார்க்க, திருத்த அல்லது நீக்க',
              'manage_routes_desc': 'வாடிக்கையாளர்களுக்கான நிலையான வழி விலைகளை அமைக்கவும்',
              'owner_profile_title': 'உரிமையாளர் சுயவிவரம்',
              'account_status': 'கணக்கு நிலை',
              'active_fleet': 'செயலில் உள்ள வாகனங்கள்',
              'no_bookings_title': 'முன்பதிவு கோரிக்கைகள் எதுவும் இல்லை',
              'no_bookings_desc': 'உங்கள் வாகனங்களுக்கான வாடிக்கையாளர் முன்பதிவுகள் இங்கே தோன்றும்.',
              'choose_vehicle_fleet_hint': 'உங்கள் பதிவுசெய்யப்பட்ட வாகனங்களில் ஒன்றைத் தேர்ந்தெடுக்கவும்.',
              'starting_point_hint': 'புறப்படும் நகரம், கிராமம் அல்லது ஏற்றும் இடம்.',
              'delivery_point_hint': 'இறக்கும் சந்தை, கிடங்கு அல்லது இலக்கு நகரம்.',
              'total_fixed_rent_hint': 'இந்த ஒருவழி பயணத்திற்கான மொத்த நிலையான வாடகை.',
              'route_open_notice': 'இந்த வழித்தடம் உடனடியாக வாடிக்கையாளர் முன்பதிவுக்கு திறக்கப்படும்.',
              'transparent_pricing_title': 'வெளிப்படையான வழித்தட விலை நிர்ணயம்',
              'source_placeholder': 'உதாரணம்: தாடிபத்ரி',
              'destination_placeholder': 'உதாரணம்: அனந்தபூர்',
              'rent_placeholder': 'உதாரணம்: 2500',
              'vehicle_name_example': 'உதாரணம்: டாடா ஏஸ்',
              'location_example': 'உதாரணம்: காஞ்சிபுரம்',
              'demo_payment': 'டெமோ கட்டணம்',
              'demo_payment_banner': 'டெமோ கட்டணம் — உண்மையான பணம் கழிக்கப்படாது',
              'secure_demo_payment': 'பாதுகாப்பான டெமோ கட்டணம்',
              'demo_payment_disclaimer': 'இது திட்ட டெமோ பரிவர்த்தனை மட்டுமே. உண்மையான பணம் வசூலிக்கப்படாது.',
              'amount_paid': 'செலுத்தப்பட்ட தொகை',
              'payment_method': 'கட்டண முறை',
              'cardholder_name': 'கார்டு வைத்திருப்பவர் பெயர்',
              'upi_id': 'UPI ID',
              'enter_cardholder_placeholder': 'டெமோ கார்டு வைத்திருப்பவர் பெயரை உள்ளிடவும்',
              'enter_upi_placeholder': 'demo@upi',
              'pay_amount_btn': 'தொகையை செலுத்தவும்',
              'payment_successful': 'கட்டணம் வெற்றிகரமாக செலுத்தப்பட்டது',
              'payment_success_desc': 'இந்த டெமோ கட்டணம் வெற்றிகரமாக நிறைவடைந்து கணினியில் பதிவு செய்யப்பட்டுள்ளது.',
              'transaction_reference': 'பரிவர்த்தனை குறிப்பு எண்',
              'date': 'தேதி',
              'view_receipt': 'ரசீதை பார்க்க',
              'download_receipt': 'ரசீதை பதிவிறக்கம் செய்ய',
              'view_booking': 'முன்பதிவை பார்க்க',
              'upload_leaf_image': 'இலை படத்தை பதிவேற்றவும்',
              'analyze_leaf': 'இலையை பகுப்பாய்வு செய்க',
              'drag_leaf_image': 'இலை புகைப்படத்தை இங்கே தேர்வு செய்யவும் அல்லது இழுத்து விடவும்',
              'image_format_limit': 'JPG, JPEG, PNG அல்லது WEBP • அதிகபட்சம் 10 MB',
              'ai_assistant_intro': 'பயிர் இலையின் தெளிவான புகைப்படத்தை பதிவேற்றி, சாத்தியமான நோய், அறிகுறிகள் மற்றும் '
                                    'தடுப்பு வழிகாட்டுதலை AI மூலம் பெறுங்கள்.',
              'photo_tips': 'சிறந்த முடிவுகளுக்கு, பாதிக்கப்பட்ட பகுதியை நல்ல வெளிச்சத்தில் தெளிவாக புகைப்படம் '
                            'எடுக்கவும்.',
              'analysis_result': 'பகுப்பாய்வு முடிவு',
              'ai_assisted_notice': 'பதிவேற்றப்பட்ட படத்தின் அடிப்படையில் AI உதவியுடன் பெறப்பட்ட முடிவு.',
              'possible_disease': 'சாத்தியமான நோய்',
              'crop': 'பயிர்',
              'confidence': 'நம்பகத்தன்மை',
              'symptoms': 'அறிகுறிகள்',
              'possible_causes': 'சாத்தியமான காரணங்கள்',
              'treatment_management': 'சிகிச்சை / மேலாண்மை',
              'prevention': 'தடுப்பு முறைகள்',
              'immediate_steps': 'உடனடி நடவடிக்கைகள்',
              'expert_advice': 'நிபுணர் ஆலோசனை',
              'ask_ai_assistant': 'AI உதவியாளரிடம் கேளுங்கள்',
              'followup_prompt_desc': 'பகுப்பாய்வு செய்யப்பட்ட இலை, அறிகுறிகள் அல்லது பயிர் பராமரிப்பு பற்றி கேள்விகள் '
                                      'கேட்கவும்.',
              'safety_notice': 'பாதுகாப்பு அறிவிப்பு',
              'safety_notice_desc': 'இந்த AI கல்வி வழிகாட்டுதலை மட்டுமே வழங்குகிறது. பூச்சிக்கொல்லி பயன்பாட்டிற்கு '
                                    'உள்ளூர் விவசாய அதிகாரிகளை அணுகவும்.',
              'chat_placeholder': 'உங்கள் விவசாய கேள்வியை இங்கே தட்டச்சு செய்க...',
              'send': 'அனுப்பு',
              'q_disease': 'இது என்ன நோய்?',
              'q_todo': 'நான் இப்போது என்ன செய்ய வேண்டும்?',
              'q_prevent': 'இதை எவ்வாறு தடுப்பது?',
              'q_telugu': 'தெலுங்கில் விளக்குங்கள்',
              'q_tamil': 'தமிழில் விளக்குங்கள்',
              'q_hindi': 'இந்தியில் விளக்குங்கள்',
              'analyzing': 'இலை படத்தை பகுப்பாய்வு செய்கிறது...',
              'ai_thinking': 'AI வழிகாட்டுதலை உருவாக்குகிறது...',
              'no_leaf_analyzed_yet': 'AI நோயறிதலைக் காண மேலே ஒரு இலை புகைப்படத்தை பதிவேற்றவும்.',
              'admin': 'நிர்வாகம்',
              'customers': 'வாடிக்கையாளர்கள்',
              'owners': 'உரிமையாளர்கள்',
              'payments': 'கட்டணங்கள்',
              'ratings_reviews': 'மதிப்பீடுகள் & மதிப்புரைகள்',
              'overview': 'மேலோட்டம்',
              'enterprise_operations': 'நிறுவன செயல்பாடுகள்',
              'live_db_connected': 'நேரலை தரவுத்தளம் இணைக்கப்பட்டுள்ளது',
              'admin_management_desc': 'நேரலை நிர்வாக மேலாண்மை மற்றும் செயல்பாட்டு பதிவுகள்.',
              'recent_bookings': 'சமீபத்திய முன்பதிவுகள்',
              'no_bookings_found': 'முன்பதிவுகள் எதுவும் இல்லை.',
              'no_records_found': 'பதிவுகள் எதுவும் இல்லை.',
              'search': 'தேடு',
              'filter': 'வடிகட்டு',
              'all_statuses': 'அனைத்து நிலைகளும்',
              'active': 'செயலில்',
              'inactive': 'செயலற்றது',
              'activate': 'செயல்படுத்து',
              'deactivate': 'செயலிழக்கச் செய்',
              'update_status': 'நிலையை புதுப்பிக்கவும்',
              'read_only_view': 'பார்வைக்கான நிர்வாகப் பக்கம் மட்டும்.',
              'registration_success': 'பதிவு வெற்றிகரமாக முடிந்தது! இப்போது உள்நுழையலாம்.',
              'login_successful': 'உள்நுழைவு வெற்றிகரமானது.',
              'logout_successful': 'நீங்கள் வெளியேறிவிட்டீர்கள்.',
              'invalid_credentials': 'மின்னஞ்சல் அல்லது கடவுச்சொல் தவறானது.',
              'invalid_customer_login': 'கைபேசி எண் அல்லது கடவுச்சொல் தவறானது.',
              'invalid_owner_login': 'மின்னஞ்சல் அல்லது கடவுச்சொல் தவறானது.',
              'invalid_admin_login': 'நிர்வாக பயனர் பெயர் அல்லது கடவுச்சொல் தவறானது.',
              'email_already_registered': 'இந்த மின்னஞ்சல் ஏற்கனவே பதிவு செய்யப்பட்டுள்ளது. வேறு மின்னஞ்சலை '
                                          'பயன்படுத்தவும் அல்லது உள்நுழையவும்.',
              'mobile_already_registered': 'இந்த கைபேசி எண் ஏற்கனவே பதிவு செய்யப்பட்டுள்ளது. வேறு எண்ணை பயன்படுத்தவும் '
                                           'அல்லது உள்நுழையவும்.',
              'valid_email_err': 'சரியான மின்னஞ்சல் முகவரியை உள்ளிடவும்.',
              'valid_phone_err': 'சரியான 10 இலக்க கைபேசி எண்ணை உள்ளிடவும்.',
              'password_length_err': 'கடவுச்சொல் 8 முதல் 128 எழுத்துக்கள் வரை இருக்க வேண்டும்.',
              'passwords_dont_match': 'கடவுச்சொற்கள் பொருந்தவில்லை.',
              'booking_date_past_err': 'கடந்த தேதிகளில் முன்பதிவு செய்ய முடியாது.',
              'select_valid_date_err': 'சரியான முன்பதிவு தேதியைத் தேர்ந்தெடுக்கவும்.',
              'select_valid_route_err': 'முன்பதிவு செய்வதற்கு முன் சரியான வழியைத் தேர்ந்தெடுக்கவும்.',
              'duplicate_booking_err': 'இந்த தேதியில் இந்த வாகனத்திற்கு ஏற்கனவே உங்களிடம் முன்பதிவு கோரிக்கை உள்ளது.',
              'booking_created_success': 'முன்பதிவு கோரிக்கை வெற்றிகரமாக சமர்ப்பிக்கப்பட்டது! உரிமையாளர் ஒப்புதலுக்கு '
                                         'காத்திருக்கிறது.',
              'booking_status_updated': 'முன்பதிவு நிலை புதுப்பிக்கப்பட்டது.',
              'booking_status_update_err': 'முன்பதிவு நிலையை புதுப்பிக்க முடியவில்லை. மீண்டும் முயற்சிக்கவும்.',
              'only_pending_can_change': 'நிலுவையில் உள்ள முன்பதிவுகளின் நிலையை மட்டுமே மாற்ற முடியும்.',
              'select_rating_err': '1 முதல் 5 நட்சத்திரங்கள் வரை மதிப்பீட்டைத் தேர்ந்தெடுக்கவும்.',
              'rating_saved': 'நன்றி! உங்கள் மதிப்பீடு மற்றும் கருத்து சேமிக்கப்பட்டது.',
              'demo_payment_success': 'டெமோ கட்டணம் வெற்றிகரமாக முடிந்தது. உறுதிப்படுத்தல் மின்னஞ்சல் அனுப்பப்பட்டது.',
              'demo_payment_success_no_mail': 'டெமோ கட்டணம் வெற்றிகரமாக முடிந்தது. கட்டணம் பதிவு செய்யப்பட்டது.',
              'demo_payment_failed': 'டெமோ கட்டண செயல்முறை தோல்வியடைந்தது. மீண்டும் முயற்சிக்கவும்.',
              'valid_payment_method_err': 'சரியான கட்டண முறையைத் தேர்ந்தெடுக்கவும்.',
              'cardholder_name_err': 'டெமோ கார்டு வைத்திருப்பவர் பெயரை உள்ளிடவும்.',
              'upi_id_err': 'கட்டணத்திற்கு டெமோ UPI ID-ஐ உள்ளிடவும்.',
              'vehicle_added_success': 'வாகனம் வெற்றிகரமாக சேர்க்கப்பட்டது! இப்போது வழிகளையும் வாடகையையும் அமைக்கலாம்.',
              'vehicle_updated_success': 'வாகன விவரங்கள் வெற்றிகரமாக புதுப்பிக்கப்பட்டன.',
              'vehicle_deleted_success': 'வாகனம் வெற்றிகரமாக நீக்கப்பட்டது.',
              'access_denied': 'அனுமதி மறுக்கப்பட்டது: உங்கள் சொந்த வாகனங்களையும் வழிகளையும் மட்டுமே நிர்வகிக்க '
                               'முடியும்.',
              'invalid_csrf_token': 'தவறான CSRF டோக்கன். பக்கத்தை புதுப்பித்து மீண்டும் முயற்சிக்கவும்.',
              'error_404_title': '404 - பக்கம் கிடைக்கவில்லை',
              'error_404_desc': 'நீங்கள் தேடும் பக்கம் இல்லை அல்லது நகர்த்தப்பட்டுள்ளது.',
              'error_500_title': '500 - சேவையக பிழை',
              'error_500_desc': 'எதிர்பாராத பிழை ஏற்பட்டது. சிறிது நேரம் கழித்து மீண்டும் முயற்சிக்கவும்.'},
    'hi': {
        'admin_login_badge': 'व्यवस्थापक लॉगिन',
        'username': 'उपयोगकर्ता नाम',
        'accepted_bookings_desc': 'पुष्टीकृत बुकिंग',
        'action': 'कार्रवाई',
        'action_col': 'कार्रवाई',
        'active_vehicles_desc': 'सक्रिय मार्गों के साथ तैयार',
        'ai_assisted_result_sub': 'अपलोड की गई छवि के आधार पर AI-सहायता प्राप्त परिणाम।',
        'ai_chat_welcome': 'नमस्ते! ऊपर एक पत्ती की छवि अपलोड करें, फिर परिणाम के बारे में मुझसे प्रश्न पूछें।',
        'ai_disclaimer_note': 'आपकी छवि का विश्लेषण केवल अनुरोधित AI सहायता प्रदान करने के लिए किया जाता है। परिणाम को गारंटीकृत निदान के रूप में उपयोग न करें।',
        'ai_disclaimer_short': 'AI परिणाम केवल सूचनात्मक हैं और कृषि पेशेवरों के साथ सत्यापित किए जाने चाहिए।',
        'ai_service_not_configured': 'AI सेवा अभी कॉन्फ़िगर नहीं की गई है। अपनी .env फ़ाइल में AI_API_KEY जोड़ें, फिर फ्लास्क को पुनरारंभ करें।',
        'amount': 'राशि',
        'ask_ai_sub': 'विश्लेषण की गई पत्ती, लक्षण, रोकथाम या सामान्य फसल देखभाल के बारे में पूछें।',
        'ask_followup_questions': 'अनुवर्ती प्रश्न पूछें',
        'booking_id_col': 'बुकिंग ID',
        'chip_disease_label': 'यह कौन सी बीमारी है?',
        'chip_disease_query': 'इस पत्ती में कौन सी बीमारी हो सकती है?',
        'chip_lang_label': 'हिंदी में समझाइए',
        'chip_lang_query': 'इसे हिंदी में विस्तार से और सरल शब्दों में समझाइए।',
        'chip_prevent_label': 'मैं इसे कैसे रोक सकता हूँ?',
        'chip_prevent_query': 'मैं इस समस्या को फैलने से कैसे रोक सकता हूँ?',
        'chip_steps_label': 'अब मुझे क्या करना चाहिए?',
        'chip_steps_query': 'इस फसल समस्या के प्रबंधन के लिए अब मुझे क्या करना चाहिए?',
        'choose_or_drag_photo': 'यहाँ एक पत्ती की तस्वीर चुनें या खींचें',
        'contact_expert': 'विशेषज्ञ से कब संपर्क करें',
        'demo_card': 'डेमो कार्ड',
        'demo_simulation_notice': 'केवल सिमुलेशन: यह डेमो भुगतान किसी वास्तविक कार्ड, बैंक या UPI डेटा को संसाधित नहीं करता है।',
        'demo_upi': 'डेमो UPI',
        'demo_upi_id': 'डेमो UPI ID',
        'enter_cardholder_name': 'डेमो कार्डधारक का नाम दर्ज करें',
        'enter_demo_upi_id': 'demo@upi',
        'farmer_ai_desc': 'एक स्पष्ट फसल-पत्ती की तस्वीर अपलोड करें और संभावित बीमारी, दिखाई देने वाले लक्षण, सुरक्षित प्रबंधन कदम और रोकथाम मार्गदर्शन प्राप्त करें।',
        'file_format_limit': 'JPG, JPEG, PNG या WEBP • अधिकतम 10 MB',
        'fleet_management': 'फ्लीट प्रबंधन',
        'fleet_overview': 'फ्लीट अवलोकन',
        'id': 'ID',
        'image_based_analysis': 'छवि आधारित विश्लेषण',
        'not_specified': 'निर्दिष्ट नहीं',
        'owner_hero_subtitle': 'अपने वाहनों, मार्गों का प्रबंधन करें और ग्राहक बुकिंग अनुरोधों का जवाब दें।',
        'owner_tagline': 'किसानों के लिए • व्यापारियों के लिए • आपकी सेवा में',
        'pay': 'भुगतान करें',
        'payment_col': 'भुगतान',
        'payment_summary': 'भुगतान सारांश',
        'pending_actions_desc': 'पुष्टि की आवश्यकता है',
        'pending_review_banner': 'बुकिंग अनुरोध(ओं) पर आपकी तत्काल समीक्षा और कार्रवाई की आवश्यकता है।',
        'pending_review_hint': 'नीचे ग्राहक, मार्ग, तिथि और किराए की समीक्षा करें, फिर स्वीकार या अस्वीकार करें पर क्लिक करें।',
        'processing': 'प्रसंस्करण हो रहा है...',
        'processing_demo_payment': 'डेमो भुगतान संसाधित हो रहा है...',
        'profile': 'मालिक प्रोफाइल',
        'ready_when_you_are': 'जब आप तैयार हों',
        'route_col': 'मार्ग',
        'route_pricing': 'मार्ग आधारित मूल्य निर्धारण',
        'route_pricing_desc': 'पारदर्शी और उचित दरें',
        'secure_desc': 'आपका विश्वास हमारी प्राथमिकता है',
        'secure_reliable': 'सुरक्षित और विश्वसनीय',
        'sidebar_dashboard': 'डैशबोर्ड',
        'start_moving': 'अपनी अगली ढुलाई शुरू करें।',
        'support_247': '24/7 सहायता',
        'support_desc': 'हम हमेशा मदद के लिए यहाँ हैं',
        'symptoms_explanation': 'लक्षण और दृश्य व्याख्या',
        'total_bookings_desc': 'अब तक की सभी बुकिंग',
        'total_due': 'कुल देय राशि',
        'total_vehicles_desc': 'पंजीकृत वाहन',
        'type_question_placeholder': 'अपना खेती संबंधी प्रश्न यहाँ टाइप करें...',
        'upload_another_image': 'दूसरी छवि अपलोड करें',
        'upload_leaf_tip': 'सर्वोत्तम परिणामों के लिए, प्रभावित क्षेत्र को स्पष्ट रूप से दिखाते हुए अच्छी रोशनी में एक पत्ती की तस्वीर लें।',
        'vehicle': 'वाहन',
        'vehicle_col': 'वाहन',
        'view_all_bookings': 'फ्लीट देखें',
        'wide_range': 'वाहनों की विस्तृत श्रृंखला',
        'wide_range_desc': 'छोटे से लेकर भारी वाहनों तक',   'language': 'भाषा',
              'english': 'English',
              'telugu': 'తెలుగు',
              'tamil': 'தமிழ்',
              'hindi': 'हिन्दी',
              'vehicle_booking': 'व्यावसायिक वाहन बुकिंग',
              'tagline': 'किसानों के लिए • व्यापारियों के लिए • आपकी सेवा में',
              'home': 'होम',
              'vehicles': 'वाहन',
              'available_vehicles': 'उपलब्ध वाहन',
              'my_bookings': 'मेरी बुकिंग्स',
              'dashboard': 'डैशबोर्ड',
              'owner_dashboard': 'मालिक डैशबोर्ड',
              'admin_dashboard': 'व्यवस्थापक डैशबोर्ड',
              'control_center': 'नियंत्रण केंद्र',
              'security_clearance': 'सुरक्षा स्वीकृति',
              'farmer_ai': 'किसान AI',
              'farmer_ai_assistant': 'किसान AI फसल रोग सहायक',
              'customer': 'ग्राहक',
              'customer_login': 'ग्राहक लॉगिन',
              'owner_login': 'वाहन मालिक लॉगिन',
              'admin_login': 'व्यवस्थापक लॉगिन',
              'book_vehicle': 'वाहन बुक करें',
              'login': 'लॉगिन',
              'register': 'पंजीकरण',
              'customer_registration': 'ग्राहक पंजीकरण',
              'owner_registration': 'वाहन मालिक पंजीकरण',
              'logout': 'लॉगआउट',
              'welcome': 'स्वागत है',
              'back_home': 'होम पर वापस जाएं',
              'back_to_vehicles': 'वाहन सूची पर वापस जाएं',
              'back_to_dashboard': 'डैशबोर्ड पर वापस जाएं',
              'back_to_public': 'सार्वजनिक साइट पर वापस जाएं',
              'mobile_number': 'मोबाइल नंबर',
              'customer_name': 'ग्राहक का नाम',
              'owner_name': 'मालिक का नाम',
              'registered_email': 'पंजीकृत ईमेल',
              'email_address': 'ईमेल पता',
              'email': 'ईमेल',
              'phone': 'फ़ोन',
              'phone_number': 'फ़ोन नंबर',
              'password': 'पासवर्ड',
              'confirm_password': 'पासवर्ड की पुष्टि करें',
              'forgot_password': 'पासवर्ड भूल गए?',
              'reset_password': 'पासवर्ड रीसेट करें',
              'set_new_password': 'नया पासवर्ड सेट करें',
              'new_password': 'नया पासवर्ड',
              'send_reset_link': 'रीसेट लिंक भेजें',
              'back_to_customer_login': 'ग्राहक लॉगिन पर वापस जाएं',
              'enter_registered_email': 'अपना पंजीकृत ईमेल पता दर्ज करें।',
              'login_mobile': 'अपने मोबाइल नंबर से लॉगिन करें।',
              'create_account': 'अपना खाता बनाएं',
              'choose_account_type': 'चुनें कि आप व्यावसायिक वाहन बुकिंग का उपयोग कैसे करना चाहते हैं',
              'for_customers': 'किसानों और व्यापारिक ग्राहकों के लिए',
              'for_owners': 'व्यावसायिक वाहन मालिकों के लिए',
              'customer_desc': 'अपनी परिवहन आवश्यकताओं के लिए विश्वसनीय व्यावसायिक वाहन खोजें और बुक करें।',
              'owner_desc': 'अपने व्यावसायिक वाहनों को पंजीकृत करें और ग्राहकों से बुकिंग अनुरोध प्राप्त करें।',
              'register_as_customer': 'ग्राहक के रूप में पंजीकरण करें',
              'customer_sign_in': 'ग्राहक साइन इन',
              'register_as_owner': 'वाहन मालिक के रूप में पंजीकरण करें',
              'owner_sign_in': 'वाहन मालिक साइन इन',
              'new_vehicle_owner': 'नए वाहन मालिक हैं?',
              'already_an_owner': 'पहले से मालिक खाता है?',
              'already_have_account': 'क्या आपके पास पहले से एक खाता है?',
              'new_customer': 'नए ग्राहक हैं?',
              'sign_in_here': 'यहाँ साइन इन करें',
              'register_here': 'यहाँ पंजीकरण करें',
              'enter_owner_name_placeholder': 'मालिक या व्यवसाय का नाम दर्ज करें',
              'enter_phone_placeholder': '10 अंकों का मोबाइल नंबर दर्ज करें',
              'enter_owner_email_placeholder': 'owner@example.com (लॉगिन और अलर्ट के लिए)',
              'enter_password_placeholder': 'पासवर्ड दर्ज करें (न्यूनतम 8 वर्ण)',
              'enter_admin_username': 'व्यवस्थापक उपयोगकर्ता नाम दर्ज करें',
              'enter_admin_password': 'व्यवस्थापक पासवर्ड दर्ज करें',
              'admin_login_desc': 'प्रबंधन और परिचालन कार्यों के लिए साइन इन करें।',
              'admin_copyright': '© 2026 व्यावसायिक वाहन बुकिंग • प्रशासनिक प्रणाली',
              'heavy_vehicles_title': 'मजबूत कल के लिए भारी वाहन',
              'book_vehicles_ease': 'आसानी से व्यावसायिक वाहन बुक करें',
              'hero_subtitle': 'किसानों और व्यापारियों को बेहतर कल के लिए विश्वसनीय व्यावसायिक वाहनों से जोड़ना।',
              'explore_vehicles': 'वाहन देखें',
              'try_farmer_ai': 'किसान AI आज़माएं',
              'smart_farming_support': 'स्मार्ट कृषि सहायता',
              'how_it_works': 'यह कैसे काम करता है',
              'choose_vehicle': 'वाहन चुनें',
              'select_date': 'तारीख चुनें',
              'confirm': 'पुष्टि करें',
              'vehicle_types': 'वाहन के प्रकार',
              'tractor': 'ट्रैक्टर',
              'mini_truck': 'मिनी ट्रक',
              'pickup': 'पिकअप',
              'lorry': 'लॉरी',
              'goods_auto': 'गुड्स ऑटो',
              'tractor_desc': 'कृषि और माल परिवहन के लिए उपयुक्त।',
              'mini_truck_desc': 'छोटे और मध्यम भार के लिए उपयुक्त।',
              'pickup_desc': 'स्थानीय परिवहन के लिए सुविधाजनक।',
              'lorry_desc': 'बड़े भार के परिवहन के लिए उपयुक्त।',
              'goods_auto_desc': 'त्वरित स्थानीय उपज और माल परिवहन के लिए उपयुक्त।',
              'simple_fast_easy': 'सरल • तेज • सुरक्षित',
              'transportation_message': 'अपनी परिवहन आवश्यकताओं के लिए सही वाहन तुरंत बुक करें।',
              'footer_message': 'सभी के लिए सरल परिवहन सेवा।',
              'vehicle_type': 'वाहन का प्रकार',
              'location': 'स्थान',
              'from': 'प्रारंभिक स्थान',
              'to': 'गंतव्य स्थान',
              'rent': 'किराया',
              'contact': 'संपर्क',
              'book_now': 'अभी बुक करें',
              'no_vehicles': 'कोई वाहन उपलब्ध नहीं है',
              'no_vehicles_message': 'वर्तमान में बुकिंग के लिए कोई वाहन उपलब्ध नहीं है।',
              'search_placeholder': 'नाम, मार्ग या स्थान खोजें',
              'location_placeholder': 'स्थान या शहर',
              'all_vehicle_types': 'सभी वाहन प्रकार',
              'min_rent': 'न्यूनतम किराया (₹)',
              'max_rent': 'अधिकतम किराया (₹)',
              'apply_filters': 'फ़िल्टर लागू करें',
              'reset_filters': 'फ़िल्टर रीसेट करें',
              'view_details': 'विवरण देखें',
              'book_this_vehicle': 'यह वाहन बुक करें',
              'available_routes_rent': 'उपलब्ध मार्ग और निर्धारित किराया',
              'base_location': 'मूल स्थान',
              'current_location_label': 'वर्तमान / मूल स्थान',
              'capacity_weight': 'भार क्षमता',
              'availability_status': 'उपलब्धता',
              'available_ready': 'उपलब्ध (बुकिंग के लिए तैयार)',
              'route_label': 'मार्ग',
              'fixed_route_rent_label': 'निर्धारित मार्ग किराया',
              'booking_date': 'बुकिंग तिथि',
              'confirm_booking': 'बुकिंग की पुष्टि करें',
              'status': 'स्थिति',
              'accepted': 'स्वीकृत',
              'rejected': 'अस्वीकृत',
              'pending': 'लंबित',
              'paid': 'भुगतान किया गया',
              'pay_now': 'अभी भुगतान करें',
              'no_bookings': 'कोई बुकिंग नहीं',
              'no_bookings_message': 'आपने अभी तक कोई वाहन बुक नहीं किया है।',
              'booking_id': 'बुकिंग आईडी',
              'customer_booking_requests': 'ग्राहक बुकिंग अनुरोध',
              'accept': 'स्वीकार करें',
              'reject': 'अस्वीकार करें',
              'no_action_needed': 'किसी कार्रवाई की आवश्यकता नहीं',
              'rate_service': 'सेवा को रेट करें',
              'rate_booking': 'अपनी बुकिंग को रेट करें',
              'rate_booking_title': '⭐ अपनी बुकिंग को रेट करें',
              'review_optional': 'समीक्षा (वैकल्पिक)',
              'review_placeholder': 'वाहन और मालिक की सेवा कैसी थी?',
              'save_rating': 'रेटिंग सहेजें',
              'rating': 'रेटिंग',
              'booking_success_title': 'बुकिंग अनुरोध सबमिट किया गया',
              'booking_success_desc': 'आपकी बुकिंग मालिक की पुष्टि के लिए लंबित है।',
              'waiting_owner_confirm': 'मालिक की पुष्टि की प्रतीक्षा कर रहा है।',
              'view_my_bookings': 'मेरी बुकिंग देखें',
              'browse_vehicles': 'वाहन ब्राउज़ करें',
              'track_bookings_desc': 'प्रत्येक बुकिंग को ट्रैक करें और सेवा को रेट करें।',
              'booking_confirmed_badge': 'बुकिंग की पुष्टि हो गई',
              'add_vehicle': 'वाहन जोड़ें',
              'add_vehicle_title': '🚚 वाहन जोड़ें',
              'add_route': 'मार्ग जोड़ें',
              'add_route_rent': '+ मार्ग और किराया जोड़ें',
              'add_route_subtitle': 'ग्राहकों द्वारा तुरंत बुकिंग के लिए अपने वाहनों के लिए निश्चित मार्ग मूल्य '
                                    'निर्धारित करें।',
              'route_details': 'मार्ग विवरण',
              'rent_details': 'किराया विवरण',
              'additional_info': 'अतिरिक्त जानकारी',
              'select_vehicle': 'वाहन चुनें',
              'choose_vehicle_prompt': '-- अपना वाहन चुनें --',
              'from_location': 'प्रारंभिक स्थान / Origin',
              'to_location': 'गंतव्य स्थान / Destination',
              'fixed_route_rent': 'मार्ग आधारित निर्धारित किराया (₹)',
              'save_route_rent': 'मार्ग और किराया सहेजें',
              'save_vehicle': 'वाहन सहेजें',
              'save_changes': 'परिवर्तन सहेजें',
              'cancel': 'रद्द करें',
              'route_added_success': 'मार्ग और किराया सफलतापूर्वक जोड़ा गया! ✅',
              'add_another_route': 'एक और मार्ग जोड़ें',
              'my_vehicles': 'मेरे वाहन',
              'edit_vehicle': 'वाहन संपादित करें',
              'edit_vehicle_title': '✏️ वाहन संपादित करें',
              'vehicle_name': 'वाहन का नाम',
              'contact_number': 'संपर्क नंबर',
              'update_vehicle': 'वाहन अपडेट करें',
              'delete_vehicle': 'वाहन हटाएं',
              'active_vehicles': 'सक्रिय वाहन',
              'total_bookings': 'कुल बुकिंग्स',
              'total_vehicles': 'कुल वाहन',
              'pending_action': 'लंबित कार्रवाई',
              'accepted_bookings': 'स्वीकृत बुकिंग्स',
              'quick_actions_title': 'त्वरित प्रबंधन',
              'manage_fleet': 'फ्लीट प्रबंधित करें',
              'manage_fleet_desc': 'मौजूदा वाहनों को देखें, संपादित करें या हटाएं',
              'manage_routes_desc': 'ग्राहकों के लिए निश्चित मार्ग मूल्य निर्धारित करें',
              'owner_profile_title': 'मालिक प्रोफ़ाइल',
              'account_status': 'खाता स्थिति',
              'active_fleet': 'सक्रिय फ्लीट',
              'no_bookings_title': 'अभी तक कोई बुकिंग अनुरोध नहीं',
              'no_bookings_desc': 'आपके वाहनों के लिए ग्राहक बुकिंग यहाँ दिखाई देगी।',
              'choose_vehicle_fleet_hint': 'अपने पंजीकृत फ्लीट से वाहन चुनें।',
              'starting_point_hint': 'प्रारंभिक शहर, गाँव या लोडिंग बिंदु।',
              'delivery_point_hint': 'डिलीवरी बाज़ार, गोदाम या गंतव्य शहर।',
              'total_fixed_rent_hint': 'इस एकतरफ़ा मार्ग यात्रा के लिए कुल निश्चित किराया।',
              'route_open_notice': 'यह वाहन मार्ग तुरंत ग्राहक बुकिंग के लिए उपलब्ध होगा।',
              'transparent_pricing_title': 'पारदर्शी मार्ग-आधारित मूल्य निर्धारण',
              'source_placeholder': 'उदाहरण: ताड़ीपत्री',
              'destination_placeholder': 'उदाहरण: अनंतपुर',
              'rent_placeholder': 'उदाहरण: 2500',
              'vehicle_name_example': 'उदाहरण: टाटा ऐस',
              'location_example': 'उदाहरण: कांचीपुरम',
              'demo_payment': 'डेमो भुगतान',
              'demo_payment_banner': 'डेमो भुगतान — कोई वास्तविक पैसा नहीं लिया जाएगा',
              'secure_demo_payment': 'सुरक्षित डेमो भुगतान',
              'demo_payment_disclaimer': 'यह केवल एक प्रोजेक्ट डेमो लेनदेन है। कोई वास्तविक पैसा नहीं लिया जाता है।',
              'amount_paid': 'भुगतान की गई राशि',
              'payment_method': 'भुगतान विधि',
              'cardholder_name': 'कार्डधारक का नाम',
              'upi_id': 'UPI ID',
              'enter_cardholder_placeholder': 'डेमो कार्डधारक का नाम दर्ज करें',
              'enter_upi_placeholder': 'demo@upi',
              'pay_amount_btn': 'राशि का भुगतान करें',
              'payment_successful': 'भुगतान सफल रहा',
              'payment_success_desc': 'यह डेमो भुगतान सफलतापूर्वक पूरा हुआ और सिस्टम में दर्ज किया गया है।',
              'transaction_reference': 'लेनदेन संदर्भ संख्या',
              'date': 'तारीख',
              'view_receipt': 'रसीद देखें',
              'download_receipt': 'रसीद डाउनलोड करें',
              'view_booking': 'बुकिंग देखें',
              'upload_leaf_image': 'पत्ती की छवि अपलोड करें',
              'analyze_leaf': 'पत्ती का विश्लेषण करें',
              'drag_leaf_image': 'यहाँ पत्ती की तस्वीर चुनें या खींचें',
              'image_format_limit': 'JPG, JPEG, PNG या WEBP • अधिकतम 10 MB',
              'ai_assistant_intro': 'फसल की पत्ती की स्पष्ट तस्वीर अपलोड करें और संभावित बीमारी, लक्षण और रोकथाम पर AI '
                                    'सहायता प्राप्त करें।',
              'photo_tips': 'सर्वोत्तम परिणामों के लिए, अच्छी रोशनी में प्रभावित क्षेत्र की स्पष्ट तस्वीर लें।',
              'analysis_result': 'विश्लेषण परिणाम',
              'ai_assisted_notice': 'अपलोड की गई छवि के आधार पर AI-सहायता प्राप्त परिणाम।',
              'possible_disease': 'संभावित बीमारी',
              'crop': 'फसल',
              'confidence': 'सटीकता',
              'symptoms': 'लक्षण',
              'possible_causes': 'संभावित कारण',
              'treatment_management': 'उपचार / प्रबंधन',
              'prevention': 'रोकथाम',
              'immediate_steps': 'तत्काल कदम',
              'expert_advice': 'विशेषज्ञ सलाह',
              'ask_ai_assistant': 'AI सहायक से पूछें',
              'followup_prompt_desc': 'विश्लेषण की गई पत्ती, लक्षणों या फसल देखभाल के बारे में प्रश्न पूछें।',
              'safety_notice': 'सुरक्षा सूचना',
              'safety_notice_desc': 'यह AI सहायक केवल शैक्षणिक मार्गदर्शन प्रदान करता है। कीटनाशक उपयोग के लिए स्थानीय '
                                    'कृषि अधिकारियों से परामर्श करें।',
              'chat_placeholder': 'अपना कृषि प्रश्न यहाँ टाइप करें...',
              'send': 'भेजें',
              'q_disease': 'यह कौन सी बीमारी है?',
              'q_todo': 'मुझे अब क्या करना चाहिए?',
              'q_prevent': 'इसे कैसे रोका जाए?',
              'q_telugu': 'तेलुगु में समझाएं',
              'q_tamil': 'तमिल में समझाएं',
              'q_hindi': 'हिन्दी में समझाएं',
              'analyzing': 'पत्ती की छवि का विश्लेषण किया जा रहा है...',
              'ai_thinking': 'AI मार्गदर्शन तैयार कर रहा है...',
              'no_leaf_analyzed_yet': 'AI निदान देखने के लिए ऊपर एक पत्ती की तस्वीर अपलोड करें।',
              'admin': 'व्यवस्थापक',
              'customers': 'ग्राहक',
              'owners': 'मालिक',
              'payments': 'भुगतान',
              'ratings_reviews': 'रेटिंग और समीक्षाएं',
              'overview': 'अवलोकन',
              'enterprise_operations': 'प्रशासनिक संचालन',
              'live_db_connected': 'लाइव डेटाबेस जुड़ा हुआ है',
              'admin_management_desc': 'प्रत्यक्ष प्रशासनिक प्रबंधन और परिचालन रिकॉर्ड।',
              'recent_bookings': 'हाल की बुकिंग्स',
              'no_bookings_found': 'कोई बुकिंग नहीं मिली।',
              'no_records_found': 'कोई रिकॉर्ड नहीं मिला।',
              'search': 'खोजें',
              'filter': 'फ़िल्टर',
              'all_statuses': 'सभी स्थितियाँ',
              'active': 'सक्रिय',
              'inactive': 'निष्क्रिय',
              'activate': 'सक्रिय करें',
              'deactivate': 'निष्क्रिय करें',
              'update_status': 'स्थिति अपडेट करें',
              'read_only_view': 'केवल पढ़ने योग्य प्रशासनिक दृश्य।',
              'registration_success': 'पंजीकरण सफल रहा! अब आप लॉगिन कर सकते हैं।',
              'login_successful': 'लॉगिन सफल रहा।',
              'logout_successful': 'आप लॉगआउट हो चुके हैं।',
              'invalid_credentials': 'ईमेल या पासवर्ड गलत है।',
              'invalid_customer_login': 'मोबाइल नंबर या पासवर्ड गलत है।',
              'invalid_owner_login': 'ईमेल या पासवर्ड गलत है।',
              'invalid_admin_login': 'व्यवस्थापक उपयोगकर्ता नाम या पासवर्ड गलत है।',
              'email_already_registered': 'यह ईमेल पता पहले से पंजीकृत है। कृपया दूसरा ईमेल उपयोग करें या साइन इन '
                                          'करें।',
              'mobile_already_registered': 'यह मोबाइल नंबर पहले से पंजीकृत है। कृपया दूसरा नंबर उपयोग करें या साइन इन '
                                           'करें।',
              'valid_email_err': 'एक मान्य ईमेल पता दर्ज करें।',
              'valid_phone_err': 'एक मान्य 10 अंकों का मोबाइल नंबर दर्ज करें।',
              'password_length_err': 'पासवर्ड 8 से 128 वर्णों का होना चाहिए।',
              'passwords_dont_match': 'पासवर्ड मेल नहीं खाते।',
              'booking_date_past_err': 'बुकिंग तिथि पिछली तारीख नहीं हो सकती।',
              'select_valid_date_err': 'कृपया एक मान्य बुकिंग तिथि चुनें।',
              'select_valid_route_err': 'कृपया बुकिंग से पहले एक मान्य मार्ग चुनें।',
              'duplicate_booking_err': 'इस तिथि पर इस वाहन के लिए आपके पास पहले से एक बुकिंग अनुरोध है।',
              'booking_created_success': 'बुकिंग अनुरोध सफलतापूर्वक सबमिट किया गया! मालिक की मंजूरी की प्रतीक्षा है।',
              'booking_status_updated': 'बुकिंग स्थिति अपडेट की गई।',
              'booking_status_update_err': 'बुकिंग स्थिति अपडेट नहीं की जा सकी। कृपया पुनः प्रयास करें।',
              'only_pending_can_change': 'केवल लंबित बुकिंग की स्थिति बदली जा सकती है।',
              'select_rating_err': 'कृपया 1 से 5 स्टार के बीच रेटिंग चुनें।',
              'rating_saved': 'धन्यवाद! आपकी रेटिंग और समीक्षा सहेज ली गई है।',
              'demo_payment_success': 'डेमो भुगतान सफलतापूर्वक पूरा हुआ। एक पुष्टिकरण ईमेल भेजा गया है।',
              'demo_payment_success_no_mail': 'डेमो भुगतान सफलतापूर्वक पूरा हुआ। भुगतान दर्ज कर लिया गया है।',
              'demo_payment_failed': 'डेमो भुगतान प्रसंस्करण विफल रहा। कृपया पुनः प्रयास करें।',
              'valid_payment_method_err': 'कृपया एक मान्य भुगतान विधि चुनें।',
              'cardholder_name_err': 'कृपया डेमो कार्ड के लिए कार्डधारक का नाम दर्ज करें।',
              'upi_id_err': 'कृपया भुगतान के लिए डेमो UPI ID दर्ज करें।',
              'vehicle_added_success': 'वाहन सफलतापूर्वक जोड़ा गया! अब आप मार्ग और किराया निर्धारित कर सकते हैं।',
              'vehicle_updated_success': 'वाहन विवरण सफलतापूर्वक अपडेट किया गया।',
              'vehicle_deleted_success': 'वाहन सफलतापूर्वक हटाया गया।',
              'access_denied': 'पहुँच अस्वीकृत: आप केवल अपने स्वयं के वाहनों और मार्गों का प्रबंधन कर सकते हैं।',
              'invalid_csrf_token': 'अमान्य CSRF टोकन। कृपया पृष्ठ को रीफ्रेश करें और पुनः प्रयास करें।',
              'error_404_title': '404 - पृष्ठ नहीं मिला',
              'error_404_desc': 'जिस पृष्ठ को आप खोज रहे हैं वह मौजूद नहीं है या स्थानांतरित कर दिया गया है।',
              'error_500_title': '500 - सर्वर त्रुटि',
              'error_500_desc': 'एक अप्रत्याशित त्रुटि हुई। कृपया थोड़ी देर बाद पुनः प्रयास करें।'}}

class TranslationDict(dict):
    """Fallback-aware translation dictionary that avoids KeyError and defaults safely."""
    def __init__(self, current_dict, fallback_dict):
        super().__init__(current_dict or {})
        self.fallback = fallback_dict or {}

    def __getitem__(self, key):
        if key in self:
            return super().__getitem__(key)
        if key in self.fallback:
            return self.fallback[key]
        return key

    def get(self, key, default=None):
        if key in self:
            return super().get(key)
        if key in self.fallback:
            return self.fallback.get(key)
        return default if default is not None else key


def get_text(key, default=None):
    """Retrieve translated text according to the active session/cookie language."""
    language = session.get("language", "en")
    if language not in SUPPORTED_LANGUAGES:
        language = "en"
    current_dict = TRANSLATIONS.get(language, TRANSLATIONS["en"])
    if key in current_dict:
        return current_dict[key]
    if key in TRANSLATIONS["en"]:
        return TRANSLATIONS["en"][key]
    return default if default is not None else key


# =========================================================
# MAKE TRANSLATIONS AVAILABLE TO HTML
# =========================================================

@app.context_processor
def inject_language():
    language = session.get("language", "en")
    if language not in SUPPORTED_LANGUAGES:
        language = "en"

    current_dict = TRANSLATIONS.get(language, TRANSLATIONS["en"])
    fallback_dict = TRANSLATIONS["en"]
    t_obj = TranslationDict(current_dict, fallback_dict)

    return {
        "t": t_obj,
        "get_text": get_text,
        "current_language": language,
        "supported_languages": SUPPORTED_LANGUAGES,
        "language_names": LANGUAGE_NAMES,
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

    referrer = request.referrer or "/"
    target = "/"
    if referrer.startswith("/") and not referrer.startswith("//"):
        target = referrer
    else:
        from urllib.parse import urlparse
        try:
            parsed = urlparse(referrer)
            if not parsed.netloc or parsed.netloc == request.host:
                target = parsed.path or "/"
                if parsed.query:
                    target += "?" + parsed.query
        except Exception:
            target = "/"

    response = redirect(target if target else "/")
    response.set_cookie("language", language, max_age=30 * 24 * 3600, samesite="Lax")
    return response
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
    if request.method == "GET":
        if session.get("user_id"):
            return redirect("/vehicles")
        if session.get("owner_id"):
            return redirect("/owner_dashboard")

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
    if request.method == "GET":
        if session.get("user_id"):
            return redirect("/vehicles")
        if session.get("owner_id"):
            return redirect("/owner_dashboard")

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
            saved_lang = session.get("language", "en")
            session.clear()
            session.permanent = True
            session["language"] = saved_lang
            session["user_id"] = user[0]
            session["user_name"] = user[1]
            session["user_phone"] = user[2]
            session["user_role"] = "customer"
            session["logged_in"] = True
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
    saved_lang = session.get("language", "en")
    session.clear()
    session["language"] = saved_lang
    return redirect("/")


# =========================================================
# OWNER REGISTRATION
# =========================================================

@app.route("/ownerregister", methods=["GET", "POST"])
def ownerregister():
    if request.method == "GET":
        if session.get("owner_id"):
            return redirect("/owner_dashboard")
        if session.get("user_id"):
            return redirect("/vehicles")

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
    if request.method == "GET":
        if session.get("owner_id"):
            return redirect("/owner_dashboard")
        if session.get("user_id"):
            return redirect("/vehicles")

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

            saved_lang = session.get("language", "en")
            session.clear()
            session.permanent = True
            session["language"] = saved_lang
            session["owner_id"] = owner[0]
            session["owner_name"] = owner[1]
            session["owner_phone"] = owner[2]
            session["owner_email"] = owner[3]
            session["user_role"] = "owner"
            session["logged_in"] = True

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
            saved_lang = session.get("language", "en")
            session.clear()
            session.permanent = True
            session["language"] = saved_lang
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
    saved_lang = session.get("language", "en")
    session.clear()
    session["language"] = saved_lang
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
        SELECT id, vehicle_name, vehicle_type, location
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
            cursor.execute(
                """
                SELECT id, vehicle_name, vehicle_type, location
                FROM vehicles
                WHERE owner_id = %s AND is_active = TRUE
                ORDER BY id DESC
                """,
                (owner_id,)
            )
            vehicles_data = cursor.fetchall()
            return render_template(
                "add_route.html",
                vehicles=vehicles_data,
                error="Access Denied: You can only add routes to your own vehicles."
            ), 403

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

        cursor.execute(
            """
            SELECT id, vehicle_name, vehicle_type, location
            FROM vehicles
            WHERE owner_id = %s AND is_active = TRUE
            ORDER BY id DESC
            """,
            (owner_id,)
        )
        vehicles_data = cursor.fetchall()

        flash("Route and rent added successfully! ✅", "success")
        return render_template(
            "add_route.html",
            vehicles=vehicles_data,
            success=True,
            added_route={
                "vehicle_name": vehicle[1],
                "vehicle_type": vehicle[2],
                "from_location": from_location,
                "to_location": to_location,
                "rent": rent,
            }
        ), 200

    cursor.execute(
        """
        SELECT
            id,
            vehicle_name,
            vehicle_type,
            location
        FROM vehicles
        WHERE owner_id = %s AND is_active = TRUE
        ORDER BY id DESC
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
    saved_lang = session.get("language", "en")
    session.clear()
    session["language"] = saved_lang
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