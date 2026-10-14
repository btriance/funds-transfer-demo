# CR-001 — Daily Transfer Limits and Step-Up Verification

| | |
|---|---|
| **Applies to** | [Funds Transfer Service v1.0](../funds-transfer-spec.md) |
| **Status** | Implemented |
| **Architecture impact** | None. No new components, external dependencies, or trust boundaries. Changes the transfer flow and adds an authentication step. |

## Why

If a customer's credentials are stolen, an attacker can currently drain the account to any payee in one request. This CR caps daily payee transfers and requires a one-time passcode (OTP) for larger transfers.

## Changes

### Daily payee limit

- Each customer has a `daily_payee_limit`. The default is $3,000.00, and seed users get the default.
- A payee transfer is rejected with `DAILY_LIMIT_EXCEEDED` if the new amount, plus all of today's `completed` and `pending_verification` payee transfers, would exceed the limit.
- "Today" means the current calendar day in `America/Toronto`.
- Transfers between a customer's own accounts don't count toward the limit and aren't restricted by it.
- Customers can't change their own limit in this CR.

### Step-up verification

- Payee transfers of **$1,000.00 or more** require an OTP before any money moves.
- For these transfers, `POST /transfers` returns `202 Accepted` with `status: "pending_verification"` and a `verification` object containing `challenge_id` and `expires_at`. Balances don't change at this point.
- The OTP is a 6-digit code that's valid for 5 minutes and can only be used once.
- The customer submits the code to `POST /transfers/{transfer_id}/verify`. If it's correct, the API re-checks the balance and daily limit, completes the transfer, and returns `200` with `status: "completed"`.
- After 3 incorrect attempts the transfer moves to `cancelled`. If it isn't verified within 5 minutes, it moves to `expired`.
- OTP delivery goes through an `OtpSender` interface. The dev implementation writes the code to the application log so the demo can run without a real SMS provider. In production, the bank's existing SMS or push service would implement this interface.

### API

| Method | Path | Roles | Description |
|---|---|---|---|
| POST | `/transfers` | customer | *Changed:* may return `202` with `pending_verification` (see above) |
| POST | `/transfers/{transfer_id}/verify` | customer | Submit `{"code": "123456"}` to complete a pending transfer |
| GET | `/limits` | customer | Returns `daily_payee_limit`, `used_today`, `remaining_today`, and `step_up_threshold` |

New error codes: `DAILY_LIMIT_EXCEEDED`, `VERIFICATION_FAILED`, `VERIFICATION_EXPIRED`, `TRANSFER_NOT_PENDING`.

### Data model

- **User:** add `daily_payee_limit_cents`.
- **Transfer:** `status` gains the values `pending_verification`, `cancelled`, and `expired`.
- **VerificationChallenge (new):** id, transfer_id, code_hash, expires_at, attempts, consumed_at, created_at.

### Audit

New audited actions: `step_up_challenge_issued`, `step_up_verified`, `step_up_failed`, `transfer_limit_exceeded`.

## Acceptance Criteria

| ID | Criterion |
|---|---|
| CR1-AC-1 | Alice sends Bob $200.00. It completes immediately, with no OTP required. |
| CR1-AC-2 | Alice sends Bob $1,500.00. She gets `202` / `pending_verification` and her balance is unchanged. After she submits the correct OTP, the transfer completes. |
| CR1-AC-3 | Three wrong OTPs cancel the transfer, and a 4th attempt returns `TRANSFER_NOT_PENDING`. |
| CR1-AC-4 | An OTP submitted after 5 minutes returns `VERIFICATION_EXPIRED`, and no money moves. |
| CR1-AC-5 | After $2,500.00 in payee transfers today, a $600.00 payee transfer is rejected with `DAILY_LIMIT_EXCEEDED`. |
| CR1-AC-6 | Alice can still move $10,000.00 from savings to her own chequing account. |
| CR1-AC-7 | `GET /limits` reflects both completed and pending payee transfers. |
| CR1-AC-8 | All baseline acceptance criteria still pass. |
