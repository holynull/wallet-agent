# Create Order Interface (accountExchange) - Request Parameter Details

**API URL:** `https://api.omnibridge.pro/api/v2/accountExchange`
**Method:** `POST`
**Content-Type:** `application/json`

## Request Parameters

| Parameter         | Required | Type   | Example                                  | Description                                                 |
| :---------------- | :------- | :----- | :--------------------------------------- | :---------------------------------------------------------- |
| `depositCoinCode` | Yes      | String | `ETH`                                    | Deposit token code, e.g., `ETH`, `USDT(ERC20)`.               |
| `receiveCoinCode` | Yes      | String | `BTC`                                    | Receive token code, e.g., `BTC`, `BNB(BSC)`.                 |
| `depositCoinAmt`  | Yes      | String | `0.01`                                   | The amount of tokens the user wishes to deposit.              |
| `receiveCoinAmt`  | No       | String | `0.1` (Expected amount)                  | Expected receive amount. If not provided, script defaults to 0. |
| `destinationAddr` | Yes      | String | `18orDLFMp3fGoy5Uk93LDGTGbxWEm7b7FY`     | The address to receive the swapped assets on the target chain.<br>If there is a `memo`, append it to the address using `#`, e.g., `address#memo`. |
| `refundAddr`      | Yes      | String | `18orDLFMp3fGoy5Uk93LDGTGbxWEm7b7FY`     | The address to return original assets if the swap fails.<br>If there is a `memo`, append it to the address using `#`, e.g., `address#memo`. |
| `equipmentNo`     | Yes      | String | `zfgryh918f93a19fdg6918a68cf5`            | Unique device number (serial number). If unavailable, use the refund address. Used to query order status and records; must match the value used during order creation. |
| `sourceType`      | Yes      | String | `H5`                                     | Source type, must be `ANDROID`, `IOS`, or `H5`.               |
| `sourceFlag`      | Yes      | String | `widget`                                 | Identifier for the platform creating the order. Values are coordinated with OmniBridge. |
| `slippage`        | No       | String | `0.02` (represents 2%)                   | Slippage setting as a decimal (system default is `0.05` i.e. 5%).<br>E.g., `0.01` = 1%. |

## Request Example

```json
{
  "depositCoinCode": "ETH",
  "receiveCoinCode": "BNB(BSC)",
  "depositCoinAmt": "1.5",
  "receiveCoinAmt": "10.0",
  "destinationAddr": "0xAE93FA34f728855cE663cf9FcF8e32148F079071",
  "refundAddr": "0xAE93FA34f728855cE663cf9FcF8e32148F079071",
  "equipmentNo": "my_device_001",
  "sourceType": "H5",
  "sourceFlag": "my_app_platform",
  "slippage": "0.01"
}
```
