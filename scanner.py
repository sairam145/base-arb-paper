"""
Read-only arbitrage opportunity logger for WETH/USDC on Base.

For each check it:
  1. Quotes selling WETH -> USDC on every venue (real quotes: fees + slippage included)
  2. Quotes buying back USDC -> WETH on every *other* venue
  3. Subtracts estimated gas, logs net profit per (size, sell venue, buy venue) to CSV

No wallet, no private key, no transactions. Pure eth_call against the latest block.
All quotes for one check are pinned to the same block and batched through Multicall3
(2 RPC calls per check) to stay inside free-tier node limits.
"""

import csv
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

# ---------- config (override in .env) ----------
RPC_HTTP_URL = os.environ["RPC_HTTP_URL"]
TRADE_SIZES_ETH = [float(x) for x in os.getenv("TRADE_SIZES_ETH", "0.5,2,10").split(",")]
CHECK_INTERVAL_SEC = float(os.getenv("CHECK_INTERVAL_SEC", "10"))
GAS_UNITS = int(os.getenv("GAS_UNITS", "350000"))          # flash loan + 2 swaps, rough
PRIORITY_FEE_GWEI = float(os.getenv("PRIORITY_FEE_GWEI", "0.01"))
L1_FEE_ETH = float(os.getenv("L1_FEE_ETH", "0.000002"))     # Base L1 data fee, rough
FLASH_FEE_BPS = float(os.getenv("FLASH_FEE_BPS", "0"))      # Balancer = 0, Aave = 5
CSV_PATH = os.getenv("CSV_PATH", "opportunities.csv")

