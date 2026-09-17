# Worked example: aggregating for a European market

This example configures the location gate for a Berlin-based engineer who
wants German onsite/hybrid roles plus anything remote-eligible from Germany
(DE-remote, EU-remote, Europe-remote, EMEA-remote, or worldwide).

## 1. `config.yaml` — the location gate

```yaml
location:
  allowed_countries: [DE]
  allowed_cities: [berlin, munich, hamburg]
  remote_policy: allowed_countries
  allow_unknown: true
```

- `allowed_countries` takes ISO 3166-1 alpha-2 codes (`UK` is accepted as an
  alias for `GB`). Multiple codes work: `[DE, NL, AT]`.
- A remote posting passes when its stated area **covers** an allowed country:
  "Remote — Europe", "Remote — EMEA", and "Worldwide" all pass for `DE`;
  "Remote — US" and "London, UK" do not. A posting that pairs a generic
  "Anywhere"/"Worldwide" token with an explicit country — "Remote - Anywhere
  in the U.S." — counts as that country, not as worldwide, so it does not
  pass for `DE` either.
- `allowed_cities` gates onsite/hybrid postings, exactly as in the US setup.

## 2. `profile.md` — keep the LLM in sync (required!)

The hard gate above runs *before* LLM scoring, but the scoring profile
independently down-scores anything it was told is a dealbreaker. The stock
profile says "US-based only" — if you don't update it, postings the new gate
passes will be scored ≤ `score_low` and suppressed anyway. Replace the
geography rule in the "Weak fit / not interested" section with something like:

> **Any role whose primary location is outside Germany or not workable from
> Berlin.** Score ≤ 2 for onsite/hybrid roles in other countries and any
> "US-only" / "APAC" / "LATAM" remote listing. Remote roles open to Germany,
> the EU, EMEA, or worldwide are fine.

Import both files (`python -m src.settings import DIR`); the new gate and
profile apply live.

## 3. Known caveats

- **Compensation floor is USD-only.** `comp_floor_usd` compares raw numbers;
  a `€85.000` posting is not converted. For non-US markets either set
  `comp_floor_usd: 0` (comp filtering off) or accept that only postings
  advertising USD figures are comp-filtered. Postings with no comp are never
  rejected by comp.
- **Discovery sources skew US.** yc-oss, the enterprise seed CSV, and
  `manual_companies` are US-curated. The gate change surfaces European roles
  on boards you already poll; turn on `discovery.eu_seeds_enabled` to widen the
  funnel automatically. (`sources.hiringcafe.extra_queries` used to be a second
  lever, but the hiring.cafe connector is disabled and blocked upstream, so
  those entries are inert.)
- The legacy key `remote_must_be_us: true|false` still works (it maps to
  `remote_policy: allowed_countries|anywhere`) but warns; migrate when
  convenient.
