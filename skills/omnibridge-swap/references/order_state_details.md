# Query Order Status Interface (queryOrderState) - Parameters and Return Details

**API URL:** `https://api.omnibridge.pro/api/v2/queryOrderState`
**Method:** `POST`
**Content-Type:** `application/json`

## Request Parameters

| Parameter     | Required | Type   | Example                                  | Description                                  |
| :------------ | :------- | :----- | :--------------------------------------- | :------------------------------------------- |
| `equipmentNo` | Yes      | String | `SFjeigreEIFegjieFei`                    | Unique device number.                        |
| `sourceType`  | Yes      | String | `H5`                                     | Source type (`ANDROID`, `IOS`, `H5`).        |
| `orderId`     | Yes      | String | `9d4a577d-fdb1-466c-8da2-a5ad3553260b`   | Swap order ID.                               |

## Request Example

```json
{
  "equipmentNo": "my_device_001",
  "sourceType": "H5",
  "orderId": "d47e8b9b-c17f-432b-9285-a46c0a3ceb9a"
}
```

## Return Data (Inside the `data` field)

The return data contains numerous fields similar to the create order interface. Key fields and status explanations are listed below:

| Field Name           | Type   | Description                                                                                           | Example Value                                                |
| :------------------- | :----- | :---------------------------------------------------------------------------------------------------- | :----------------------------------------------------------- |
| `orderId`            | String | Order ID.                                                                                             | `d47e8b9b-c17f-432b-9285-a46c0a3ceb9a`                       |
| `depositCoinCode`    | String | Deposit token code.                                                                                   | `ETH`                                                        |
| `receiveCoinCode`    | String | Receive token code.                                                                                   | `BTC`                                                        |
| `depositCoinAmt`     | String | Deposit amount.                                                                                       | `1`                                                          |
| `receiveCoinAmt`     | String | Expected receive amount.                                                                              | `0.1`                                                        |
| `platformAddr`       | String | OmniBridge provided deposit address (user transfers funds here).                                      | `0x3181af4f7cc7251a6a4eda75526c8abe10106db8`                 |
| `destinationAddr`    | String | Address to receive the swapped assets.                                                                | `0xAE93FA34f728855cE663cf9FcF8e32148F079071`                 |
| `refundAddr`         | String | Address to refund original assets if the swap fails.                                                  | `0xAE93FA34f728855cE663cf9FcF8e32148F079071`                 |
| `depositCoinFeeRate` | String | Deposit fee rate.                                                                                     | `0.002`                                                      |
| `depositCoinFeeAmt`  | String | Deposit fee amount.                                                                                   | `0.004`                                                      |
| `refundCoinAmt`      | String | Refund amount (if swap fails).                                                                        | `0.98`                                                       |
| `transactionId`      | String | Transaction ID for token issuance (available only after successful swap and issuance).                | `0x...`                                                      |
| `refundDepositTxid`  | String | Refund transaction ID (available only after refund).                                                  | `0x...`                                                      |
| `detailState`        | String | **Detailed Order Status (see explanations below).**                                                   | `wait_deposit_send`                                          |
| `kycUrl`             | String | KYC URL (provided if daily limit is exceeded).                                                        | `https://kyc.omnibridge.pro/...`                             |
| `dealReceiveCoinAmt` | String | Actual amount of swapped tokens received (empty if incomplete).                                       | `13.713109`                                                  |
| `completeTime`       | String | Time when token issuance or refund is completed (UTC+8).                                              | `2022-03-10 18:44:21`                                        |
| `burnRate`           | String | Burn rate (default 0).                                                                                | `0`                                                          |
| `createTime`         | String | Order creation time.                                                                                  | `2022-03-10 18:44:21`                                        |
| `depositCoinState`   | String | Deposit state (`wait_send`, `wait_confirm`, `already_confirm`).                                       | `wait_send`                                                  |
| `chainFee`           | String | Network fee deducted upon token issuance.                                                             | `0.001`                                                      |
| `refundReason`       | String | Refund reason (returns a number, see explanations below).                                             | `1`                                                          |

## Detailed Order Status (`detailState`) Explanations

1.  `wait_deposit_send`: Waiting for deposit to be sent.
2.  `timeout`: Order timed out.
3.  `wait_exchange_push`: Waiting for exchange info push.
4.  `wait_exchange_return`: Waiting for exchange info return.
5.  **Receive Token Flow:**
    *   `wait_receive_send`: Waiting for receive token to be sent.
    *   `wait_receive_confirm`: Waiting for receive token confirmation.
    *   `receive_complete`: Receive token confirmation completed.
6.  **Refund Flow:**
    *   `wait_refund_send`: Waiting for refund to be sent.
    *   `wait_refund_confirm`: Waiting for refund confirmation.
    *   `refund_complete`: Refund confirmation completed.
7.  `ERROR/error`: Order is being processed (with errors/delays).
8.  `WAIT_KYC`: Waiting for KYC or contact support for a link.

## Refund Reason (`refundReason`) Explanations

1.  `1`: Insufficient liquidity (default).
2.  `2`: Slippage/Error exceeds threshold.
3.  `3`: KYC limit exceeded.
4.  `4`: Address blacklisted.
5.  `5`: Target token under maintenance.
6.  `6`: Swap amount out of bounds.
7.  `7`: Deposit timeout.
8.  `8`: Interaction with risky addresses.