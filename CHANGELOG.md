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

## V3 update
- Added account deletion beside Log out, with a confirmation prompt. Deleting an account also removes its alerts, saved webhook destinations, and delivery history.
- Added Edit webhook to each saved destination's three-dot action menu.
- Saved webhook editing validates the HTTPS destination and updates the saved name, URL, and optional secret.


## Closing condition update
- Replaced separate Closes Above and Closes Below choices with one Closing option.
- The form previews Closes Above/Closes Below by comparing the target with the live mid-price, refreshing every 10 seconds while the form is open.
- The server checks the current mid-price again when saving and stores the detected direction for the alert; it does not flip direction later as the market moves.
- Candle timeframe remains available for Closing alerts and uses completed candles only.

## Condition wording update
- Renamed the condition choices to **Crosses** and **Closes**.
- Webhook messages now use present-tense wording: `crosses`, `closes above`, and `closes below`.

## Webhook wording correction
- Kept the site condition choices as **Crosses** and **Closes**.
- Restored webhook message wording to `crossed`, `closed above`, and `closed below`.

## Asset favorites
- Added a star toggle beside each asset in the symbol search list.
- Favorited assets are grouped at the top when the symbol search is opened, above the full asset catalogue.
- Favorites are saved in the browser on the current device.
