# Historical remediation acceptance contract

This engineering contract applies to the candidate descending from commit
`18ee8ab82f22ea03ff1a1eb04b2edfb52ab3e93c` (tree
`71d681b8a350b8f33c21d5867e8e17289da6131f`) and HA Core 2026.9.3 / CPython
3.14.2 with the existing lockfile. Implementation evidence is distinct from
independent review. The candidate is not a released version or an installation
artifact; the version remains unchanged during this work.

## Entity purposes (A1 / R1-B4, M7-U5)

The following declaration is independent of the production descriptions. Every
identity remains `csg_plus.<account>.<suffix>`, with the same translation key,
ConfigEntry, device, and source. Snapshot values are observations for display;
authoritative Energy history comes from separate external statistics.

| Translation key | Suffix | Source / purpose | Device class | Native unit | State class |
| --- | --- | --- | --- | --- | --- |
| yesterday_usage | yesterday_kwh | Published daily usage for strictly Shanghai yesterday | energy | kWh | none |
| balance | balance | Current account balance; signed | monetary | CNY | none |
| arrears | arrears | Current account arrears | monetary | CNY | none |
| current_ladder | current_ladder | Profile-derived current tier | none | none | none |
| current_ladder_remaining | current_ladder_remaining_kwh | Profile-derived tier allowance | energy | kWh | none |
| current_ladder_tariff | current_ladder_tariff | Current marginal unit price, not an official bill | none | CNY/kWh | measurement |
| latest_settlement_usage | latest_settlement_day_kwh | Latest accepted published daily usage and its fact date | energy | kWh | none |
| latest_settlement_cost | latest_settlement_day_cost | Unavailable placeholder; no authoritative daily cost source | monetary | CNY | none |
| this_month_usage | this_month_total_usage | Official API current-month total; may revise | energy | kWh | none |
| this_month_cost | this_month_total_cost | Unavailable placeholder; no authoritative current daily-cost total | monetary | CNY | none |
| last_month_usage | last_month_total_usage | Official closed-month bill usage | energy | kWh | none |
| last_month_cost | last_month_total_cost | Official closed-month bill cost | monetary | CNY | none |
| this_year_usage | this_year_total_usage | Official API current-year usage total | energy | kWh | none |
| this_year_cost | this_year_total_cost | Official API current-year charge total | monetary | CNY | none |
| last_year_usage | last_year_total_usage | Official API previous-year usage total | energy | kWh | none |
| last_year_cost | last_year_total_cost | Official API previous-year charge total | monetary | CNY | none |

Fourteen energy/monetary snapshots must have no state class. Tier has no LTS
semantics. Unit price alone may produce measurement mean/min/max, with no sum.
Missing placeholders remain unavailable, never zero or tariff-derived charges.
Ordinary state history may still exist. Independent external energy and official
monthly cost retain their existing source, account hash, IDs, units, sums and
monthly anchor. Metadata changes do not erase existing snapshot statistics.

Acceptance evaluates all 16 entities through real SensorEntity lifecycle with
synthetic valid values, zero and unavailable, rejects sensor metadata/unit
warnings, and compiles actual sensor Recorder statistics in temporary databases.
Both clean-database and synthetic beta.1-to-candidate observations are required.

## Response and candidate boundaries (B1 / R1-B2, M0-B1/B6, M2-B3)

