# SwapUI – Uniswap ETH/USDT Dashboard

A Streamlit app to monitor ETH/USDT swaps and spot prices on Uniswap V2 & V3 in real time.

## Features
- Live VWAP (Volume Weighted Average Price) from recent swaps
- Spot prices from Uniswap V2 reserves & V3 `slot0`
- Comparison with Binance ETH/USDT
- Interactive charts (Plotly)

## Run Locally
```bash
git clone https://github.com/bollakishore003/swapui.git
cd swapui
python -m venv .venv
source .venv/bin/activate   # (on Windows: .venv\Scripts\activate)
pip install -r requirements.txt
streamlit run uniswap_eth_usdt_dashboard.py
