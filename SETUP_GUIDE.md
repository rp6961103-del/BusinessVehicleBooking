# BusinessVehicleBooking — Professional Feature Setup

This version keeps the existing Flask + MySQL booking workflow and adds:

- Customer booking confirmation email
- Vehicle-owner booking notification email
- Farmer AI crop-leaf image analysis
- Farmer AI follow-up chatbot
- English, Telugu and Hindi UI support
- Secure upload validation and CSRF-protected AI POST routes

## 1. Install dependencies

Create/activate your Python virtual environment and run:

```bash
pip install -r requirements.txt
```

## 2. Configure `.env`

Copy `.env.example` to `.env` if needed. Keep your existing MySQL values.

### Email

Set:

```text
MAIL_ENABLED=true
MAIL_SERVER=smtp.gmail.com
MAIL_PORT=587
MAIL_USE_TLS=true
MAIL_USERNAME=your-email@gmail.com
MAIL_PASSWORD=your-app-password
MAIL_DEFAULT_SENDER=BusinessVehicleBooking <your-email@gmail.com>
```

For Gmail, use a Google App Password rather than your normal account password when required by your account security settings.

If email is not configured, bookings still work and the success page will accurately say that email confirmation is unavailable.

### Farmer AI

For Gemini:

```text
AI_PROVIDER=gemini
AI_API_KEY=your-api-key
AI_MODEL=gemini-3.6-flash
GEMINI_TIMEOUT=30
```

For an OpenAI-compatible provider:

```text
AI_PROVIDER=openai
AI_API_KEY=your-api-key
AI_MODEL=your-vision-capable-model
AI_BASE_URL=https://api.openai.com/v1
```

Never place an AI key in HTML or JavaScript and never commit `.env` to Git.

## 3. Start the application

Windows:

```powershell
python app.py
```

Then open the local address shown by Flask, normally:

`http://127.0.0.1:5000/`

## 4. Test booking email

1. Use an existing customer account with a real email address.
2. Register/login as an owner and add a vehicle + route.
3. Login as the customer.
4. Open Available Vehicles.
5. Book an available date.
6. Confirm the booking.
7. Verify the booking appears in My Bookings.
8. Verify the customer receives the confirmation email.
9. Verify the owner receives the new-booking notification.

The booking is saved before email delivery. A temporary SMTP failure does not roll back the booking.

## 5. Test Farmer AI

1. Open **Farmer AI** from the navigation.
2. Upload a clear JPG/PNG/WEBP leaf image.
3. Click **Analyze Leaf**.
4. Review crop, possible disease, confidence, symptoms, causes, immediate steps, management and prevention.
5. Ask a follow-up question in the chatbot.
6. Change the UI language to Telugu or Hindi and analyze again.

The result is an AI estimate, not a guaranteed diagnosis. Chemical/pesticide use must be verified with a qualified agricultural professional and the product label.

## 6. Important project safety

The final source package intentionally does not need a `.env` file. Copy your existing private `.env` into the project folder locally. Do not upload credentials to GitHub or share them publicly.

## Route selection and ratings
Each vehicle can have multiple route/rent entries. Customers now book the exact route they selected. If you already created the old database before this version, run:

```powershell
python migrate_route_and_ratings.py
```

After an owner accepts a booking, the customer will see a **Rate** action in My Bookings and can submit 1–5 stars plus an optional review.
