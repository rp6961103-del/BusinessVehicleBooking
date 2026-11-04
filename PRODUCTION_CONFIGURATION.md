# Production configuration

Set these environment variables through the hosting platform's secret/configuration settings. Do not commit a production `.env` file.

Required in production:

- `APP_ENV=production`
- `SECRET_KEY`: a long, random secret used to sign Flask sessions.
- `MYSQL_HOST`: production MySQL host.
- `MYSQL_PORT`: production MySQL port.
- `MYSQL_DATABASE`: production database name.
- `MYSQL_USER`: least-privileged production database user.
- `MYSQL_PASSWORD`: production database password.
- `SESSION_COOKIE_SECURE=true`: required when serving over HTTPS.
- `TRUSTED_PROXY_HOPS=0`: leave at zero unless a known reverse proxy is directly in front of the app. Set it to the exact trusted proxy hop count only after deployment topology is verified.
- `MAIL_ENABLED=false`: keep disabled unless SMTP settings are configured.
- `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USE_TLS`, `MAIL_USE_SSL`, `MAIL_DEFAULT_SENDER`: required when email notifications are enabled. Use STARTTLS (commonly port 587) or implicit SSL (commonly port 465), not both.
- `MAIL_USERNAME` and `MAIL_PASSWORD`: optional when the SMTP relay does not require authentication; configure both together when authentication is required. `MAIL_PASSWORD` is secret.
- `PASSWORD_RESET_BASE_URL`: optional public HTTPS origin used in password-reset links, for example `https://your-service.onrender.com`. If unset, Render's `RENDER_EXTERNAL_URL` is used. In production, reset mail is not sent unless one of these resolves to a valid HTTPS origin.

Email submission is independent from booking persistence. Customers receive a booking confirmation and an email whenever the owner accepts or rejects a booking; the application does not impose a notification count limit. SMTP providers may enforce their own sending quotas or rate limits. A successful SMTP submission means the SMTP server accepted the message, not that it reached the recipient's inbox. The booking confirmation page reports customer and owner submissions separately and shows missing SMTP setting names when configuration is incomplete.

For Gmail SMTP, use `MAIL_SERVER=smtp.gmail.com`, `MAIL_PORT=587`, `MAIL_USE_TLS=true`, and `MAIL_USE_SSL=false`. Set `MAIL_USERNAME` and `MAIL_DEFAULT_SENDER` to the sending Gmail address, and use a Google App Password for `MAIL_PASSWORD` (not the normal account password). Google requires 2-Step Verification to create an App Password.

Optional:

- `FLASK_DEBUG=0`: production startup rejects debug mode.

Development uses the `MYSQL_*` variables and may use convenient local defaults. Integration tests use only `TEST_MYSQL_*` variables and must target a separate database. Production must never use test variables.

The application validates the required production variables during startup. Missing names are reported, but secret values are never printed.

`bookings.booking_date` is a `DATE` representing the requested service/booking day, not the time when the customer created the booking. The owner dashboard displays only this date; the application does not require `bookings.created_at`, so it works with databases that do not have that optional column. If a booking-created timestamp is needed later, use a separately reviewed migration that first verifies the column is absent and then adds `bookings.created_at DATETIME NULL DEFAULT NULL`. Existing bookings should remain `NULL`; never infer or backfill a creation time from `booking_date`. Deploy code that writes or displays this timestamp only after the column exists. The current `migrate_database.py` also performs unrelated account and password migrations, so do not run it solely to add this timestamp without reviewing those effects. No production migration is part of this dashboard fix.

The same migration creates `password_reset_tokens` and `password_reset_attempts`. Run `python migrate_database.py` once before deploying password reset to an existing database. Password-reset emails reuse the existing SMTP settings above; set `MAIL_ENABLED=true`, the mail host/port/sender, TLS or SSL, and both SMTP credentials when required. Reset links are account-type-specific, single-use, expire after 30 minutes, and contain the token in the URL fragment so it is not sent in HTTP requests or normal access logs. The database stores only its SHA-256 hash. Generic responses are shown whether or not an account matches. Configure `PASSWORD_RESET_BASE_URL` to the canonical Render/custom domain when necessary; no reset-token or password values should be added to logs.

Owner subscriptions use a separate additive migration. Back up the target database, then run `python migrate_subscriptions.py` after the existing `owners` and `admins` tables are present. Configure owner-facing payment instructions through **Admin → Subscriptions**; no bank/UPI details or payment gateway are configured by the application. Admins must independently verify receipt and explicitly confirm it before premium status becomes active. No existing owner feature is premium-gated.

The application connects using the `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_DATABASE`, `MYSQL_USER`, and `MYSQL_PASSWORD` variables; it does not read `DATABASE_URL`. Customer phone lookup supports the MySQL 8 `REGEXP_REPLACE` function so older rows with punctuation or an optional India country code remain findable without changing stored account data. On successful connection and connection failures, server logs identify the MySQL host, port, database name, and whether a user is configured, but never log the username or password. For Render with Aiven, compare those logged target values with the Aiven service endpoint, port, and database name. A successful connection to a different database containing no customer rows will correctly look like an incorrect-login attempt, so verify the target before changing any account data.

Production WSGI entry point: `wsgi:app`. Run it with Waitress from the hosting platform. Do not use Flask's development server in production. The app assumes HTTPS is terminated by the trusted proxy when `SESSION_COOKIE_SECURE=true`.