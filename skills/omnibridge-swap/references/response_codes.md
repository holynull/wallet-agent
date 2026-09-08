# OmniBridge API Status Codes and Refund Reasons

## 1. Response Status Codes (`resCode` / `resMsg`)

OmniBridge API responses usually contain `resCode` and `resMsg` fields to indicate the result of the API call.

| `resCode` | `resMsg` | Description           |
| :-------- | :------- | :------------- |
| `800`     | `Success`| API call succeeded. |
| `Other`   | `Error Msg` | API call failed, `resMsg` will contain specific error details. |

## 2. Detailed Order Status (`detailState`) Explanations

The `detailState` field returned when querying order status indicates the detailed processing workflow and current stage of the order.

1.  `wait_deposit_send`: Waiting for deposit to be sent.
2.  `timeout`: Order timed out.
3.  `wait_exchange_push`: Waiting for exchange info push.
4.  `wait_exchange_return`: Waiting for exchange info return.
5.  **Receive Token Flow (5.1):**
    *   `wait_receive_send`: Waiting for receive token to be sent.
    *   `wait_receive_confirm`: Waiting for receive token confirmation.
    *   `receive_complete`: Receive token confirmation completed.
6.  **Refund Flow (5.2):**
    *   `wait_refund_send`: Waiting for refund to be sent.
    *   `wait_refund_confirm`: Waiting for refund confirmation.
    *   `refund_complete`: Refund confirmation completed.
7.  `ERROR/error`: Order is being processed (might have exceptions, requires manual intervention).
8.  `WAIT_KYC`: Waiting for KYC or contact support for a link (when daily limits are exceeded).

## 3. Refund Reason (`refundReason`) Explanations

The `refundReason` field returned when an order is refunded represents the specific reason for the refund as a number.

| `refundReason` | Description             |
| :------------- | :--------------- |
| `1`            | Insufficient liquidity (default). |
| `2`            | Slippage/Error exceeds threshold. |
| `3`            | KYC limit exceeded. |
| `4`            | Address blacklisted. |
| `5`            | Target token under maintenance. |
| `6`            | Swap amount out of bounds. |
| `7`            | Deposit timeout. |
| `8`            | Interaction with risky addresses. |
