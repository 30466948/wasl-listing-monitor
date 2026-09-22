# Data source decision record

## Discovery run 1 (2026-09-22, GitHub-hosted runner, Azure North Central US)

| Probe | Result |
|---|---|
| robots.txt | Drupal default. `/en/search` and `/en/unit` allowed. Sitemaps declared. |
| Sitemap | `/en/sitemap.xml` has 348 URLs with lastmod, but only 6 unit pages: not a listing detector. |
| Plain HTTP (requests) | 302 to `validate.perfdrive.com` = **Radware Bot Manager captcha**. Cookies `__uzm*`. Unusable. |
| Headless Chromium | 200 from www.wasl.ae, 217 KB, Drupal (`x-drupal-dynamic-cache`). Page passed the JS challenge. |
| Results grid | Filled by JavaScript after load. First snapshot (7.5 s) still showed the default card set, no counter. |
| Card format | `h3` "R441 - al barsha"; `span` "Al Barsha First"; `h4`s "Unit No. 604", "Price <dirham glyph> 101,999 / Year", "Size (Sq.ft.) 1,355.00", "Type: 2 room flat", "Parking: PB255". Detail link `/en/unit/residential/IM001...`. |
| Pagination / coverage probes | All returned 0 refs; probably later navigations were served challenge pages. Re-probed in run 2 with titles logged. |

Decisions:

1. `fetch.strategy: browser`. The HTTP path is not attempted; every monitor run uses Chromium
   (full `channel=chromium`, `AutomationControlled` disabled, Dubai timezone).
2. Wait for the results counter or an explicit no-results message in the page's visible text
   (`innerText`), after `networkidle`, up to 25 s.
3. Card selector `section.all-units-cards`; heading `h3` gives building code and community;
   rent, size, bedrooms and unit number are parsed from the labelled `h4` texts.
4. Off-site redirect (`final_url` host differs) is a challenge signal in its own right.

## Discovery run 2 (2026-09-22 04:33 UTC, commit 4cd4318)

| Probe | Result |
|---|---|
| Browser, first navigation | 200, 221 KB, Drupal headers, waited 39.6 s: **no results counter ever appeared**. The page shows the same 10 default units as run 1 (Al Barsha, Al Hudaiba, Naif, Al Sabkha, Al Murqqabat, Al Garhoud) with the community/room/usage filters ignored. No listing API call was made by the page; the only wasl XHRs were the bot-manager fingerprint POST and `/en/search/suggestions` (autocomplete). |
| Browser, every later navigation | Redirected to `validate.perfdrive.com` ("Radware Captcha Page"): pagination and building probes all blocked. |
| Parsers | Correct on all 10 real cards: rent `Price <glyph> 101,999 / Year` -> 101999, `Type: 2 room flat` -> 2, `Size (Sq.ft.) 1,355.00` -> 1355, heading `R441 - al barsha` -> code R441 / community al barsha, location, unit no. Each card appears twice in the DOM (grid + list view); extraction now de-duplicates by ref. |
| Autocomplete data | `/en/search/suggestions` returns 350 slugs grouped LOCATIONS / communities / buildings; useful to verify `wasl-village` exists, not for listings. |

Conclusion (Gate 1): wasl.ae is protected by Radware Bot Manager, which classifies the GitHub-hosted
runner (Azure datacenter IP + headless Chromium) as a bot after its JavaScript fingerprint runs.
The generic first page is consistent with a "serve alternate content" bot response. Because the
monitor requires the results counter before it trusts a page, such runs are reported as DEGRADED
(`counter_missing`), never as "no new listings". A different vantage point (residential network
and a real browser) is needed; see README "Deployment options".
