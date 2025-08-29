# pip install web3==6.* python-dotenv
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Any, Optional

from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

INFURA_WSS = os.getenv("INFURA_WSS", "wss://mainnet.infura.io/ws/v3/cf93996e62f944a3aa24b550be5f479e")

# -------------------
# Addresses / Decimals
# -------------------
USDT = Web3.to_checksum_address("0xdAC17F958D2ee523a2206206994597C13D831ec7")
USDT_DECIMALS = 6

# Uniswap V2 WETH/USDT pair (token0=WETH, token1=USDT)
UNISWAP_V2_POOL = Web3.to_checksum_address("0x0d4a11d5EEaaC28EC3F61d100daF4d40471f1852")

# Uniswap V3 WETH/USDT 0.05% pool (token0=WETH, token1=USDT)
UNISWAP_V3_POOL = Web3.to_checksum_address("0x11b815efB8f581194ae79006d24E0d814B7697F6")

# -------------------
# Minimal ABIs
# -------------------
USDT_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "name": "from",  "type": "address"},
            {"indexed": True,  "name": "to",    "type": "address"},
            {"indexed": False, "name": "value", "type": "uint256"},
        ],
        "name": "Transfer",
        "type": "event",
    }
]

UNIV2_SWAP_EVENT_ABI = {
    "anonymous": False,
    "inputs": [
        {"indexed": True,  "name": "sender",    "type": "address"},
        {"indexed": False, "name": "amount0In", "type": "uint256"},
        {"indexed": False, "name": "amount1In", "type": "uint256"},
        {"indexed": False, "name": "amount0Out","type": "uint256"},
        {"indexed": False, "name": "amount1Out","type": "uint256"},
        {"indexed": True,  "name": "to",        "type": "address"},
    ],
    "name": "Swap",
    "type": "event",
}

UNIV3_SWAP_EVENT_ABI = {
    "anonymous": False,
    "inputs": [
        {"indexed": True,  "name": "sender",        "type": "address"},
        {"indexed": True,  "name": "recipient",     "type": "address"},
        {"indexed": False, "name": "amount0",       "type": "int256"},
        {"indexed": False, "name": "amount1",       "type": "int256"},
        {"indexed": False, "name": "sqrtPriceX96",  "type": "uint160"},
        {"indexed": False, "name": "liquidity",     "type": "uint128"},
        {"indexed": False, "name": "tick",          "type": "int24"},
    ],
    "name": "Swap",
    "type": "event",
}

# -------------------
# Dataclasses
# -------------------
@dataclass
class TransferDetails:
    block: int
    tx_hash: str
    sender: str
    recipient: str
    amount: float  # human (6 decimals)

@dataclass
class SwapDetails:
    block: int
    tx_hash: str
    pool: str
    eth: float     # absolute ETH amount traded
    usdt: float    # absolute USDT amount traded
    price: float   # USDT per 1 ETH


# -------------------
# Helpers
# -------------------
def human_usdt(value_wei_like: int) -> float:
    return value_wei_like / (10 ** USDT_DECIMALS)

def safe_avg(values) -> Optional[float]:
    vals = [v for v in values if v and v != float("inf")]
    return sum(vals) / len(vals) if vals else None


# -------------------
# Decoders
# -------------------
def decode_transfer(contract, log) -> TransferDetails:
    ev = contract.events.Transfer().process_log(log)
    amount_raw = int(ev["args"]["value"])
    return TransferDetails(
        block=log["blockNumber"],
        tx_hash=log["transactionHash"].hex(),
        sender=ev["args"]["from"],
        recipient=ev["args"]["to"],
        amount=human_usdt(amount_raw)
    )

def decode_swap_v2(contract, log) -> SwapDetails:
    # token0=WETH, token1=USDT for this pool
    ev = contract.events.Swap().process_log(log)
    eth_delta  = int(ev["args"]["amount0In"]) - int(ev["args"]["amount0Out"])  # +ve => pool received ETH
    usdt_delta = int(ev["args"]["amount1In"]) - int(ev["args"]["amount1Out"])  # +ve => pool received USDT

    eth  = abs(Web3.from_wei(eth_delta, "ether"))
    usdt = abs(human_usdt(usdt_delta))
    price = (usdt / eth) if eth else float("inf")

    return SwapDetails(
        block=log["blockNumber"],
        tx_hash=log["transactionHash"].hex(),
        pool="UniswapV2",
        eth=float(eth),
        usdt=float(usdt),
        price=float(price),
    )

def decode_swap_v3(contract, log) -> SwapDetails:
    # token0=WETH, token1=USDT for this pool
    ev = contract.events.Swap().process_log(log)
    amount0 = int(ev["args"]["amount0"])  # signed
    amount1 = int(ev["args"]["amount1"])  # signed

    eth  = abs(Web3.from_wei(amount0, "ether"))
    usdt = abs(human_usdt(amount1))
    price = (usdt / eth) if eth else float("inf")

    return SwapDetails(
        block=log["blockNumber"],
        tx_hash=log["transactionHash"].hex(),
        pool="UniswapV3 0.05%",
        eth=float(eth),
        usdt=float(usdt),
        price=float(price),
    )


