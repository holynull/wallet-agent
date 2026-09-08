# Query Coin List Interface (queryCoinList) - Parameters and Return Details

**API URL:** `https://api.omnibridge.pro/api/v1/queryCoinList`
**Method:** `POST`
**Content-Type:** `application/json`

## Request Parameters

| Parameter     | Required | Type   | Example         | Description                                                |
| :------------ | :------- | :----- | :-------------- | :--------------------------------------------------------- |
| `supportType` | No       | String | `advanced`      | `advanced`: Only returns tokens that support cross-chain swaps. Otherwise, returns all tokens. |
| `mainNetwork` | No       | String | `ETH`           | Query by the mainnet the token belongs to, e.g., `ETH`, `BSC`. |
| `sourceFlag`  | Yes      | String | `my_app_platform` | Channel name, needs to be coordinated with OmniBridge.       |

## Request Example

```json
{
  "supportType": "advanced",
  "mainNetwork": "ETH",
  "sourceFlag": "my_app_platform"
}
```

## Return Data (`data` field is an array, each element is a token object)

| Field Name          | Type   | Description                                                    | Example Value                                             |
| :------------------ | :----- | :------------------------------------------------------------- | :-------------------------------------------------------- |
| `coinAllCode`       | String | Token full name.                                               | `Bitcoin`                                                 |
| `coinCode`          | String | Token code.                                                    | `BTC`                                                     |
| `coinImageUrl`      | String | Token image URL.                                               | `/static/image/coins/bitcoin.png`                         |
| `coinName`          | String | Token display name.                                            | `Bitcoin`                                                 |
| `coinDecimal`       | String | Token decimal precision.                                       | `8`                                                       |
| `contact`           | String | Contract address.                                              | `0x...`                                                   |
| `isSupportAdvanced` | String | Whether it supports swap (`Y`/`N`).                            | `Y`                                                       |
| `isSupportMemo`     | String | Whether it requires a Memo (`Y`/`N`).                          | `N`                                                       |
| `mainNetwork`       | String | Token's mainnet abbreviation, e.g., `ETH`, `BSC`, `TRON`.      | `ETH`                                                     |
| `noSupportCoin`     | String | Comma-separated list of unsupported swap token codes.          | `BCC,SAN,ICX`                                             |

## Return Notes

*   **Token Name Conflicts:** Some tokens supported by the platform may conflict with the name of tokens supported by the integrating platform, or multiple tokens may share the same name.
*   **Identification:** To prevent sending the wrong token, verify using the `mainNetwork` and `contact` fields.
*   **Supported Mainnets:** Currently, the platform mainly supports tokens on mainnets such as ETH, TRON, BSC, HECO, MATIC, OEC, EOS, XLM (Stellar), XRP, and Waves.