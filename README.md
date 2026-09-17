# Business Vehicle Booking — Ready-to-Run Project

A Flask + MySQL vehicle booking system for farmers/business customers and vehicle owners.

## Included
- Existing customer login
- Owner registration/login
- Add, edit and delete vehicles
- Route-wise rent (a vehicle can have multiple routes)
- Customer vehicle search/filter
- Date-based booking with duplicate-booking protection
- Owner booking dashboard with accept/reject
- Customer booking history
- Customer rating/review after an accepted booking
- Booking confirmation + owner notification emails
- Farmer AI crop-leaf analysis and follow-up chatbot
- English/Telugu/Hindi UI support
- CSRF protection and secure password hashing

## Windows quick start
1. Install Python 3.11+ and MySQL 8.
2. Open this folder in VS Code.
3. Double-click `setup_windows.bat`.
4. Open `.env` and set your MySQL values.
5. Make sure MySQL Server is running.
6. Double-click `setup_database_windows.bat` (or run `python setup_database.py`).
7. Double-click `start_windows.bat`.
8. Open http://127.0.0.1:5000/

For Farmer AI, add an AI API key to `.env`. For email, configure SMTP settings. Both are optional; booking itself does not depend on email delivery.

## Important
The ZIP does not contain private `.env` credentials or `.venv`. This prevents accidental credential leakage. Your local `.env` is the only file that should contain passwords/API keys.

### If you already have an older database
Run `python migrate_route_and_ratings.py` once after activating `.venv`. This adds route-aware bookings and the ratings table without deleting existing booking records.
