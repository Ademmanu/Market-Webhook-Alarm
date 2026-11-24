# Changelog

## v2.0.0
- Added per-user accounts with generated `...alarm` usernames and separate passwords.
- Added 24-hour secure sessions, CSRF protection, and account lockout.
- Added alert labels, editing, webhook test, once/repeat modes, cooldown, UTC expiry, manual re-arm, and bulk pause/resume.
- Added per-user saved webhook destinations and delivery history.
- Added persistent delivery queue with up to three retries after the initial attempt.
- Added live dashboard prices, target-distance percentages, feed status, and counters.
- Rebuilt instrument picker as a searchable dropdown with browse-on-focus.
- Corrected biquote batch quote parsing to support the documented symbol-keyed JSON response.
- Added symbol catalogue cache, feed outage backoff, database health check, and legacy alert-table column migration.
- Resume/re-arm/edit now clear the previous quote baseline to prevent stale-price immediate triggers.
