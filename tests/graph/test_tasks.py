from wallet_agent.graph.tasks import (
    hydrate_active_task,
    merge_task_patch,
    new_active_task,
    project_legacy_draft,
)


def test_hydrates_legacy_swap_draft_without_changing_values():
    state = {
        "conversation_id": "conversation-1",
        "swap_draft": {
            "source_chain": "BASE",
            "destination_chain": "BASE",
            "source_symbol": "USDC",
            "destination_symbol": "USDT",
            "input_amount": "1",
        },
    }

    first = hydrate_active_task(state)
    second = hydrate_active_task(state)

    assert first == second
    assert first["kind"] == "swap"
    assert first["revision"] == 1
    assert first["slots"] == state["swap_draft"]
    assert set(first["slot_sources"].values()) == {"legacy"}


def test_transfer_patch_increments_revision_once_for_multiple_changes():
    task = new_active_task("transfer", task_id="transfer-1")

    result = merge_task_patch(
        task,
        {"chain": "BASE", "symbol": "ETH", "amount": "0.01"},
    )

    assert result.task["revision"] == 1
    assert result.changed_slots == frozenset({"chain", "symbol", "amount"})
    assert result.task["slots"]["amount"] == "0.01"
    assert result.task["slot_sources"] == {
        "chain": "user",
        "symbol": "user",
        "amount": "user",
    }


def test_identical_patch_does_not_increment_revision():
    task = merge_task_patch(
        new_active_task("transfer", task_id="transfer-1"),
        {"chain": "BASE", "amount": "1"},
    ).task

    result = merge_task_patch(task, {"chain": "BASE", "amount": "1"})

    assert result.task["revision"] == 1
    assert result.changed_slots == frozenset()
    assert result.invalidation == {}


def test_task_patch_normalizes_chain_and_symbol_noise():
    task = new_active_task("swap", task_id="swap-1")

    result = merge_task_patch(
        task,
        {
            "source_chain": "Ethereum",
            "destination_chain": " ERC20 ",
            "source_symbol": "usdc",
            "destination_symbol": "usd't",
        },
    )

    assert result.task["slots"] == {
        "source_chain": "ETH",
        "destination_chain": "ETH",
        "source_symbol": "USDC",
        "destination_symbol": "USDT",
    }


def test_normalized_equivalent_patch_does_not_increment_revision():
    task = merge_task_patch(
        new_active_task("transfer", task_id="transfer-1"),
        {"chain": "BASE", "symbol": "USDC"},
    ).task

    result = merge_task_patch(task, {"chain": "Base", "symbol": "usd-c"})

    assert result.task["revision"] == 1
    assert result.changed_slots == frozenset()


def test_task_patch_normalizes_amount_with_token_unit():
    task = new_active_task("swap", task_id="swap-1")

    result = merge_task_patch(task, {"input_amount": "1 USDC"})

    assert result.task["slots"]["input_amount"] == "1"


def test_swap_chain_correction_clears_resolved_asset_metadata():
    task = new_active_task("swap", task_id="swap-1")
    task["revision"] = 2
    task["slots"] = {
        "source_chain": "ETHEREUM",
        "source_symbol": "USDC",
        "source_token_address": "0x" + "3" * 40,
        "source_decimals": 6,
        "destination_chain": "BASE",
        "destination_symbol": "USDT",
        "destination_token_address": "0x" + "4" * 40,
        "destination_decimals": 6,
        "input_amount": "1",
        "input_amount_raw": "1000000",
    }

    result = merge_task_patch(task, {"source_chain": "BASE"})

    assert result.task["revision"] == 3
    assert "source_token_address" not in result.task["slots"]
    assert "source_decimals" not in result.task["slots"]
    assert result.task["slots"]["destination_token_address"] == "0x" + "4" * 40
    assert result.invalidation["selected_quote"] is None
    assert result.invalidation["quote_candidates"] == [{"__clear__": True}]


def test_swap_amount_correction_invalidates_transaction_artifacts():
    task = new_active_task("swap", task_id="swap-1")
    task["revision"] = 1
    task["slots"] = {"input_amount": "1", "input_amount_raw": "1000000"}

    result = merge_task_patch(task, {"input_amount": "2"})

    assert result.task["slots"] == {"input_amount": "2"}
    assert result.invalidation == {
        "selected_quote": None,
        "quote_candidates": [{"__clear__": True}],
        "confirmation_state": None,
        "swap_gas_estimate": None,
        "pending_transaction": None,
        "preflight": None,
        "swap_request": None,
        "approval_transaction": None,
        "allowance_requirement": None,
    }


def test_projection_keeps_legacy_transfer_field_names():
    task = new_active_task("transfer", task_id="transfer-1")
    task["slots"] = {
        "chain": "BASE",
        "symbol": "USDC",
        "token_address": "0x" + "3" * 40,
        "decimals": 6,
        "amount": "4",
        "recipient": "0x" + "2" * 40,
    }

    assert project_legacy_draft(task) == {
        "transfer_chain": "BASE",
        "transfer_symbol": "USDC",
        "transfer_token_address": "0x" + "3" * 40,
        "transfer_decimals": 6,
        "transfer_amount": "4",
        "transfer_recipient": "0x" + "2" * 40,
    }
