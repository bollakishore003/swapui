# swapui.py
# Streamlit Uniswap ETH/USDT dashboard (V2 & V3)

import os
import time
import threading
from collections import deque
from dataclasses import dataclass
from typing import Deque, Tuple, Optional, Dict, List

import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv
from web3 import Web3

# -------------------- Config --------------------
st.set_page_config(page_title="Uniswap ETH/USDT — V2 & V3", layout="wide")
load_dotenv()  # local .env support

# Prefer Streamlit secrets on Cloud, fall back to .env locally
INFURA_WSS = st.secrets.get("INFURA_WSS") or os.getenv("INFURA_WSS")
if not INFURA_WSS:
    st.error("Missing INFURA_WSS. Add it to Streamlit Secrets or your local .env")
    st.stop()

USDT_DECIMALS = 6
WETH_DECIMALS = 18
VWAP_WINDOW = 30           # number of recent swaps to include in VWAP
SPOT_REFRESH_SEC = 2       # how often to refresh spot call
POLL_SLEEP_SEC = 1        # event polling interval

# Mainnet pools: token0=WETH, token1=USDT
UNISWAP_V2_POOL = Web3.to_checksum_address("0x0d4a11d5EEaaC28EC3F61d100daF4d40471f1852")
UNISWAP_V3_POOL = Web3.to_checksum_address("0x11b815efB8f581194ae79006d24E0d814B7697F6")  # 0.05%

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

# -------------------- Data model --------------------
@dataclass
class ExecTrade:
    ts: float
    block: int
    pool: str
    eth_size: float
    price: float      # USDT/ETH
    tx: str

# -------------------- Math helpers --------------------
def vwap(items: Deque[Tuple[float, float]]) -> Optional[float]:
    if not items:
        return None
    num = sum(p * sz for p, sz in items)
    den = sum(sz for _, sz in items)
    return (num / den) if den else None

def v2_spot_price(w3: Web3) -> float:
    pair = w3.eth.contract(address=UNISWAP_V2_POOL, abi=UNIV2_PAIR_ABI_MIN)
    r0, r1, _ = pair.functions.getReserves().call()
    reserve_eth  = r0 / 1e18
    reserve_usdt = r1 / (10 ** USDT_DECIMALS)
    return reserve_usdt / reserve_eth if reserve_eth else float("inf")

def v3_spot_price(w3: Web3) -> float:
    pool = w3.eth.contract(address=UNISWAP_V3_POOL, abi=UNIV3_POOL_ABI_MIN)
    sqrtP, *_ = pool.functions.slot0().call()
    raw = (sqrtP / (2 ** 96)) ** 2              # token1/token0, no decimals
    return raw * (10 ** (WETH_DECIMALS - USDT_DECIMALS))  # multiply by 1e12

def v2_price_from_swap(args) -> Optional[Tuple[float, float]]:
    a0in  = int(args["amount0In"])   # ETH in
    a1out = int(args["amount1Out"])  # USDT out
    if a0in > 0 and a1out > 0:       # ETH -> USDT
        eth = a0in / 1e18
        usdt = a1out / (10 ** USDT_DECIMALS)
        return usdt / eth, eth
    return None

def v3_price_from_swap(args) -> Optional[Tuple[float, float]]:
    a0 = int(args["amount0"])  # ETH, signed
    a1 = int(args["amount1"])  # USDT, signed
    if a0 > 0 and a1 < 0:      # ETH -> USDT
        eth = a0 / 1e18
        usdt = abs(a1) / (10 ** USDT_DECIMALS)
        return usdt / eth, eth
    return None

