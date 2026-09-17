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
- `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USE_TLS`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_DEFAULT_SENDER`: required only when email notifications are enabled; `MAIL_PASSWORD` is secret.

Optional:

- `FLASK_DEBUG=0`: production startup rejects debug mode.

Development uses the `MYSQL_*` variables and may use convenient local defaults. Integration tests use only `TEST_MYSQL_*` variables and must target a separate database. Production must never use test variables.

The application validates the required production variables during startup. Missing names are reported, but secret values are never printed.

Production WSGI entry point: `wsgi:app`. Run it with Waitress from the hosting platform. Do not use Flask's development server in production. The app assumes HTTPS is terminated by the trusted proxy when `SESSION_COOKIE_SECURE=true`.