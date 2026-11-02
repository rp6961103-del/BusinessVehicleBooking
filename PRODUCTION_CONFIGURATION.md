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
- `MYSQL_SSL_CA`: path to the Aiven CA certificate; the application verifies the server certificate and hostname when this is set.
- `SESSION_COOKIE_SECURE=true`: required when serving over HTTPS.
- `TRUSTED_PROXY_HOPS=0`: leave at zero unless a known reverse proxy is directly in front of the app. Set it to the exact trusted proxy hop count only after deployment topology is verified.
- `MAIL_ENABLED=false`: keep disabled unless SMTP settings are configured.
- `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USE_TLS`, `MAIL_USE_SSL`, `MAIL_DEFAULT_SENDER`: required when email notifications are enabled. Use STARTTLS (commonly port 587) or implicit SSL (commonly port 465), not both.
- `MAIL_USERNAME` and `MAIL_PASSWORD`: optional when the SMTP relay does not require authentication; configure both together when authentication is required. `MAIL_PASSWORD` is secret.
- `PASSWORD_RESET_BASE_URL`: optional public HTTPS origin used in password-reset links, for example `https://your-service.onrender.com`. If unset, Render's `RENDER_EXTERNAL_URL` is used. In production, reset mail is not sent unless one of these resolves to a valid HTTPS origin.

Email submission is independent from booking persistence. Customers receive a booking confirmation and a status message each time an owner accepts or rejects a booking; the application does not impose a notification count limit. SMTP providers may enforce their own quotas. A successful SMTP submission means the SMTP server accepted the message, not that it reached the recipient's inbox.

The Render blueprint enables Gmail SMTP with STARTTLS. Enter a Google App Password in Render's `MAIL_PASSWORD` environment setting; never commit it. The sending account must have 2-Step Verification enabled.

For Aiven MySQL, download the service CA certificate and add it to the Render service as a secret file. Set `MYSQL_SSL_CA` to that file's path (for example, `/etc/secrets/aiven-ca.pem`) along with the Aiven host, port, database, user, and password. Do not copy the CA certificate or database credentials into source code.

Render's blueprint starts `gunicorn wsgi:app` and binds it to the assigned `$PORT`. Use the blueprint from the GitHub repository, enter the Aiven values and a generated `SECRET_KEY` in Render, and deploy. Flask serves the HTML frontend and backend from the same web service.

Optional:

- `FLASK_DEBUG=0`: production startup rejects debug mode.

Development uses the `MYSQL_*` variables and may use convenient local defaults. Integration tests use only `TEST_MYSQL_*` variables and must target a separate database. Production must never use test variables.

The application validates the required production variables during startup. Missing names are reported, but secret values are never printed.

Before deploying the booking-created timestamp change to an existing database, run `python migrate_database.py` once with the production `MYSQL_*` settings configured. This safely adds nullable `bookings.created_at`; existing bookings remain `NULL` and display “Booking time unavailable.” New bookings store a UTC timestamp, which the owner dashboard displays in IST. Fresh databases receive the field from `database/vehicle_booking.sql`.

The same migration creates `password_reset_tokens` and `password_reset_attempts`. Run `python migrate_database.py` once before deploying password reset to an existing database. Password-reset emails reuse the existing SMTP settings above; set `MAIL_ENABLED=true`, the mail host/port/sender, TLS or SSL, and both SMTP credentials when required. Reset links are account-type-specific, single-use, expire after 30 minutes, and contain the token in the URL fragment so it is not sent in HTTP requests or normal access logs. The database stores only its SHA-256 hash. Generic responses are shown whether or not an account matches. Configure `PASSWORD_RESET_BASE_URL` to the canonical Render/custom domain when necessary; no reset-token or password values should be added to logs.

The application connects using the `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_DATABASE`, `MYSQL_USER`, and `MYSQL_PASSWORD` variables; it does not read `DATABASE_URL`. Customer phone lookup supports the MySQL 8 `REGEXP_REPLACE` function so older rows with punctuation or an optional India country code remain findable without changing stored account data. On successful connection and connection failures, server logs identify the MySQL host, port, database name, and whether a user is configured, but never log the username or password. For Render with Aiven, compare those logged target values with the Aiven service endpoint, port, and database name. A successful connection to a different database containing no customer rows will correctly look like an incorrect-login attempt, so verify the target before changing any account data.

Production WSGI entry point: `wsgi:app`. Run it with Waitress from the hosting platform. Do not use Flask's development server in production. The app assumes HTTPS is terminated by the trusted proxy when `SESSION_COOKIE_SECURE=true`.