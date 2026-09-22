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
