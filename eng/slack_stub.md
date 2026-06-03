Example Slack payloads and expected responses

Happy path
```
{ "event": { "text": "Update customer onboarding profiles to change 'risk_status' to ENUM" } }
```

Expected: Policy clears, PR URL posted back to the channel.

No‑Go path
```
{ "event": { "text": "Change ledger integer format across the whole core ledger framework" } }
```

Expected: Policy denies and returns scope split recommendation.
