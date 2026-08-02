# Admin Endpoints

Admin endpoints require the normal bearer token for a user whose `users.is_admin` value is `1`.

For local development, promote a user directly in SQLite:

```sql
UPDATE users SET is_admin = 1 WHERE username = 'admin_username';
```

## List Refund Requests

```http
GET /admin/refunds?status=pending
Authorization: Bearer <admin_token>
```

Allowed `status` values:

- `pending`
- `approved`
- `rejected`
- `all`

Response:

```json
{
  "refunds": [
    {
      "booking_id": 12,
      "flight_instance_id": "AI101:a1b2c3d4e5f6",
      "flight_number": "AI101",
      "departure_time": "2026-07-17T10:30",
      "travel_class": "economy",
      "seats": 2,
      "amount": 12000,
      "status": "confirmed",
      "refund": {
        "status": "pending",
        "reason": "Trip cancelled",
        "requested_at": "2026-07-20T09:30:00",
        "admin_note": null,
        "resolved_at": null
      }
    }
  ]
}
```

## Approve Or Reject Refund

```http
POST /admin/refunds/{booking_id}/decision
Authorization: Bearer <admin_token>
Content-Type: application/json
```

Approve:

```json
{
  "action": "approve",
  "note": "Approved under cancellation policy"
}
```

Reject:

```json
{
  "action": "reject",
  "note": "Flight is not eligible for refund"
}
```

Behavior:

- `approve` changes the booking status to `refunded`.
- `approve` changes refund status to `approved`.
- `approve` returns the seats to the exact `flight_instance_id`.
- `approve` refreshes Redis seat cache and publishes an SSE seat update.
- `reject` keeps the booking confirmed.
- `reject` changes refund status to `rejected`.

Only pending refund requests can be approved or rejected.
