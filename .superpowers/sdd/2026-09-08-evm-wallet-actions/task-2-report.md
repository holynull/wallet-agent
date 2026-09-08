# Task 2 Report

Implemented the EVM adapter read/build slice for wallet actions.

What changed:
- Added deterministic ABI helpers in `src/wallet_agent/chains/_common.py` for raw integer validation, address-word encoding, calldata assembly, and receipt success parsing.
- Extended `src/wallet_agent/chains/evm.py` with:
  - `get_token_balance`
  - `get_allowance`
  - `get_transaction_receipt`
  - `build_native_transfer`
  - `build_erc20_transfer`
  - `build_erc20_approve`
- Reused the new balance and receipt helpers inside the existing token balance and transaction status paths.
- Updated `src/wallet_agent/domain/providers.py` so the protocol exposes the new chain methods.
- Added focused tests in `tests/chains/test_evm.py` for selectors, address validation, allowance reads, balance reads, receipt lookup, and native unsigned transfer shape.

Verification:
- `PYTHONPATH=src /Users/zhangleping/github.com/holynull/wallet-agent/.venv/bin/pytest tests/chains/test_evm.py -q`
- `PYTHONPATH=src /Users/zhangleping/github.com/holynull/wallet-agent/.venv/bin/pytest tests/chains -q`
- `/Users/zhangleping/github.com/holynull/wallet-agent/.venv/bin/ruff check src/wallet_agent/chains/_common.py src/wallet_agent/chains/evm.py src/wallet_agent/domain/providers.py tests/chains/test_evm.py`

Commit:
- `1802ec4` — `feat: add EVM allowance and unsigned transaction builders`

Concerns:
- `PYTHONPATH=src` was required in this worktree so pytest resolved the edited source tree instead of the installed package from the main checkout.

Review fix:
- Added token contract address validation for `get_token_balance`, `get_allowance`, `build_erc20_transfer`, and `build_erc20_approve`.
- Added focused regression tests for malformed ERC-20 contract addresses on both read and build paths.
- Re-verified with the same focused EVM and chain test commands plus `ruff check`.
