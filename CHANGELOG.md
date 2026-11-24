# Changelog

## V3
- Removed all database-reset code and reset environment configuration.
- Added Crossing, Closes Above and Closes Below conditions.
- Added completed OHLC candle monitoring for 1m, 5m, 15m, 30m, 1h, 4h and 1d.
- Removed the price-field selector; all live and candle conditions use mid-price.
- Webhook payload now includes a human-readable `message` field.
- Replaced per-alert action buttons with three-dot action menus.
- Added asynchronous background form actions with dismissible five-second toast notifications.
- Added conditional display for candle timeframe, repeat cooldown, expiry and saved-webhook name.
- Kept the existing database schema; no new columns or migration are required.
