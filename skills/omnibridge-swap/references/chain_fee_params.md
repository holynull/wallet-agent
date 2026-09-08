# Query Target Chain GAS Fee Interface (chainFeeList) - Parameters and Return Details

**API URL:** `https://api.omnibridge.pro/api/v1/chainFeeList`
**Method:** `POST`
**Content-Type:** `application/json`

## Request Parameters

| Parameter  | Required | Type   | Example    | Description                                                |
| :--------- | :------- | :----- | :--------- | :--------------------------------------------------------- |
| `coinCode` | Yes      | String | `BNB(BSC)` | The token abbreviation used to query the fee required to issue the token on the target chain (e.g., `ETH`, `BNB(BSC)`). |

## Request Example

```json
{
  "coinCode": "BNB(BSC)"
}
```

## Return Data (`data` field is an array of objects)

| Field Name | Type   | Description                            | Example Value |
| :--------- | :----- | :------------------------------------- | :------------ |
| `chainFee` | String | Network fee deducted for token issuance. | `0.001`       |
| `coinCode` | String | Token abbreviation.                    | `BNB(BSC)`    |
```
