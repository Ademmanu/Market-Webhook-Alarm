# Changelog

## v2.1.0
- Users choose their own username during registration; no generated `alarm` suffix.
- Added confirm-password field and show/hide controls for admin, registration, and login passwords.
- Reduced minimum personal password length to 6 characters.
- Renamed the registration field label to “Admin password”.
- Removed automatic database migration/ALTER TABLE logic; startup only creates missing tables. Existing database must already have the v2 schema.

## v2.0.0
- Added per-user accounts, secure sessions, CSRF protection, and account lockout.
- Added alert labels, editing, webhook test, once/repeat modes, cooldown, UTC expiry, manual re-arm, and bulk pause/resume.
- Added per-user saved webhook destinations and delivery history.
- Added persistent delivery queue with up to three retries after the initial attempt.
- Added live dashboard prices, target-distance percentages, feed status, and counters.
- Rebuilt instrument picker as a searchable dropdown with browse-on-focus.
- Corrected biquote batch quote parsing and added symbol cache, feed outage backoff, and health checks.
- Resume/re-arm/edit clear the previous quote baseline to prevent stale-price immediate triggers.
