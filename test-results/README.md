# Test results

**1,997 test cases · 1,992 passed · 0 failed · 5 skipped**
Run on 16 Nov 2026 with Python 3.12, against a fresh demo database, AI on its built-in fallbacks.

Every individual case is listed in **[TEST_CASES.csv](TEST_CASES.csv)** — one row per
test with its area, the exact case, what it checks, the result and how long it took.
GitHub shows it as a searchable table; it also opens in Excel or Google Sheets.

## By area

| Area | Passed | Skipped | Failed |
|---|---:|---:|---:|
| Matrix A · Every page × every visitor | 495 | 0 | 0 |
| Matrix B · Cross-studio reads | 52 | 0 | 0 |
| Matrix C · Control cases | 6 | 0 | 0 |
| Matrix C · Hostile requests | 320 | 4 | 0 |
| Matrix D · Cross-studio writes | 80 | 1 | 0 |
| Matrix E · Hostile form input | 289 | 0 | 0 |
| Matrix E · Stored input rendered safely | 14 | 0 | 0 |
| Matrix F · Money formatting | 247 | 0 | 0 |
| Matrix G · Assistant parsing | 52 | 0 | 0 |
| Matrix H · Passwords, redirects, lockout | 44 | 0 | 0 |
| Matrix I · Totals, money input, tracking | 90 | 0 | 0 |
| AI agents | 36 | 0 | 0 |
| Booking scheduling | 9 | 0 | 0 |
| Business intelligence | 34 | 0 | 0 |
| Concurrency | 14 | 0 | 0 |
| Core services | 48 | 0 | 0 |
| Currency | 8 | 0 | 0 |
| Customer follow-ups | 19 | 0 | 0 |
| Database upgrades | 2 | 0 | 0 |
| Estimate approvals | 12 | 0 | 0 |
| Invoices, assistant & security | 45 | 0 | 0 |
| Job records | 27 | 0 | 0 |
| Production hardening | 15 | 0 | 0 |
| Time corrections | 5 | 0 | 0 |
| Tracking links & portal | 16 | 0 | 0 |
| Vehicle workflow | 13 | 0 | 0 |
| **Total** | **1,992** | **5** | **0** |

The 5 skipped cases are the login form in the "hostile request" and "cross-studio"
groups — signing in is meant to work without an existing session.

## What the matrix covers

The **Matrix** rows come from [tests/test_matrix.py](../tests/test_matrix.py), which builds its cases
from the app's own route list, so a new page or action is tested automatically.

| Group | What it proves |
|---|---|
| A · Every page × every visitor | 99 pages × logged-out visitor, owner, manager, technician and another studio's owner: nothing crashes, strangers are sent to login |
| B · Cross-studio reads | Every page that takes a record id, opened by studio 2 with studio 1's ids: studio 1's data never appears |
| C · Hostile requests | Every action sent with no session, from another website (Origin or Referer), or with a forged `null` origin: no data changes |
| C · Control cases | The same kinds of request done legitimately *do* change data — proof the checks above can see a change |
| D · Cross-studio writes | Every action, signed in to studio 2 but aimed at studio 1's records: studio 1 is untouched |
| E · Hostile form input | 17 forms × 17 inputs — script injection, SQL injection, template injection, 20,000-character text, null bytes, NaN, infinity, path traversal, header injection: no crash, and nothing executable is shown back |
| F · Money formatting | 16 currencies × 14 amounts, including Indian grouping (1,80,000), rounding and negatives |
| G · Assistant parsing | "takes 2 days" → 48 h, "45 minutes" → 0.75 h; "rupees", "₹", "Rs." → INR |
| H · Passwords, redirects, lockout | Weak passwords rejected; open-redirect tricks blocked; lockout exactly at the limit, shared across workers |
| I · Totals, money input, tracking | 60 randomised invoices add up; "1.005" → ₹1.01; "inf"/"1e400" rejected; tracking stages always consistent |

## Bugs these tests found (all fixed)

The first run of the matrix failed 37 cases; 36 were real bugs, each one an
"Internal Server Error" from input someone could actually type:

- New lead and new inventory item crashed on any non-number in a number field (31 cases)
- New dispatch job crashed on a very large price
- Invoices crashed on `inf` / `1e400`, and saved `1.005` as 1.00
- Currency formatting crashed on `NaN`

The same unsafe number parsing was fixed in eight more places (estimates, products,
stock adjustments, AI quick quote, agent feed). All now use one strict parser
([src/forms.py](../src/forms.py)) that answers "must be a number" instead of crashing,
and rejects negative prices and quantities.

## Also tested outside this suite

On a production setup — gunicorn with 3 workers, `APP_ENV=production`, HTTPS as nginx forwards it:

| Check | Result |
|---|---|
| HTTPS login, Secure/HttpOnly/SameSite cookies, HSTS, Content-Security-Policy | Passed |
| Cross-site request blocked, friendly 404 page | Passed |
| Tracking links built with the public https domain | Passed |
| Login lockout shared across all workers; other IPs unaffected | Passed |
| 504 requests from 12 users at once, including 72 writes | Passed, 0 errors |
| 450 writes from 30 users at once | Passed, all saved |
| Every page as owner and technician | Passed, 0 server errors |
| Studio Assistant with the real local AI: prompt injection, requests for secrets, requests about another studio, gibberish | Never changes data while planning; refuses other studios; never claims access to credentials |

## Run it yourself

```bash
python -m pytest tests                              # everything
python -m pytest tests/test_matrix.py               # just the matrix
python -m pytest tests --junitxml=results.xml       # machine-readable results
```
