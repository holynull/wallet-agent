# OmniBridge API Endpoints

The following are the main API endpoints for OmniBridge cross-chain swap functionalities:

## Core API Endpoint
- **Base URL:** `https://api.omnibridge.pro`
- **Method:** `POST`
- **Content-Type:** `application/json`

## Swap Order Interfaces

### 1. Create Order (Account Exchange)
- **URL:** `/api/v2/accountExchange`
- **Description:** Used to initiate a new cross-chain swap order.

### 2. Query Order State
- **URL:** `/api/v2/queryOrderState`
- **Description:** Query the detailed processing status of an order using the order number and equipment info.

### 3. Batch Query Order State
- **URL:** `/api/v2/batchQueryOrderState`
- **Description:** (Not fully detailed in docs, but presumed to exist based on directory structure)

### 4. Query Order Record
- **URL:** `/api/v2/queryAllTrade`
- **Description:** (Not fully detailed in docs, but presumed to exist based on directory structure)

## Coin & Exchange Rate Interfaces

### 5. Query Coin List
- **URL:** `/api/v1/queryCoinList`
- **Description:** Retrieve a list of all swappable tokens supported by OmniBridge and their mainnets.

### 6. Query Exchange Rate (Get Base Info)
- **URL:** `/api/v1/getBaseInfo`
- **Description:** Fetch the real-time exchange rate, fees, and min/max swap amounts for a specific token pair.

### 7. Batch Query Exchange Rates (Batch Quote)
- **URL:** `/api/v1/batchQuote`
- **Description:** (Not fully detailed in docs, but presumed to exist based on directory structure)

## Other Swap Interfaces

### 8. Submit Deposit Hash (Modify TxId)
- **URL:** `/api/v2/modifyTxId`
- **Description:** Submit the transaction hash of the deposit to the system after the user completes the transfer.

### 9. Batch Submit Deposit Hash
- **URL:** `/api/v2/batchModifyTxId`
- **Description:** (Not fully detailed in docs, but presumed to exist based on directory structure)

### 10. Query Target Chain GAS Fee (Chain Fee List)
- **URL:** `/api/v1/chainFeeList`
- **Description:** Get the miner fee required to issue specific tokens on the target chain.

### 11. Free Gas Swap
- **URL:** `/api/v2/freeGasExchange`
- **Description:** (Not fully detailed in docs, but presumed to exist based on directory structure)