# eth_usdt_v2_v3_prices.py
# pip install web3==6.* python-dotenv
import os, time, math
from collections import deque
from typing import Deque, Tuple, Optional
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()
INFURA_WSS = os.getenv("INFURA_WSS", "wss://mainnet.infura.io/ws/v3/cf93996e62f944a3aa24b550be5f479e")

# ------------------- constants (Ethereum mainnet) -------------------
USDT_DECIMALS = 6
WETH_DECIMALS = 18

# Uniswap V2: WETH/USDT (token0=WETH, token1=USDT)
UNISWAP_V2_POOL = Web3.to_checksum_address("0x0d4a11d5EEaaC28EC3F61d100daF4d40471f1852")

# Uniswap V3 0.05%: WETH/USDT (token0=WETH, token1=USDT)
UNISWAP_V3_POOL = Web3.to_checksum_address("0x11b815efB8f581194ae79006d24E0d814B7697F6")

VWAP_WINDOW = 20              # last N swaps for VWAP
SPOT_INTERVAL_SEC = 10        # print spot this often
DEVIATION_WARN_PCT = 1.0      # warn if |VWAP-Spot| > this %

# ------------------- ABIs (minimal) -------------------
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
UNIV2_PAIR_ABI_MIN = [
    {"name": "getReserves", "outputs": [
        {"type": "uint112", "name": "_reserve0"},
        {"type": "uint112", "name": "_reserve1"},
        {"type": "uint32",  "name": "_blockTimestampLast"}
    ], "stateMutability": "view", "type": "function", "inputs": []}
]
UNIV3_POOL_ABI_MIN = [
    {"name":"slot0","outputs":[
        {"type":"uint160","name":"sqrtPriceX96"},
        {"type":"int24","name":"tick"},
        {"type":"uint16","name":"observationIndex"},
        {"type":"uint16","name":"observationCardinality"},
        {"type":"uint16","name":"observationCardinalityNext"},
        {"type":"uint8","name":"feeProtocol"},
        {"type":"bool","name":"unlocked"}
    ],"stateMutability":"view","type":"function","inputs":[]}
]

# ------------------- helpers -------------------
def vwap(deq: Deque[Tuple[float, float]]) -> Optional[float]:
    if not deq: return None
    num = sum(p * sz for p, sz in deq); den = sum(sz for _, sz in deq)
    return (num / den) if den else None

def pct_diff(a: float, b: float) -> Optional[float]:
    if a is None or b is None or b == 0: return None
    return 100.0 * (a - b) / b

# --- executed price decoders (ETH→USDT only) ---
def v2_price_from_swap(ev_args) -> Optional[Tuple[float, float]]:
    a0in  = int(ev_args["amount0In"])     # ETH in
    a1out = int(ev_args["amount1Out"])    # USDT out
    if a0in > 0 and a1out > 0:
        eth_in = a0in / 1e18
        usdt_out = a1out / (10 ** USDT_DECIMALS)
        px = usdt_out / eth_in if eth_in else float("inf")
        return px, eth_in
    return None

def v3_price_from_swap(ev_args) -> Optional[Tuple[float, float]]:
    a0 = int(ev_args["amount0"])  # ETH (token0), signed
    a1 = int(ev_args["amount1"])  # USDT (token1), signed
    # ETH->USDT: pool gets ETH (+), sends USDT (-)
    if a0 > 0 and a1 < 0:
        eth_in = a0 / 1e18
        usdt_out = abs(a1) / (10 ** USDT_DECIMALS)
        px = usdt_out / eth_in if eth_in else float("inf")
        return px, eth_in
    return None

# --- spot price (mid) ---
def v2_spot_price(w3: Web3) -> float:
    pair = w3.eth.contract(address=UNISWAP_V2_POOL, abi=UNIV2_PAIR_ABI_MIN)
    r0, r1, _ = pair.functions.getReserves().call()
    reserve_eth  = r0 / 1e18
    reserve_usdt = r1 / (10 ** USDT_DECIMALS)
    return reserve_usdt / reserve_eth if reserve_eth else float("inf")

def v3_spot_price(w3: Web3) -> float:
    pool = w3.eth.contract(address=UNISWAP_V3_POOL, abi=UNIV3_POOL_ABI_MIN)
    sqrtP, *_ = pool.functions.slot0().call()
    # raw price token1/token0 with no decimals
    raw = (sqrtP / (2 ** 96)) ** 2
    # apply decimals: token0=WETH(18), token1=USDT(6) → multiply by 10^(18-6)
    return raw * (10 ** (WETH_DECIMALS - USDT_DECIMALS))

