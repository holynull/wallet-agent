---
name: omnibridge-swap
description: 🌐 Secure and efficient cross-chain swap functionality via the OmniBridge API. Supports various mainstream blockchain networks and tokens (Native, ERC-20). Enables asset swaps, real-time exchange rate queries, order status tracking, and related on-chain operations. Use this skill to initiate cross-chain swaps or fetch required swap information.
---

# 🌐 OmniBridge Cross-Chain Swap Skill

This skill integrates the OmniBridge API to provide convenient cross-chain asset exchange services.

## ✨ Core Features

1.  **Create Swap Order:** Initiate an asset exchange between different tokens and networks.
2.  **Query Order Status:** Track the real-time progress of your cross-chain swap orders.
3.  **Query Supported Tokens:** Retrieve a list of all supported swap tokens and their respective mainnets on OmniBridge.
4.  **Query Exchange Rates:** Get real-time exchange rates, fees, and min/max swap amounts for specific token pairs.
5.  **Submit Deposit Hash:** Submit the transaction hash to the system after completing your deposit.
6.  **Query Target Chain GAS Fee:** Fetch the network fee required to issue tokens on the target chain.

## 🚀 How to Use

### 1. Create a Swap Order

To initiate a cross-chain swap, you need to provide the following information:

-   **Deposit Token Code** (e.g., `ETH`, `USDT(ERC20)`)
-   **Receive Token Code** (e.g., `BNB(BSC)`, `USDT(TRC20)`)
-   **Deposit Amount** (The amount of tokens you wish to swap)
-   **Receive Address** (The destination address to receive assets on the target chain)
-   **Refund Address** (The address where the original assets will be returned if the swap fails)

**Example:**
`Please create an order to swap 1 ETH from Ethereum to BNB on BSC. The receive address is 0x123...abc, and the refund address is 0x456...def.`

*   **Auto-Transfer (New Feature):**
    We added a private key configuration item in `assets/.env`. If the token deposited by the user belongs to an **EVM-compatible chain** (e.g., ARB, BSC, ETH, POLYGON, Optimism, etc.) and the `EVM_PRIVATE_KEY` is configured, after the order is successfully created, you can ask the user if they want an **auto-transfer**.
    If the user agrees, use the `scripts/evm_transfer.js` script to automatically transfer funds to the deposit address (`platformAddr`) specified in the order (supports native tokens and ERC-20 tokens). Upon successful transfer, automatically call `submit_deposit_hash.js` to submit the Hash.
    If the user is on a **non-EVM chain**, provide the deposit address for the user to transfer manually; the auto-transfer feature will not be used.
    If the user **has not configured a private key but is on an EVM chain**, you can ask the user if they want to provide a private key to enable the **auto-transfer** mode.

### 2. Query Order Status

You can use the `orderId` or `equipmentNo` to query the latest status of an order.

**Example:**
`Query the order status for order ID d47e8b9b-...eb9a, equipment number my_device_001, source H5.`

### 3. Query Supported Tokens

Retrieve a list of all tokens supported by OmniBridge.

**Example:**
`Query all supported swap tokens.`
`Query supported ERC-20 tokens.`
`Query supported tokens on the Ethereum mainnet.`

### 4. Query Exchange Rates

Before creating an order, you can query real-time exchange rates, fees, and other information between two tokens.

**Example:**
`Query the exchange rate for swapping 1 ETH to BNB(BSC).`

### 5. Submit Deposit Hash

After you have successfully deposited assets to the address provided by OmniBridge, you need to submit the transaction hash.

**Example:**
`Submit the deposit hash 0xabcdef123... for order d47e8b9b-...eb9a.`

### 6. Query Target Chain GAS Fee

Query the network transaction fee required to issue specific tokens on the target chain.

**Example:**
`Query the on-chain GAS fee for BNB(BSC).`

## ⚠️ Security & Precautions

*   **Address Confirmation:** Before submitting an order, please carefully double-check the **Receive Address** and **Refund Address**. Incorrect addresses may result in permanent loss of assets.
*   **Private Key Security:** This skill will not ask you for your private key (unless explicitly configuring the `.env` for auto-transfers). All manual on-chain deposits must be completed by you.
*   **Slippage:** Understand how slippage affects the final received amount.
*   **KYC:** Some transactions may trigger a KYC process; please keep an eye on your order status.
*   **Transaction Confirmation:** Cross-chain transactions take time. Please be patient and check your order status when necessary.

## 📚 References

The following files provide detailed API parameters, status codes, and refund reasons. They will be loaded into context when needed.

*   **`references/api_endpoints.md`**: A list of all API URLs for OmniBridge.
*   **`references/create_order_params.md`**: Detailed request parameters for the create order API.
*   **`references/order_state_details.md`**: Detailed explanations and query parameters for order statuses.
*   **`references/coin_list_details.md`**: Token lists and their detailed attributes.
*   **`references/exchange_rate_details.md`**: Detailed parameters and return fields for the exchange rate query API.
*   **`references/submit_hash_params.md`**: Request parameters for submitting the deposit hash.
*   **`references/chain_fee_params.md`**: Query parameters for on-chain GAS fees.
*   **`references/response_codes.md`**: Detailed explanations of API status codes and refund reasons.

## ⚙️ Scripts

The `scripts/` directory will contain Python/JS scripts interacting with the OmniBridge API, handling HTTP requests and JSON data processing. These scripts will call the appropriate API endpoints based on user requests.
