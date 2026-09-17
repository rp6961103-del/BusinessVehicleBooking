# BusinessVehicleBooking deployment and operations

This document describes a controlled production-readiness procedure. It does not deploy the application, provision infrastructure, or run migrations automatically.

## 0. Application profile

- Stack: Flask, MySQL, Jinja HTML templates, CSS, and JavaScript.
- Flask entry point: `app.py`.
- WSGI entry point: `wsgi:app` from `wsgi.py`.
- Current development interpreter: Python 3.14.5 in `.venv`.
- Recommended production baseline: Python 3.14.x after the hosting provider confirms support. If the provider does not support it, use its supported Python version and re-run the clean requirements and full test checks.
- Runtime dependencies are pinned in `requirements.txt`; pytest is development-only in `requirements-dev.txt`.

The application serves templates and static assets from `templates/` and `static/`. Templates use Flask-generated static URLs. No upload or media-storage route was found, so there is currently no user-upload persistence requirement. If uploads are added later, use persistent provider storage or object storage rather than relying on application-local disk.

## 1. Production configuration

Set environment variables through the hosting platform's secret/configuration store. Do not commit a production `.env` file.

Required variables:

- `APP_ENV=production`
- `SECRET_KEY=<SET_IN_HOSTING_PLATFORM>`
- `MYSQL_HOST=<SET_IN_HOSTING_PLATFORM>`
- `MYSQL_PORT=<SET_IN_HOSTING_PLATFORM>`
- `MYSQL_DATABASE=<SET_IN_HOSTING_PLATFORM>`
- `MYSQL_USER=<SET_IN_HOSTING_PLATFORM>`
- `MYSQL_PASSWORD=<SET_IN_HOSTING_PLATFORM>`
- `SESSION_COOKIE_SECURE=true`
- `FLASK_DEBUG=0`
- `TRUSTED_PROXY_HOPS=0`, unless the verified topology has a trusted reverse proxy
- `MAIL_ENABLED=false`, unless SMTP notifications are configured.
- `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USE_TLS`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_DEFAULT_SENDER` when mail is enabled.

`SECRET_KEY` and `MYSQL_PASSWORD` are secrets. Values must be entered directly into the hosting platform's secret store and must never be logged or committed. Production startup rejects missing required variables and debug mode.

When `MAIL_ENABLED=true`, `MAIL_PASSWORD` is also a secret. Email delivery is disabled by default in development and notification failures do not cancel a committed booking.

Development uses `MYSQL_*`. Integration tests use `TEST_MYSQL_*` and must point to a separate disposable database. Never copy test variables into production configuration.

## 2. Application startup

The WSGI application is exposed as `wsgi:app`. Use the pinned Waitress dependency from `requirements.txt` through the hosting platform's process configuration. Do not use Flask's development server in production.

Example process command, with the bind address and port supplied by the hosting platform:

```text
waitress-serve --listen=0.0.0.0:<PORT> wsgi:app
```

Use the platform's documented Waitress command syntax and bind/port settings. Do not place credentials in the command line. HTTPS should be terminated by the verified reverse proxy or hosting platform, with `SESSION_COOKIE_SECURE=true` enabled.

## 2.1. Hosting options

### Option A: Render or similar managed application hosting

- Flask support: Yes, through a Python build and a Waitress start command.
- MySQL support: Usually requires an external managed MySQL provider; verify network access and TLS requirements.
- Deployment complexity: Low to moderate. Configure build/install and start commands plus environment variables.
- Persistent files: Local disk should be treated as ephemeral. This project currently has no upload feature.
- HTTPS: Managed HTTPS is typically available.
- Environment variables: Supported through service configuration.
- Suitability: Reasonable for this stateless Flask app if an external MySQL database is selected.
- Limitation: Adds an external database dependency and provider-network configuration.

### Option B: PythonAnywhere

- Flask support: Yes, through its WSGI configuration.
- MySQL support: Available according to plan and account limits; confirm the selected plan and database access before deployment.
- Deployment complexity: Low for a small Flask/MySQL application, but provider-specific WSGI and static-file mappings are required.
- Persistent files: Filesystem persistence depends on the account and should still be treated deliberately for future uploads.
- HTTPS: Provider HTTPS support is available; verify custom-domain requirements separately.
- Environment variables: Configure through provider web/WSGI settings, not committed files.
- Suitability: Strong fit for the current architecture if its MySQL and Python-version support meet the selected plan.
- Limitation: Provider-specific limits and supported Python versions must be checked before choosing it.

### Option C: VPS or cloud server

- Flask support: Yes, with Waitress behind a reverse proxy.
- MySQL support: Full control, including local or separately managed MySQL.
- Deployment complexity: Highest. The operator owns OS updates, firewalling, TLS, process supervision, backups, monitoring, and database security.
- Persistent files: Available on attached storage, but backups and durability remain the operator's responsibility.
- HTTPS: Must be configured and maintained by the operator or a managed reverse proxy.
- Environment variables: Configure through a protected service manager or secret store.
- Suitability: Appropriate when operational ownership and database control are required.
- Limitation: Greatest operational and security burden.

Recommended next-phase option: PythonAnywhere, subject to confirming MySQL availability and supported Python 3.14.x or a tested supported version. It matches the current Flask WSGI architecture with the least infrastructure work. This is a recommendation only; no account or deployment has been created.

## 3. Identify the database before any operation

Confirm the target from the platform configuration and record the database name without recording its password. For this project the normal local development database is configured separately from the isolated Phase 7 test database. Never run a production operation until the target host, port, database, and account have been independently checked.

The production database account should be least-privileged: application runtime access should not automatically imply permission to alter schema or restore data.

Required production schema tables are `users`, `owners`, `admins`, `vehicles`, `vehicle_routes`, and `bookings`, as defined in `database/vehicle_booking.sql`. The application uses MySQL host, port, database, user, and password environment variables. The explicit migration procedure is documented below; no cloud database is provisioned by this step.

## 4. Database backup

Create a logical backup before every production migration and before a release that changes database behavior. Store backups outside the repository in an access-controlled backup location. Keep multiple timestamped versions and define a retention period with the operations owner.

Use a password-safe MySQL credential method approved by the deployment environment, such as MySQL client login paths or the platform secret store. Do not put the password in a command argument, script, source file, shell history, or log.

Example command structure after configuring a protected MySQL client login path:

```powershell
New-Item -ItemType Directory -Force backups | Out-Null
mysqldump --login-path=businessvehiclebooking `
  --single-transaction --routines --triggers --events `
  --databases <TARGET_DATABASE> | gzip > backups\businessvehiclebooking_<UTC_TIMESTAMP>.sql.gz