# ---------- Base mainnet addresses ----------
cs = Web3.to_checksum_address
WETH = cs("0x4200000000000000000000000000000000000006")
USDC = cs("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
UNI_FACTORY = cs("0x33128a8fC17869897dcE68Ed026d694621f6FDfD")
UNI_QUOTER_V2 = cs("0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a")
AERO_FACTORY = cs("0x420DD381b31aEf6683db6B902084cB0FFECe40Da")
MULTICALL3 = cs("0xcA11bde05977b3631167028862bE2a173976CA11")

# ---------- minimal ABIs ----------
UNI_FACTORY_ABI = [{
    "name": "getPool", "type": "function", "stateMutability": "view",
    "inputs": [{"name": "a", "type": "address"}, {"name": "b", "type": "address"},
               {"name": "fee", "type": "uint24"}],
    "outputs": [{"name": "", "type": "address"}],
}]
QUOTER_ABI = [{
    "name": "quoteExactInputSingle", "type": "function", "stateMutability": "nonpayable",
    "inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
        {"name": "amountIn", "type": "uint256"}, {"name": "fee", "type": "uint24"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"}]}],
    "outputs": [{"name": "amountOut", "type": "uint256"},
                {"name": "sqrtPriceX96After", "type": "uint160"},
                {"name": "initializedTicksCrossed", "type": "uint32"},
                {"name": "gasEstimate", "type": "uint256"}],
}]
AERO_FACTORY_ABI = [{
    "name": "getPool", "type": "function", "stateMutability": "view",
    "inputs": [{"name": "a", "type": "address"}, {"name": "b", "type": "address"},
               {"name": "stable", "type": "bool"}],
    "outputs": [{"name": "", "type": "address"}],
}]
AERO_POOL_ABI = [{
    "name": "getAmountOut", "type": "function", "stateMutability": "view",
    "inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "tokenIn", "type": "address"}],
    "outputs": [{"name": "", "type": "uint256"}],
}]
MULTICALL3_ABI = [{
    "name": "aggregate3", "type": "function", "stateMutability": "payable",
    "inputs": [{"name": "calls", "type": "tuple[]", "components": [
        {"name": "target", "type": "address"}, {"name": "allowFailure", "type": "bool"},
        {"name": "callData", "type": "bytes"}]}],
    "outputs": [{"name": "returnData", "type": "tuple[]", "components": [
        {"name": "success", "type": "bool"}, {"name": "returnData", "type": "bytes"}]}],
}]

w3 = Web3(Web3.HTTPProvider(RPC_HTTP_URL, request_kwargs={"timeout": 20}))
quoter = w3.eth.contract(address=UNI_QUOTER_V2, abi=QUOTER_ABI)
multicall = w3.eth.contract(address=MULTICALL3, abi=MULTICALL3_ABI)
ZERO = "0x0000000000000000000000000000000000000000"


def discover_venues():
    """Find live WETH/USDC pools. Skips any that don't exist."""
    venues = []
    uni_f = w3.eth.contract(address=UNI_FACTORY, abi=UNI_FACTORY_ABI)
    for fee in (500, 3000):
        pool = uni_f.functions.getPool(WETH, USDC, fee).call()
        if pool != ZERO:
            venues.append({"name": f"uni_v3_{fee / 10000:.2f}%", "kind": "uni", "fee": fee, "pool": pool})
    aero_f = w3.eth.contract(address=AERO_FACTORY, abi=AERO_FACTORY_ABI)
    pool = aero_f.functions.getPool(WETH, USDC, False).call()
    if pool != ZERO:
        venues.append({"name": "aero_volatile", "kind": "aero", "pool": pool,
                       "contract": w3.eth.contract(address=pool, abi=AERO_POOL_ABI)})
    return venues


def encode_quote(venue, token_in, token_out, amount_in):
    if venue["kind"] == "uni":
        data = quoter.encode_abi("quoteExactInputSingle",
                                 args=[(token_in, token_out, amount_in, venue["fee"], 0)])
        target = UNI_QUOTER_V2
    else:
        data = venue["contract"].encode_abi("getAmountOut", args=[amount_in, token_in])
        target = venue["pool"]
    return (target, True, bytes.fromhex(data[2:]))


def decode_quote(venue, result):
    success, ret = result
    if not success or len(ret) < 32:
        return None
    return int.from_bytes(ret[:32], "big")  # first word = amountOut for both venue types


def batch(calls, block):
    return multicall.functions.aggregate3(calls).call(block_identifier=block)


def check(venues, writer):
    blk = w3.eth.get_block("latest")
    bn = blk["number"]
    gas_price_wei = blk["baseFeePerGas"] + int(PRIORITY_FEE_GWEI * 1e9)
    gas_cost_eth = GAS_UNITS * gas_price_wei / 1e18 + L1_FEE_ETH

    # Stage 1: sell WETH -> USDC on every venue, every size
    sell_keys, sell_calls = [], []
    for size in TRADE_SIZES_ETH:
        amt = int(size * 1e18)
        for v in venues:
            sell_keys.append((size, v["name"]))
            sell_calls.append(encode_quote(v, WETH, USDC, amt))
    sell_res = batch(sell_calls, bn)
    by_name = {v["name"]: v for v in venues}
    usdc_out = {k: decode_quote(by_name[k[1]], r) for k, r in zip(sell_keys, sell_res)}

    # Stage 2: buy WETH back on every other venue with that USDC
    buy_keys, buy_calls = [], []
    for size in TRADE_SIZES_ETH:
        for a in venues:
            u = usdc_out.get((size, a["name"]))
            if not u:
                continue
            for b in venues:
                if b["name"] == a["name"]:
                    continue
                buy_keys.append((size, a["name"], b["name"], u))
                buy_calls.append(encode_quote(b, USDC, WETH, u))
    buy_res = batch(buy_calls, bn) if buy_calls else []

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    best = None
    for (size, a, b, u), r in zip(buy_keys, buy_res):
        weth_back = decode_quote(by_name[b], r)
        if weth_back is None:
            continue
        weth_back_eth = weth_back / 1e18
        flash_fee_eth = size * FLASH_FEE_BPS / 10000
        gross = weth_back_eth - size
        net = gross - gas_cost_eth - flash_fee_eth
        eth_usd = (u / 1e6) / size
        row = [ts, bn, size, a, b, round(u / 1e6, 6), f"{weth_back_eth:.10f}",
               f"{gross:.10f}", f"{gas_cost_eth:.10f}", f"{net:.10f}", f"{net * eth_usd:.4f}"]
        writer.writerow(row)
        if best is None or net > best[0]:
            best = (net, net * eth_usd, size, a, b)

    if best:
        tag = "PROFIT" if best[0] > 0 else "no edge"
        print(f"[block {bn}] best: {best[2]} ETH  sell {best[3]} -> buy {best[4]}  "
              f"net ${best[1]:.4f}  ({tag})")


def main():
    print(f"Connected: {w3.is_connected()}  chain_id={w3.eth.chain_id}")
    venues = discover_venues()
    for v in venues:
        print(f"  venue {v['name']:<16} pool {v['pool']}")
    if len(venues) < 2:
        raise SystemExit("Need at least 2 venues to compare.")

    new_file = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["time_utc", "block", "size_eth", "sell_venue", "buy_venue",
                             "usdc_mid", "weth_back", "gross_eth", "gas_eth", "net_eth", "net_usd"])
        while True:
            try:
                check(venues, writer)
                f.flush()
            except Exception as e:
                print(f"[-] check failed: {e}")
            time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