# ------------------- main -------------------
def main():
    w3 = Web3(Web3.WebsocketProvider(INFURA_WSS, websocket_timeout=60))
    assert w3.is_connected(), "Web3 not connected. Check INFURA_WSS."

    latest = w3.eth.block_number
    from_block = max(0, latest - 50)
    print(f"Connected. Latest block {latest}. Starting at {from_block}…")

    # contracts
    v2 = w3.eth.contract(address=UNISWAP_V2_POOL, abi=[UNIV2_SWAP_EVENT_ABI])
    v3 = w3.eth.contract(address=UNISWAP_V3_POOL, abi=[UNIV3_SWAP_EVENT_ABI])

    # topics + filters
    v2_topic = w3.keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)").hex()
    v3_topic = w3.keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()
    v2_f = w3.eth.filter({"fromBlock": from_block, "address": UNISWAP_V2_POOL, "topics": [v2_topic]})
    v3_f = w3.eth.filter({"fromBlock": from_block, "address": UNISWAP_V3_POOL, "topics": [v3_topic]})

    # vwap windows
    v2_win: Deque[Tuple[float, float]] = deque(maxlen=VWAP_WINDOW)
    v3_win: Deque[Tuple[float, float]] = deque(maxlen=VWAP_WINDOW)

    last_spot_t = 0.0

    while True:
        try:
            # V2 executed swaps (ETH->USDT only)
            for log in v2_f.get_new_entries():
                ev = v2.events.Swap().process_log(log)
                res = v2_price_from_swap(ev["args"])
                if not res: continue
                px, eth_sz = res
                v2_win.append((px, eth_sz))
                print(f"[V2] {eth_sz:.6f} ETH → @ {px:,.2f} USDT/ETH  (blk {log['blockNumber']})")

            # V3 executed swaps (ETH->USDT only)
            for log in v3_f.get_new_entries():
                ev = v3.events.Swap().process_log(log)
                res = v3_price_from_swap(ev["args"])
                if not res: continue
                px, eth_sz = res
                v3_win.append((px, eth_sz))
                print(f"[V3] {eth_sz:.6f} ETH → @ {px:,.2f} USDT/ETH  (blk {log['blockNumber']})")

            # Rolling VWAPs
            v2_v = vwap(v2_win)
            v3_v = vwap(v3_win)
            if v2_v is not None:
                print(f"  ↳ V2 VWAP (last {len(v2_win)}): {v2_v:,.2f} USDT/ETH")
            if v3_v is not None:
                print(f"  ↳ V3 VWAP (last {len(v3_win)}): {v3_v:,.2f} USDT/ETH")

            # Periodic spot (and compare)
            now = time.time()
            if now - last_spot_t >= SPOT_INTERVAL_SEC:
                try:
                    spot_v3 = v3_spot_price(w3)
                    print(f"  ↳ V3 Spot (slot0): {spot_v3:,.2f} USDT/ETH")
                    if v3_v is not None:
                        d = pct_diff(v3_v, spot_v3)
                        if d is not None and abs(d) > DEVIATION_WARN_PCT:
                            print(f"    ⚠ VWAP vs Spot dev: {d:+.2f}%")
                except Exception as e:
                    print(f"  [spot] V3 failed: {e}")

                try:
                    spot_v2 = v2_spot_price(w3)
                    print(f"  ↳ V2 Spot (reserves): {spot_v2:,.2f} USDT/ETH")
                    if v2_v is not None:
                        d = pct_diff(v2_v, spot_v2)
                        if d is not None and abs(d) > DEVIATION_WARN_PCT:
                            print(f"    ⚠ VWAP vs Spot dev: {d:+.2f}%")
                except Exception as e:
                    print(f"  [spot] V2 failed: {e}")

                last_spot_t = now

            time.sleep(2)

        except Exception as e:
            print(f"[Warn] loop error: {e}. Reconnecting in 5s…")
            time.sleep(5)
            w3 = Web3(Web3.WebsocketProvider(INFURA_WSS, websocket_timeout=60))
            v2 = w3.eth.contract(address=UNISWAP_V2_POOL, abi=[UNIV2_SWAP_EVENT_ABI])
            v3 = w3.eth.contract(address=UNISWAP_V3_POOL, abi=[UNIV3_SWAP_EVENT_ABI])
            v2_f = w3.eth.filter({"fromBlock": w3.eth.block_number-10, "address": UNISWAP_V2_POOL, "topics": [v2_topic]})
            v3_f = w3.eth.filter({"fromBlock": w3.eth.block_number-10, "address": UNISWAP_V3_POOL, "topics": [v3_topic]})

if __name__ == "__main__":
    main()