| Input | Accepted value / scope | Failure and consumer contract |
| --- | --- | --- |
| Monthly daily response | Mapping with `result` list and `totalPower` field | Invalid response/container raises a safe typed validation error; an empty list is valid |
| `totalPower`, `totalBillingElectricity` | Finite nonnegative numeric value or missing | Missing/invalid total is unavailable, never a manufactured zero; valid independent rows survive |
| Daily `power` / normalized `kwh` | Finite nonnegative numeric value; boolean excluded | Invalid numeric row retains its valid date marker for coverage; it is not a fact |
| Daily date | Valid ISO calendar date in the requested month, no later than the Shanghai day captured for validation | Out-of-request and future dates are rejected before display or fact publication |
| Monthly bill response | Mapping with `electricAndChargeList` list | Invalid batch is distinguished from an empty list; other accounts/years remain independent |
| Monthly bill key | API YYYYMM or YYYY-MM, valid calendar month in requested year | Malformed/wrong-year rows cannot update facts or closed-month snapshots |
| Monthly bill usage | Finite nonnegative; missing field preserves existing fact field | Same-response conflicts reject the whole affected month; other months survive |
| Official monthly bill cost | Existing finite nonnegative official bill domain | Keep existing bill/Bridge semantics; no inference from daily sums or tariffs |
| `totalActualAmount` | Finite monetary total; signed monetary semantics are kept distinct from usage | Missing/invalid charge is unavailable independently of valid usage and monthly rows |
| Account balance / arrears | Finite signed monetary observation, boolean excluded | Do not apply daily-usage nonnegativity to money mechanically; missing field is unavailable |
| All conversions | Reject NaN, infinity, booleans and conversion overflow | No broad exception-to-zero fallback; errors omit payload/account/token values |

Requests and business dates use Asia/Shanghai. Future means after today's
Shanghai natural date, not after yesterday. Existing published-day semantics,
yesterday identity and coverage completeness are retained. Midnight, month/year
boundaries, leap dates, request scope and explicit markers require tests.

Daily duplicates retain the existing full-range absolute tolerance of 1e-9 and
minimum actual source representative. Conflicts are not resolved by arrival
order. Monthly conflicts retain existing per-field exact agreement. Display
consumers use this batch's accepted candidates, never an unlabelled old Store
value. Invalid candidates do not delete old facts. Cross-fetch legitimate upward
and downward revisions, same-value persistence and materialization retries stay
valid. Markers never become usage facts or fake complete coverage.

Required examples include both 5/7 orderings with old fact 4, wrong requested
month, conflicting 10/5 versus 20/9 bills, invalid total/container/nonmapping/bool/
overflow cases, valid zero, duplicate agreement and legitimate revisions. The
September 3 / September 30 future-day case must traverse actual synthetic client
conversion, coordinators, Store, Bridge and temporary Recorder. That expands the
original evidence beyond Store/statistics construction; it does not demonstrate
any user's database impact.

## V1 persisted business schema (B2 / M1-B2/B3/B5)

No storage version, key or identity migration is permitted. A nonexistent file
initializes by the existing contract. An existing invalid wrapper or invalid
business payload must fail before publishing runtime data and must retain its
exact bytes and mtime without save, deletion or automatic replacement.

| Existing V1 shape | Compatibility requirement |
| --- | --- |
| Empty business mapping or absent `accounts` | Valid empty state; initialize defaults in memory |
| `accounts` mapping; string account keys; account mapping | Missing known account collections remain valid and get in-memory defaults |
| `daily_usage` mapping of valid ISO days to fact mappings | Finite nonnegative `kwh`; known source/timestamp fields have safe types; retain valid zero and updated_at |
| `monthly_bills` mapping of valid YYYY-MM to bill mappings | Usage-only or cost-only bills remain valid; known fields finite in existing domains |
| `daily_coverage` mapping of valid months to coverage mappings | Validate known state/count/date/list fields and their container types |
| `monthly_reconciliation` mapping of valid months to result mappings | Validate known finite sums, nullable bill/difference, state and timestamp; optional compatible freshness fields may be additive |
| Missing/partial `sync`, malformed history progress members | Retain approved progress normalization and retryability; business fact validation cannot reject progress-tolerant V1 files |
| Unknown harmless extension fields | Preserve; validate necessary known business fields only |
| Unknown storage version | Diagnose and stop; do not invoke destructive migration or change storage key |

