# Business Vehicle Booking — Final Setup Guide

## What was fixed in this package

- Persistent Flask `SECRET_KEY` is included in the local `.env` so restarting Flask does not invalidate old CSRF/session cookies unexpectedly.
- Language switching remains CSRF-protected and now safely handles the referrer.
- Owner dashboard redesigned with a professional responsive layout.
- Pending booking requests now have a highly visible **Accept** and **Reject** action area.
- Owner booking status endpoint normalizes `Accepted`/`Rejected` consistently.
- Added `/health` diagnostic endpoint.
- Gemini AI integration uses the multimodal `gemini-3.6-flash` model and retries transient API failures.
- AI errors are reported more usefully without exposing the API key.
- Booking email status is already shown on the booking-success page.
- The project does not fake AI results or fake email delivery: real external credentials are required.

## 1. Install dependencies

Activate the existing virtual environment or create a new one:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 2. MySQL

Make sure MySQL is running and the database in `.env` exists:

```text
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_DATABASE=vehicle_booking
MYSQL_USER=root
MYSQL_PASSWORD=your_password
```

Run the existing schema/migration process used by the project. If route/rating migration is needed:

```powershell
python migrate_route_and_ratings.py
```

## 3. AI disease analysis

The AI page requires a real AI API key.

Set these values in `.env`:

```text
AI_PROVIDER=gemini
AI_API_KEY=YOUR_GEMINI_API_KEY
AI_MODEL=gemini-3.6-flash
GEMINI_TIMEOUT=30
```

Restart Flask after changing `.env`.

Then test:

1. Open `/farmer-ai`
2. Upload a JPG/PNG/WEBP leaf image under 10 MB
3. Click Analyze Leaf
4. Ask a follow-up question in the chat

If the page says AI is not configured, the key is missing from `.env`.

## 4. Booking email

Email cannot be sent without a real SMTP account.

For Gmail, use an App Password rather than your normal Gmail password.

Set:

```text
MAIL_ENABLED=true
MAIL_SERVER=smtp.gmail.com
MAIL_PORT=587
MAIL_USE_TLS=true
MAIL_USERNAME=yourgmail@gmail.com
MAIL_PASSWORD=your_16_character_app_password
MAIL_DEFAULT_SENDER=BusinessVehicleBooking <yourgmail@gmail.com>
```

Also make sure the customer account has an email address. The owner account should have an email address for owner notifications.

Restart Flask after changing `.env`.

The booking-success page will show one of:

- Confirmation email sent
- Booking saved but email failed
- No email address registered
- Email confirmation is not configured

## 5. Start the application

```powershell
python app.py
```

Open:

```text
http://127.0.0.1:5000/
```

## 6. Test the complete booking flow

Customer:

1. Register/login
2. Ensure the account has an email address
3. Open Available Vehicles
4. Select a vehicle and route
5. Select a future booking date
6. Confirm booking
7. Verify Booking Successful page
8. Check My Bookings

Owner:

1. Login as the vehicle owner
2. Open Owner Dashboard
3. The new booking should appear under Customer Booking Requests
4. A Pending booking displays **Accept** and **Reject**
5. Click Accept or Reject
6. The dashboard should immediately show the updated status

## 7. Health check

With Flask running, open:

```text
http://127.0.0.1:5000/health
```

A healthy local setup should report:

```json
{
  "status": "ok",
  "database": true,
  "email": true,
  "ai": true
}
```

If `email` or `ai` is false, the corresponding `.env` configuration is missing.

## Important

Do not commit `.env` to GitHub. It contains database credentials, session secrets, email credentials, and API keys.

The application code is connected to the backend, but external services cannot be activated without the owner's own credentials.
