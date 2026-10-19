SYNTHETIC DOCUMENT - Northwind Aerial Robotics (fictional): SkyFleet API Reference v3. Written for evaluation and demos; it describes no real company.

# SkyFleet API Reference v3

The SkyFleet API manages drones, missions and flight data. The base URL is `https://api.skyfleet.example/v3`.

## Authentication

Every request sends a bearer token in the `Authorization` header. Tokens are created in the SkyFleet console, carry scopes such as `missions:write`, and expire after 90 days.

```bash
curl -H "Authorization: Bearer $SKYFLEET_TOKEN" \
  "https://api.skyfleet.example/v3/drones?page_size=50"
```

## Rate limits

Limits apply per organization and depend on the plan.

| Plan | Requests per minute | Burst | Concurrent uploads |
|---|---|---|---|
| Starter | 60 | 10 | 2 |
| Professional | 600 | 100 | 10 |
| Enterprise | 3,000 | 500 | 50 |

When a limit is exceeded the API responds with HTTP 429 and a `Retry-After` header that gives the number of seconds to wait before retrying.

## Pagination

List endpoints return at most `page_size` items. The `page_size` parameter defaults to 50 and may not exceed 200. Pass the `next_cursor` value from a response to fetch the next page.

## Errors

| HTTP status | Error code | Meaning |
|---|---|---|
| 400 | invalid_request | The request body or a parameter is malformed |
| 401 | invalid_token | The token is missing, expired or revoked |
| 403 | insufficient_scope | The token lacks the scope the endpoint needs |
| 404 | not_found | The resource does not exist in your organization |
| 409 | mission_conflict | The drone is already assigned to an active mission |
| 429 | rate_limited | A rate limit was exceeded |

## Webhooks

SkyFleet sends webhook events for mission status changes. Each delivery is signed with HMAC-SHA256 using the endpoint's signing secret, and the signature is sent in the `X-SkyFleet-Signature` header. Failed deliveries are retried up to 8 times over 24 hours.
