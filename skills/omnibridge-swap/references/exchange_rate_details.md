# Query Exchange Rate Interface (getBaseInfo) - Parameters and Return Details

**API URL:** `https://api.omnibridge.pro/api/v1/getBaseInfo`
**Method:** `POST`
**Content-Type:** `application/json`

## Request Parameters

| Parameter         | Required | Type   | Example           | Description                               |
| :---------------- | :------- | :----- | :---------------- | :---------------------------------------- |
| `depositCoinCode` | Yes      | String | `BTC`             | Deposit token code.                       |
| `receiveCoinCode` | Yes      | String | `ETH`             | Receive token code.                       |
| `depositCoinAmt`  | Yes      | String | `1.5`             | Amount of tokens the user wishes to deposit. |
| `sourceFlag`      | Yes      | String | `my_app_platform` | Channel name, coordinated with OmniBridge.  |

## Request Example

```json
{
  "depositCoinCode": "ETH",
  "receiveCoinCode": "BNB(BSC)",
  "depositCoinAmt": "1.5",
  "sourceFlag": "my_app_platform"
}
```

## Return Data (Inside the `data` field)

| Field Name           | Type    | Description                                                    | Example Value          |
| :------------------- | :------ | :------------------------------------------------------------- | :--------------------- |
| `chainFee`           | String  | Network fee deducted upon token issuance (in receive token).   | `0.001`                |
| `depositCoinFeeRate` | String  | Swap fee rate.<br>Swap fee = `Deposit Amount * depositCoinFeeRate`. | `0.002`                |
| `depositMax`         | String  | Maximum deposit limit.                                         | `14`                   |
| `depositMin`         | String  | Minimum deposit limit.                                         | `0.038603`             |
| `instantRate`        | String  | Current exchange rate (up to 10 decimal places, `Receive / Deposit`). | `6.875775974236`       |
| `burnRate`           | String  | Burn rate (default `0`).                                       | `0`                    |
| `isSupportNoGas`     | Boolean | Whether gas-free swap is supported (`true`/`false`).           | `true`                 |
| `isSupport`          | Boolean | Whether the token pair is supported for swap (`true`/`false`). | `true`                 |
| `difference`         | String  | Swap difference (returned as a decimal).                       | `0.1`                  |

## Special Field Explanations

*   **`chainFee`**: Used for decentralized swaps. Represents the network fee deducted when issuing tokens after a successful swap (in the receive token). Can be used to estimate the user's final received amount or display the network fee that will be deducted.
*   **`depositCoinFeeRate`**: The swap fee rate. Swap fee = `depositCoinAmt * depositCoinFeeRate`.

## Notes

*   **Decentralized Swap Fee:** For decentralized swaps, the fee is deducted from the original deposit token. The fixed fee rate is 0.3% of the deposited token (e.g., depositing 0.1 BTC incurs a 0.0003 BTC fee, and only 0.0997 BTC is actually swapped).

## Calculating Actual Received Amount

`Actual Received Amount = (Deposit Amount - Swap Fee Amount) * Exchange Rate - Chain Fee`
`= (depositCoinAmt - depositCoinAmt * depositCoinFeeRate) * instantRate - chainFee`