```

On Windows, use an approved compression tool if `gzip` is unavailable. The target database placeholder must be replaced only after verification. Do not use `--databases vehicle_booking` without confirming that it is the intended target.

Verify the artifact without printing secrets:

```powershell
Get-Item backups\businessvehiclebooking_<UTC_TIMESTAMP>.sql.gz | Select-Object FullName, Length, LastWriteTime
Get-FileHash backups\businessvehiclebooking_<UTC_TIMESTAMP>.sql.gz -Algorithm SHA256
```

Record the checksum and backup metadata in the protected operations record. Never commit dumps, checksums containing sensitive operational context, or logs to Git.

## 5. Database restore

A restore can overwrite existing data. Do not perform one against a live database without an approved incident/change record and a verified backup.

1. Stop or isolate application traffic if required by the incident.
2. Confirm the target host, port, database, and account independently.
3. Verify the backup filename, size, checksum, and readable archive contents.
4. Take a fresh pre-restore backup if the target is reachable.
5. Restore only after explicit approval, using the protected MySQL credential method.
6. Verify expected tables, columns, indexes, foreign keys, and row-level smoke data.
7. Verify application connectivity using a read-only health/smoke check.
8. Run the appropriate isolated or staging test suite.
9. Restart application traffic and monitor logs and error rates.

Never restore a production dump into the normal development database without deliberate target confirmation, and never restore over the production database as an ad-hoc test.

## 6. Schema migration procedure

This project does not use Alembic or another migration framework. The current explicit migration entry point is `migrate_database.py`; it applies additive security/status columns and migrates legacy password values in a transaction. It must be run intentionally and is not called by `app.py` or `wsgi.py` imports.

For every future migration:

1. Review the SQL and identify affected tables, indexes, constraints, locks, and expected duration.
2. Create and verify a backup before changing any shared database.
3. Apply the migration to the isolated Phase 7 test database first.
4. Run the complete test suite and inspect schema metadata.
5. Rehearse on a staging copy if available.
6. Review the production target and migration account before execution.
7. Apply the migration deliberately during an approved change window.
8. Verify tables, columns, indexes, foreign keys, application startup, authentication, bookings, and admin flows.
9. Keep the pre-migration backup and migration output as the recovery point.
10. Document the result and any follow-up maintenance.

Do not use `DROP`, `TRUNCATE`, broad `DELETE`, or destructive reset commands as a migration shortcut. A migration framework should be considered before the schema grows further, but adding one is not part of this readiness step.

## 7. Rollback and recovery

### Application rollback

Application rollback means returning application code and dependencies to a previously verified release. It does not undo database changes. Stop new traffic if needed, switch the WSGI process to the prior release, verify its configuration, and run smoke tests against the current compatible schema.

### Database recovery

Database recovery means restoring or otherwise repairing database state. It can overwrite data and must use an approved backup, verified target, and recovery plan. It is separate from application rollback.

### Recommended order

1. Capture logs, timestamps, release identifier, and current symptoms without recording secrets.
2. Stop or isolate traffic if continued writes could worsen the incident.
3. Determine whether the failure is application-only, connection/configuration-only, or schema/data-related.
4. For an application-only failure, roll back the application first and verify compatibility.
5. For a failed migration or corrupted schema, preserve the evidence, take a current backup if safe, and restore the verified pre-migration backup only with approval.
6. Verify schema, connectivity, authentication, bookings, and admin access.
7. Resume traffic gradually and monitor.

There is no automatic database rollback or point-in-time recovery implemented by this project. Recovery depends on external backup and MySQL operational capabilities.

## 8. Logging and errors

The application uses Python logging and does not intentionally log passwords, session contents, tokens, secret keys, or database passwords. Production errors return generic responses while details go to server logs. Protect logs with access controls, retention limits, and redaction policies.

## 9. Pre-deployment checklist

- [ ] Production variables entered into the hosting platform's secret store.
- [ ] `APP_ENV=production`, `FLASK_DEBUG=0`, and secure cookies verified.
- [ ] WSGI command and trusted proxy topology reviewed.
- [ ] Least-privilege database account verified.
- [ ] Pre-change backup created, checked, and stored outside Git.
- [ ] Migration tested on the isolated test database and staging copy.
- [ ] Full test suite passes.
- [ ] Application import and WSGI import pass.
- [ ] Smoke-test plan prepared for registration/login, browsing, booking, owner management, and admin management.
- [ ] Rollback owner, backup location, and recovery decision-maker identified.

## 10. Post-deployment smoke tests

After deployment, verify the health endpoint or home page, login flows, vehicle search/details, booking validation and history, owner vehicle/booking management, admin authentication and status management, CSRF rejection, safe 404/500 responses, secure cookies, and absence of secret values in logs.