# -------------------
# Main
# -------------------
def main():
    w3 = Web3(Web3.WebsocketProvider(INFURA_WSS, websocket_timeout=60))
    assert w3.is_connected(), "Not connected. Check INFURA_WSS / internet."

    latest_block = w3.eth.block_number
    from_block = max(0, latest_block - 50)
    print(f"Connected. Latest block {latest_block}. Watching from {from_block}…")

    # Contracts
    usdt = w3.eth.contract(address=USDT, abi=USDT_ABI)
    univ2 = w3.eth.contract(address=UNISWAP_V2_POOL, abi=[UNIV2_SWAP_EVENT_ABI])
    univ3 = w3.eth.contract(address=UNISWAP_V3_POOL, abi=[UNIV3_SWAP_EVENT_ABI])

    # Topics
    transfer_topic = w3.keccak(text="Transfer(address,address,uint256)").hex()
    v2_swap_topic  = w3.keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)").hex()
    v3_swap_topic  = w3.keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()

    # Filters
    transfer_filter = w3.eth.filter({"fromBlock": from_block, "address": USDT,             "topics": [transfer_topic]})
    v2_filter       = w3.eth.filter({"fromBlock": from_block, "address": UNISWAP_V2_POOL,  "topics": [v2_swap_topic]})
    v3_filter       = w3.eth.filter({"fromBlock": from_block, "address": UNISWAP_V3_POOL,  "topics": [v3_swap_topic]})

    # Rolling windows
    last5_transfers: Deque[TransferDetails] = deque(maxlen=5)
    last50_transfers: Deque[TransferDetails] = deque(maxlen=50)

    last5_v2_prices: Deque[float] = deque(maxlen=5)
    last5_v3_prices: Deque[float] = deque(maxlen=5)
    last5_all_prices: Deque[float] = deque(maxlen=5)

    while True:
        try:
            # --- USDT Transfers ---
            for log in transfer_filter.get_new_entries():
                td = decode_transfer(usdt, log)
                last50_transfers.append(td)
                last5_transfers.append(td)
                print(f"[Transfer] {td.amount:,.2f} USDT {td.sender} -> {td.recipient} (blk {td.block})")

            if len(last5_transfers):
                total5 = sum(t.amount for t in last5_transfers)
                print(f"  ↳ Last 5 transfer total: {total5:,.2f} USDT")

            # --- Uniswap V2 Swaps ---
            for log in v2_filter.get_new_entries():
                s = decode_swap_v2(univ2, log)
                last5_v2_prices.append(s.price)
                last5_all_prices.append(s.price)
                print(f"[Swap V2] {s.eth:.6f} ETH ⇄ {s.usdt:,.2f} USDT | Price={s.price:,.2f} USDT/ETH (blk {s.block})")

            # --- Uniswap V3 Swaps ---
            for log in v3_filter.get_new_entries():
                s = decode_swap_v3(univ3, log)
                last5_v3_prices.append(s.price)
                last5_all_prices.append(s.price)
                print(f"[Swap V3] {s.eth:.6f} ETH ⇄ {s.usdt:,.2f} USDT | Price={s.price:,.2f} USDT/ETH (blk {s.block})")

            # --- Rolling averages (only print when we have enough points) ---
            v2_avg = safe_avg(last5_v2_prices)
            v3_avg = safe_avg(last5_v3_prices)
            all_avg = safe_avg(last5_all_prices)

            if v2_avg is not None:
                print(f"  ↳ V2 last-5 avg price: {v2_avg:,.2f} USDT/ETH")
            if v3_avg is not None:
                print(f"  ↳ V3 last-5 avg price: {v3_avg:,.2f} USDT/ETH")
            if all_avg is not None:
                print(f"  ↳ Combined last-5 avg price: {all_avg:,.2f} USDT/ETH")

            time.sleep(2)

        except Exception as e:
            # Common causes: provider hiccup, filter ID not found, temporary disconnect
            print(f"[Warn] Poll loop error: {e}. Reconnecting in 5s…")
            time.sleep(5)
            try:
                # Rebuild provider and filters on failure
                w3 = Web3(Web3.WebsocketProvider(INFURA_WSS, websocket_timeout=60))
                usdt = w3.eth.contract(address=USDT, abi=USDT_ABI)
                univ2 = w3.eth.contract(address=UNISWAP_V2_POOL, abi=[UNIV2_SWAP_EVENT_ABI])
                univ3 = w3.eth.contract(address=UNISWAP_V3_POOL, abi=[UNIV3_SWAP_EVENT_ABI])
                latest_block = w3.eth.block_number
                from_block = max(0, latest_block - 50)
                transfer_filter = w3.eth.filter({"fromBlock": from_block, "address": USDT,            "topics": [transfer_topic]})
                v2_filter       = w3.eth.filter({"fromBlock": from_block, "address": UNISWAP_V2_POOL, "topics": [v2_swap_topic]})
                v3_filter       = w3.eth.filter({"fromBlock": from_block, "address": UNISWAP_V3_POOL, "topics": [v3_swap_topic]})
                print("[Info] Reconnected and filters recreated.")
            except Exception as e2:
                print(f"[Error] Reconnect failed: {e2}. Retrying…")
                time.sleep(5)

if __name__ == "__main__":
    main()
