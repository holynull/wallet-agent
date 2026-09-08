# Submit Deposit Hash Interface (modifyTxId) - Request Parameter Details

**API URL:** `https://api.omnibridge.pro/api/v2/modifyTxId`
**Method:** `POST`
**Content-Type:** `application/json`

## Request Parameters

| Parameter     | Required | Type   | Example                                  | Description                  |
| :------------ | :------- | :----- | :--------------------------------------- | :--------------------------- |
| `orderId`     | Yes      | String | `33120af8-1866-4cb6-99a8-2c303f490c2c`   | Swap order ID.               |
| `depositTxid` | Yes      | String | `0x123abc...def`                         | Transaction hash of the user's deposit. |

## Request Example

```json
{
  "orderId": "d47e8b9b-c17f-432b-9285-a46c0a3ceb9a",
  "depositTxid": "0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
}
```