# -------------------- Engine (background thread) --------------------
class Engine:
    def __init__(self, wss_url: str):
        self.w3 = Web3(Web3.WebsocketProvider(wss_url, websocket_timeout=60))
        if not self.w3.is_connected():
            raise RuntimeError("Web3 not connected. Check INFURA_WSS.")
        latest = self.w3.eth.block_number
        self.from_block = max(0, latest - 50)

        self.v2 = self.w3.eth.contract(address=UNISWAP_V2_POOL, abi=[UNIV2_SWAP_EVENT_ABI])
        self.v3 = self.w3.eth.contract(address=UNISWAP_V3_POOL, abi=[UNIV3_SWAP_EVENT_ABI])

        self.v2_topic = self.w3.keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)").hex()
        self.v3_topic = self.w3.keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()

        self.v2_filter = self.w3.eth.filter({"fromBlock": self.from_block, "address": UNISWAP_V2_POOL, "topics": [self.v2_topic]})
        self.v3_filter = self.w3.eth.filter({"fromBlock": self.from_block, "address": UNISWAP_V3_POOL, "topics": [self.v3_topic]})

        self.lock = threading.Lock()
        self.v2_trades: Deque[Tuple[float, float]] = deque(maxlen=VWAP_WINDOW)   # (price, eth)
        self.v3_trades: Deque[Tuple[float, float]] = deque(maxlen=VWAP_WINDOW)
        self.recent_execs: Deque[ExecTrade] = deque(maxlen=200)
        self.series: Deque[Tuple[float, float, float, float]] = deque(maxlen=600)  # ts, v2spot, v3spot, comb_vwap
        self.v2spot = None
        self.v3spot = None

        self._stop = False
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def stop(self): self._stop = True

    def _run(self):
        last_spot = 0.0
        while not self._stop:
            try:
                # V2 swaps
                for log in self.v2_filter.get_new_entries():
                    ev = self.v2.events.Swap().process_log(log)
                    res = v2_price_from_swap(ev["args"])
                    if res:
                        px, eth_sz = res
                        with self.lock:
                            self.v2_trades.append((px, eth_sz))
                            self.recent_execs.append(ExecTrade(time.time(), log["blockNumber"], "V2", eth_sz, px, ev["transactionHash"].hex()))

                # V3 swaps
                for log in self.v3_filter.get_new_entries():
                    ev = self.v3.events.Swap().process_log(log)
                    res = v3_price_from_swap(ev["args"])
                    if res:
                        px, eth_sz = res
                        with self.lock:
                            self.v3_trades.append((px, eth_sz))
                            self.recent_execs.append(ExecTrade(time.time(), log["blockNumber"], "V3", eth_sz, px, ev["transactionHash"].hex()))

                # Spot refresh
                now = time.time()
                if now - last_spot >= SPOT_REFRESH_SEC:
                    v2s = v2_spot_price(self.w3)
                    v3s = v3_spot_price(self.w3)
                    comb = vwap(deque(list(self.v2_trades) + list(self.v3_trades), maxlen=VWAP_WINDOW))
                    with self.lock:
                        self.v2spot = v2s
                        self.v3spot = v3s
                        self.series.append((now, v2s, v3s, comb if comb else float("nan")))
                    last_spot = now

                time.sleep(POLL_SLEEP_SEC)

            except Exception:
                # Basic reconnect
                time.sleep(5)
                try:
                    self.w3 = Web3(Web3.WebsocketProvider(INFURA_WSS, websocket_timeout=60))
                    self.v2 = self.w3.eth.contract(address=UNISWAP_V2_POOL, abi=[UNIV2_SWAP_EVENT_ABI])
                    self.v3 = self.w3.eth.contract(address=UNISWAP_V3_POOL, abi=[UNIV3_SWAP_EVENT_ABI])
                    self.v2_filter = self.w3.eth.filter({"fromBlock": self.w3.eth.block_number-10, "address": UNISWAP_V2_POOL, "topics": [self.v2_topic]})
                    self.v3_filter = self.w3.eth.filter({"fromBlock": self.w3.eth.block_number-10, "address": UNISWAP_V3_POOL, "topics": [self.v3_topic]})
                except Exception:
                    pass

    def snapshot(self) -> Dict:
        with self.lock:
            v2_v = vwap(self.v2_trades)
            v3_v = vwap(self.v3_trades)
            execs = list(self.recent_execs)[-20:][::-1]  # last 20, newest first
            df = pd.DataFrame(self.series, columns=["ts", "V2 Spot", "V3 Spot", "Combined VWAP"])
        return {"v2_vwap": v2_v, "v3_vwap": v3_v, "v2_spot": self.v2spot, "v3_spot": self.v3spot, "execs": execs, "chart_df": df}

# -------------------- UI --------------------
@st.cache_resource
def get_engine():
    return Engine(INFURA_WSS)

st.title("Uniswap ETH ⇄ USDT — Live (V2 & V3)")
st.caption("Executed ETH→USDT trades (VWAP) and mid spot from pool state (V2 reserves, V3 slot0)")

eng = get_engine()
snap = eng.snapshot()

colA, colB, colC, colD = st.columns(4)
colA.metric("V3 Spot (slot0)", f"{(snap['v3_spot'] or float('nan')):,.2f} USDT/ETH")
colB.metric("V2 Spot (reserves)", f"{(snap['v2_spot'] or float('nan')):,.2f} USDT/ETH")
colC.metric(f"V3 VWAP (last {VWAP_WINDOW})", f"{(snap['v3_vwap'] or float('nan')):,.2f} USDT/ETH")
colD.metric(f"V2 VWAP (last {VWAP_WINDOW})", f"{(snap['v2_vwap'] or float('nan')):,.2f} USDT/ETH")

st.subheader("Spot & Combined VWAP")
df = snap["chart_df"]
if not df.empty:
    df = df.copy()
    df["time"] = pd.to_datetime(df["ts"], unit="s")
    fig = px.line(df, x="time", y=["V2 Spot", "V3 Spot", "Combined VWAP"])
    fig.update_layout(height=340, margin=dict(l=20, r=20, t=10, b=10))
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("Warming up… collecting initial data.")

st.subheader("Recent Executed ETH→USDT Swaps")
execs = snap["execs"]
if execs:
    data = [{
        "When": time.strftime("%H:%M:%S", time.localtime(e.ts)),
        "Pool": e.pool,
        "ETH Size": round(e.eth_size, 6),
        "Price (USDT/ETH)": round(e.price, 2),
        "Block": e.block,
        "Tx": e.tx[:10] + "…"
    } for e in execs]
    st.dataframe(pd.DataFrame(data), use_container_width=True, height=360)
else:
    st.write("No swaps yet in the window…")

# Soft auto-refresh by pinging snapshot periodically
# --- Auto-refresh (simple + reliable) ---
# You can tweak the interval in seconds from the sidebar.
# --- Auto-refresh (simple + reliable) ---
with st.sidebar:
    st.markdown("### Live refresh")
    refresh_enabled = st.toggle("Auto refresh", value=True)
    # Cloud can get unhappy with super-fast loops; 3–10s is a good range
    refresh_secs = st.number_input("Interval (seconds)", min_value=3, max_value=30, value=5, step=1)

st.caption("App auto-updates continuously.")
if refresh_enabled:
    time.sleep(int(refresh_secs))
    st.rerun()   # <-- use this (not st.experimental_rerun)

