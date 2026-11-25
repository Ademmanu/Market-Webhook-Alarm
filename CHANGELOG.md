# Changelog

## Bid price + close-alert reliability update
- All live prices now use the **bid** automatically instead of mid: Crosses ticks, the closing-direction preview/detection, the dashboard's Live bid column, `/api/quote`, and the webhook payload (`price_field` is now `bid`).
- Close alerts (Closes Above / Closes Below) now fire when a completed candle closes beyond the target. Previously the candle before it also had to be on the other side of the target, so a close could be missed when the last completed candle was already beyond the target.
- Every newly completed candle is now judged in order, so a close that happened between two checks (for example while the instance was waking up) is no longer skipped.
- The candle feed parser accepts a bare list or a `bars`/`candles`/`items`/`data` wrapper, and orders candles by their timestamps instead of assuming newest-first.
- A failed live-tick request no longer blocks close alerts, and one failing alert no longer stops the others in the same cycle.
- A warning is logged (at most every 10 minutes per symbol) when no completed candles or no bid price can be read, instead of failing silently.
- `/health` now includes `monitor_last_cycle` and `monitor_age_seconds`.
- New module `candles.py` holds the close-alert logic. No database changes.
- Alert status is now **Armed**, **Ringed** (it has fired) or **Dis-armed** (switched off by you). A once-alert that has fired shows Ringed instead of Paused, and the badge updates live without reloading the page.
- Replaced Pause/Resume with **Dis-arm**/**Re-arm** (per alert, plus **Dis-arm all** and **Re-arm all**). Dis-arm all dis-arms every alert, so all of them show **Dis-armed** afterwards, including ones that had rung.
- A repeat alert shows **Ringed - Cooling down** while its cooldown timer is running, then **Ringed - Waiting** while it waits for the price to return across the target and re-arm itself (no Re-arm in its menu then, since it is already waiting to re-arm). Plain **Ringed** is for fire-once alerts that have rung.
- Saving an edit now always re-arms the alert, even if it was dis-armed or had already rung.
- "Fire once — pause after firing" is now "Fire once — dis-arm after firing".
- The dashboard's live bid prices and the closing-direction preview in the alert form now refresh every 5 seconds (was 10). The server-side monitor still polls every `POLL_SECONDS` (default 2).

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
