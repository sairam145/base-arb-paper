"""
Paper arbitrage on Base — $0, no wallet, no private key.

Every check:
  1. Quotes all pools at real trade sizes (scanner.py) and picks the best route.
  2. Runs that FULL trade on-chain for free: real Balancer flash loan -> sell -> buy back,
     using the PaperArb contract injected into an eth_call (never deployed).
  3. Subtracts real gas and logs the exact result to paper_trades.csv.

  python paper.py           run
  python paper.py report    per-day summary: trades, wins, $ earned
"""
import csv
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()
import scanner as s  # noqa: E402

w3 = s.w3
PAPER_CSV = os.getenv("PAPER_CSV", "paper_trades.csv")
MIN_PROFIT_USD = float(os.getenv("MIN_PROFIT_USD", "0.05"))
RUN_SECONDS = float(os.getenv("RUN_SECONDS", "0")) or None  # unset/0 = run forever
SIM_ADDR = s.cs("0x00000000000000000000000000000000000a4b17")
VENUE_INDEX = {"uni_v3_0.05%": 0, "uni_v3_0.30%": 1, "aero_volatile": 2}
CHECK_ADDRS = {
    "Balancer vault": "0xBA12222222228d8Ba445958a75a0704d566BF2C8",
    "Uniswap router": "0x2626664c2603336E57B271c5C0b26F421741e481",
    "Aerodrome router": "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43",
}

art = json.load(open("PaperArb.json"))
paper = w3.eth.contract(address=SIM_ADDR, abi=art["abi"])
OVERRIDE = {SIM_ADDR: {"code": art["runtime"]}}
SHORT_SELECTOR = w3.keccak(text="Short(uint256,uint256)")[:4].hex()


def best_route(venues):
    """Best (size, sell, buy) by quoted net profit this block."""
    blk = w3.eth.get_block("latest")
    bn, base = blk["number"], blk["baseFeePerGas"]
    gas_eth = s.GAS_UNITS * (base + int(s.PRIORITY_FEE_GWEI * 1e9)) / 1e18 + s.L1_FEE_ETH
    by = {v["name"]: v for v in venues}

    keys, calls = [], []
    for size in s.TRADE_SIZES_ETH:
        for v in venues:
            keys.append((size, v["name"]))
            calls.append(s.encode_quote(v, s.WETH, s.USDC, int(size * 1e18)))
    usdc = {k: s.decode_quote(by[k[1]], r) for k, r in zip(keys, s.batch(calls, bn))}

    keys, calls = [], []
    for size in s.TRADE_SIZES_ETH:
        for a in venues:
            u = usdc.get((size, a["name"]))
            for b in venues:
                if u and b["name"] != a["name"]:
                    keys.append((size, a["name"], b["name"], u))
                    calls.append(s.encode_quote(b, s.USDC, s.WETH, u))
    best = None
    for (size, a, b, u), r in zip(keys, s.batch(calls, bn) if calls else []):
        back = s.decode_quote(by[b], r)
        if back is None:
            continue
        net = back / 1e18 - size - gas_eth
        if best is None or net > best["quoted_net_eth"]:
            best = dict(block=bn, size=size, sell=a, buy=b, quoted_net_eth=net,
                        gas_eth=gas_eth, eth_usd=(u / 1e6) / size)
    return best


def simulate(opp):
    """Run the real trade in a free eth_call. Returns gross WETH profit (negative = loss)."""
    data = paper.encode_abi("simulate", args=[int(opp["size"] * 1e18),
                                              VENUE_INDEX[opp["sell"]], VENUE_INDEX[opp["buy"]]])
    try:
        out = w3.eth.call({"to": SIM_ADDR, "data": data}, opp["block"], OVERRIDE)
        return int.from_bytes(out[-32:], "big") / 1e18, "ok"
    except Exception as e:
        raw = getattr(e, "data", None) or (e.args[1] if len(e.args) > 1 else "")
        raw = raw if isinstance(raw, str) else str(raw)
        if SHORT_SELECTOR.removeprefix("0x") in raw:
            hexd = raw[raw.index(SHORT_SELECTOR.removeprefix("0x")) + 8:]
            have, owed = int(hexd[:64], 16), int(hexd[64:128], 16)
            return (have - owed) / 1e18, "ok"
        return None, f"sim_error: {str(e)[:60]}"


def log(row):
    new = not os.path.exists(PAPER_CSV)
    with open(PAPER_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_utc", "block", "size_eth", "sell", "buy", "quoted_net_usd",
                        "sim_gross_eth", "gas_eth", "net_usd", "result"])
        w.writerow(row)


def run():
    print(f"Connected: {w3.is_connected()}  chain_id={w3.eth.chain_id}")
    for name, a in CHECK_ADDRS.items():
        if len(w3.eth.get_code(s.cs(a))) == 0:
            sys.exit(f"No contract at {name} {a}. Stopping.")
    venues = [v for v in s.discover_venues() if v["name"] in VENUE_INDEX]
    print(f"Paper trading on {[v['name'] for v in venues]}  -> {PAPER_CSV}\n")
    deadline = time.monotonic() + RUN_SECONDS if RUN_SECONDS else None
    while True:
        if deadline and time.monotonic() >= deadline:
            print(f"RUN_SECONDS={RUN_SECONDS:g} reached, exiting cleanly.")
            return
        try:
            opp = best_route(venues)
            if opp:
                gross, status = simulate(opp)
                ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
                quoted_usd = opp["quoted_net_eth"] * opp["eth_usd"]
                if gross is None:
                    net_usd, result = 0.0, status
                else:
                    net_usd = (gross - opp["gas_eth"]) * opp["eth_usd"]
                    result = "WIN" if net_usd > MIN_PROFIT_USD else "no_trade"
                log([ts, opp["block"], opp["size"], opp["sell"], opp["buy"], f"{quoted_usd:.4f}",
                     f"{gross if gross is not None else 0:.10f}", f"{opp['gas_eth']:.10f}",
                     f"{net_usd:.4f}", result])
                print(f"[{opp['block']}] {opp['size']} ETH {opp['sell']} -> {opp['buy']}  "
                      f"net ${net_usd:.4f}  {result}")
        except Exception as e:
            print(f"[-] {e}")
        time.sleep(s.CHECK_INTERVAL_SEC)


def report():
    if not os.path.exists(PAPER_CSV):
        sys.exit("No paper trades yet. Run: python paper.py")
    days = defaultdict(lambda: {"checks": 0, "wins": 0, "usd": 0.0, "best": -1e9})
    for r in csv.DictReader(open(PAPER_CSV)):
        d = days[r["time_utc"][:10]]
        d["checks"] += 1
        n = float(r["net_usd"])
        d["best"] = max(d["best"], n)
        if r["result"] == "WIN":
            d["wins"] += 1
            d["usd"] += n
    print(f"{'date':<12}{'checks':>8}{'wins':>6}{'paper $':>10}{'best trade $':>14}")
    for date in sorted(days):
        d = days[date]
        print(f"{date:<12}{d['checks']:>8}{d['wins']:>6}{d['usd']:>10.2f}{d['best']:>14.4f}")
    print("\nTarget: $10/day. 'wins' = trades that would have made > MIN_PROFIT_USD after gas.")


if __name__ == "__main__":
    {"run": run, "report": report}[sys.argv[1] if len(sys.argv) > 1 else "run"]()