Internal month input must be two non-boolean integers, year 1..9999 and month
1..12. Nonmapping rows and huge conversions are safely rejected. Validation
errors report structural location and category without raw keys/accounts/values.
Real HA Store wrappers and temporary files must prove hash/mtime/save invariance
for corruption and equivalent reload for legitimate beta.1 files.

## Setup failure ownership (B2 / M2-B2)

| Failure stage | Entry-owned resources | Required outcome |
| --- | --- | --- |
| Session verification | Client verification executor call; no producers | Existing authentication versus transient connection classification |
| History load | Real Store readers and existing physical I/O lane | Transient storage failure follows supported retry; deterministic schema corruption fails diagnostically; no overwrite |
| Bridge creation/runtime publication | This entry's Bridge and runtime reference | Stop requests and drain existing lifecycle facilities before removing runtime |
| Forward before platform start | Published Store/Bridge | Dynamically distinguish actual forward exceptions from Core-handled platform failure |
| Forward after partial platform start | Realtime/Billing producers, refresh tasks, platform entities/listeners/timers | Quiesce producers, drain/abort through existing facilities, finalize Bridge, unload entry platforms, then remove runtime |
| History task start / setup sync | Above plus optional history producer/Bridge worker | Same entry-only cleanup; preserve physical writer and lane ownership until actual completion |
| Cleanup fails again / cancellation | Any still-owned producer, disk operation or lane | Retain diagnostic runtime/ownership; no replacement writer can pass an old writer |

Tests cover load, early/partial forwarding, failure/retry success, unload/reload,
active producer and pending-write cancellation, multiple-entry isolation and
cleanup failure. Real Core/Store/Recorder observations and mock injection results
are reported separately. No history_io or Bridge ownership redesign, new long
running cleanup task, arbitrary external same-key writers or OS power-loss
certification is included.

## Reconciliation currentness (B3 / M1-B4, M2-B4)

The minimum compatible solution must make an old calculation distinguishable
from one applicable to current daily and monthly facts. Facts changing in memory
invalidate currentness even if saving fails. Same-value refetch does not invent
a revision; successful persistence retry does not recalculate the business
comparison. Different accounts/months remain isolated. Checked time never means
complete collection or confirmed persistence. Calendar transitions and reload
must expose stale or unknown currentness conservatively.

The implementation may add compatible freshness/revision information while
retaining comparison values, existing usage-state meanings and Decimal tolerance.
Legacy results without freshness evidence are not silently labelled current.
Any incompatible schema migration requires a new decision. Bridge continues to
read facts independently of reconciliation and no unbounded recomputation is
introduced. The concrete compatible choice and behavior evidence are recorded
before G4 production changes.

## Group and risk gates (A2)

G0 introduces this contract and the 77-record public ledger and set gate; G1
implements A1; G2 implements B1 and the shared D5 conversion/date validation;
G3 implements B2 using that helper; G4 implements B3; G5 reconciles all groups,
tests and CI. Each group has a separate conventional commit and candidate HEAD/
tree, file/finding mapping, ledger version and test evidence.

The original 77-record audit ledger remains immutable outside the repository.
Public records preserve IDs, relationships, original severity and beta.1
classification separately from priority, disposition, implementation and review.
All 25 active source records require ownership, disposition and acceptance or
revisit criteria. New records have their own collection. Implementation passes
are `IMPLEMENTED_PENDING_REVIEW`; independent closure needs reviewer, exact HEAD
and evidence. Empty set difference establishes completeness, not zero risk.

Deferred performance, lane retention, extra sync requests, minute guard and
physical disk ownership/exit limits remain explicit. HACS/hassfest Actions,
real cloud coexistence and real HA/Python 3.14.6 acceptance remain unverified.
Real Beta A stays STOP; that does not mean its integration has been disabled.
Public known-problem/Issue/Release drafts and a later read-only inventory plan
remain unpublished and outside this repository. Real access, cleanup, migration,
release, merge and Ready transitions require their own authorization.
