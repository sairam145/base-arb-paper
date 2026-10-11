"""
Multi-coin paper arbitrage on Base — $0, no wallet, no private key.

Each check, for every coin in COINS:
  1. Quote WETH -> COIN on every pool and COIN -> WETH back on every other pool,
     at real trade sizes (fees + slippage included), minus gas.
  2. Any route that clears MIN_PROFIT_USD is run as a FULL trade for free
     (Balancer flash loan -> sell -> buy back) via PaperArb injected into eth_call.
  3. Confirmed winners go to paper_trades.csv. Per-coin stats go to status.json.

  python paper.py           run (RUN_SECONDS=N to stop after N seconds)
  python paper.py report    per-day summary of winning trades
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
import scanner as s  # noqa: E402  (reuses w3, encode_quote, decode_quote, batch)

w3 = s.w3
cs = s.cs
PAPER_CSV = os.getenv("PAPER_CSV", "paper_trades.csv")
STATUS_JSON = os.getenv("STATUS_JSON", "status.json")
MIN_PROFIT_USD = float(os.getenv("MIN_PROFIT_USD", "0.05"))
RUN_SECONDS = float(os.getenv("RUN_SECONDS", "0")) or None
SIZES = [float(x) for x in os.getenv("PAPER_SIZES_ETH", "0.1,0.5,2").split(",")]
MAX_SIMS_PER_CHECK = int(os.getenv("MAX_SIMS_PER_CHECK", "5"))
BATCH_CHUNK = 40

# symbol must match on-chain symbol() or the coin is skipped (guards against wrong addresses)
COINS = {
    "USDC":    "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "cbETH":   "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22",
    "cbBTC":   "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
    "AERO":    "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
    "DEGEN":   "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
    "BRETT":   "0x532f27101965dd16442E59d40670FaF5eBB142E4",
    "VIRTUAL": "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b",
    "TOSHI":   "0xAC1Bd2486aAf3B5C0fc3Fd868558b082a531B2B4",
}
UNI_FEES = {500: 0, 3000: 1, 10000: 2}   # fee tier -> venue id in PaperArb
AERO_VENUE = 3
CHECK_ADDRS = {
    "Balancer vault": "0xBA12222222228d8Ba445958a75a0704d566BF2C8",
    "Uniswap router": "0x2626664c2603336E57B271c5C0b26F421741e481",
    "Aerodrome router": "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43",
}
SYMBOL_ABI = [{"name": "symbol", "type": "function", "stateMutability": "view",
               "inputs": [], "outputs": [{"name": "", "type": "string"}]}]

SIM_ADDR = cs("0x00000000000000000000000000000000000a4b17")
art = json.load(open("PaperArb.json"))
paper = w3.eth.contract(address=SIM_ADDR, abi=art["abi"])
OVERRIDE = {SIM_ADDR: {"code": art["runtime"]}}
SHORT_SEL = w3.keccak(text="Short(uint256,uint256)")[:4].hex().removeprefix("0x")
ETH_USD_VENUE = {"name": "uni_0.05%", "kind": "uni", "fee": 500, "id": 0}


# ---------------- discovery ----------------
def discover():
    uni_f = w3.eth.contract(address=s.UNI_FACTORY, abi=s.UNI_FACTORY_ABI)
    aero_f = w3.eth.contract(address=s.AERO_FACTORY, abi=s.AERO_FACTORY_ABI)
    coins = {}
    for sym, addr in COINS.items():
        addr = cs(addr)
        try:
            onchain = w3.eth.contract(address=addr, abi=SYMBOL_ABI).functions.symbol().call()
        except Exception:
            onchain = None
        if onchain != sym:
            print(f"  skip {sym}: on-chain symbol is {onchain!r}")
            continue
        venues = []
        for fee, vid in UNI_FEES.items():
            if uni_f.functions.getPool(s.WETH, addr, fee).call() != s.ZERO:
                venues.append({"name": f"uni_{fee / 10000:g}%", "kind": "uni", "fee": fee, "id": vid})
        pool = aero_f.functions.getPool(s.WETH, addr, False).call()
        if pool != s.ZERO:
            venues.append({"name": "aero", "kind": "aero", "pool": pool, "id": AERO_VENUE,
                           "contract": w3.eth.contract(address=pool, abi=s.AERO_POOL_ABI)})
        if len(venues) < 2:
            print(f"  skip {sym}: only {len(venues)} pool(s)")
            continue
        coins[sym] = {"addr": addr, "venues": venues}
        print(f"  {sym:<8} pools: {', '.join(v['name'] for v in venues)}")
    return coins


def chunked_batch(calls, block):
    out = []
    for i in range(0, len(calls), BATCH_CHUNK):
        out.extend(s.batch(calls[i:i + BATCH_CHUNK], block))
    return out


# ---------------- one check ----------------
def scan(coins):
    blk = w3.eth.get_block("latest")
    bn, base = blk["number"], blk["baseFeePerGas"]
    gas_eth = s.GAS_UNITS * (base + int(s.PRIORITY_FEE_GWEI * 1e9)) / 1e18 + s.L1_FEE_ETH

    keys = [("ethusd",)]
    calls = [s.encode_quote(ETH_USD_VENUE, s.WETH, s.USDC, 10**18)]
    for sym, c in coins.items():
        for size in SIZES:
            for v in c["venues"]:
                keys.append((sym, size, v["name"]))
                calls.append(s.encode_quote(v, s.WETH, c["addr"], int(size * 1e18)))
    res = chunked_batch(calls, bn)
    eth_usd = (s.decode_quote(ETH_USD_VENUE, res[0]) or 0) / 1e6
    vby = {sym: {v["name"]: v for v in c["venues"]} for sym, c in coins.items()}
    out1 = {k: s.decode_quote(vby[k[0]][k[2]], r) for k, r in zip(keys[1:], res[1:])}

    keys, calls = [], []
    for sym, c in coins.items():
        for size in SIZES:
            for a in c["venues"]:
                got = out1.get((sym, size, a["name"]))
                if not got:
                    continue
                for b in c["venues"]:
                    if b["name"] != a["name"]:
                        keys.append((sym, size, a["name"], b["name"]))
                        calls.append(s.encode_quote(b, c["addr"], s.WETH, got))
    routes = []
    for (sym, size, a, b), r in zip(keys, chunked_batch(calls, bn) if calls else []):
        back = s.decode_quote(vby[sym][b], r)
        if back is None:
            continue
        net_eth = back / 1e18 - size - gas_eth
        routes.append(dict(coin=sym, size=size, sell=a, buy=b, net_eth=net_eth,
                           net_usd=net_eth * eth_usd))
    return bn, gas_eth, eth_usd, routes, vby


def simulate(coins, vby, route, block):
    data = paper.encode_abi("simulate", args=[
        int(route["size"] * 1e18), coins[route["coin"]]["addr"],
        vby[route["coin"]][route["sell"]]["id"], vby[route["coin"]][route["buy"]]["id"]])
    try:
        out = w3.eth.call({"to": SIM_ADDR, "data": data}, block, OVERRIDE)
        return int.from_bytes(out[-32:], "big") / 1e18
    except Exception as e:
        raw = str(getattr(e, "data", "") or e)
        if SHORT_SEL in raw:
            h = raw[raw.index(SHORT_SEL) + 8:]
            return (int(h[:64], 16) - int(h[64:128], 16)) / 1e18
        return None


# ---------------- output ----------------
def log_win(row):
    new = not os.path.exists(PAPER_CSV)
    with open(PAPER_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_utc", "block", "coin", "size_eth", "sell", "buy",
                        "net_usd", "gross_eth", "gas_eth", "eth_usd"])
        w.writerow(row)


def load_status():
    try:
        return json.load(open(STATUS_JSON))
    except Exception:
        return {"total_checks": 0, "total_wins": 0, "coins": {}}


def save_status(st):
    tmp = STATUS_JSON + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATUS_JSON)


# ---------------- main loop ----------------
def run():
    print(f"Connected: {w3.is_connected()}  chain_id={w3.eth.chain_id}")
    for name, a in CHECK_ADDRS.items():
        if len(w3.eth.get_code(cs(a))) == 0:
            sys.exit(f"No contract at {name} {a}. Stopping.")
    coins = discover()
    if not coins:
        sys.exit("No coins with 2+ pools found.")

    st = load_status()
    st.update(run_started_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
              checks_this_run=0, wins_this_run=0, interval_sec=s.CHECK_INTERVAL_SEC)
    for sym, c in coins.items():
        cst = st["coins"].setdefault(sym, {"checks": 0, "wins": 0, "best_ever_usd": None})
        cst.update(pools=[v["name"] for v in c["venues"]], best_this_run_usd=None, best_route=None)

    deadline = time.monotonic() + RUN_SECONDS if RUN_SECONDS else None
    while not (deadline and time.monotonic() >= deadline):
        try:
            bn, gas_eth, eth_usd, routes, vby = scan(coins)
            best_by_coin = {}
            for r in routes:
                if r["coin"] not in best_by_coin or r["net_usd"] > best_by_coin[r["coin"]]["net_usd"]:
                    best_by_coin[r["coin"]] = r

            wins = 0
            candidates = sorted([r for r in routes if r["net_usd"] > MIN_PROFIT_USD],
                                key=lambda r: -r["net_usd"])[:MAX_SIMS_PER_CHECK]
            for r in candidates:
                gross = simulate(coins, vby, r, bn)
                if gross is None:
                    continue
                net_usd = (gross - gas_eth) * eth_usd
                if net_usd > MIN_PROFIT_USD:
                    wins += 1
                    st["coins"][r["coin"]]["wins"] += 1
                    log_win([datetime.now(timezone.utc).isoformat(timespec="seconds"), bn, r["coin"],
                             r["size"], r["sell"], r["buy"], f"{net_usd:.4f}", f"{gross:.10f}",
                             f"{gas_eth:.10f}", f"{eth_usd:.2f}"])
                    print(f"  WIN {r['coin']} {r['size']} ETH {r['sell']}->{r['buy']} +${net_usd:.4f}")

            st["checks_this_run"] += 1
            st["total_checks"] += 1
            st["wins_this_run"] += wins
            st["total_wins"] += wins
            st["eth_usd"] = round(eth_usd, 2)
            st["last_check_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            st["last_block"] = bn
            for sym, r in best_by_coin.items():
                cst = st["coins"][sym]
                cst["checks"] += 1
                v = round(r["net_usd"], 4)
                if cst["best_this_run_usd"] is None or v > cst["best_this_run_usd"]:
                    cst["best_this_run_usd"] = v
                    cst["best_route"] = f"{r['size']} ETH {r['sell']} -> {r['buy']}"
                if cst["best_ever_usd"] is None or v > cst["best_ever_usd"]:
                    cst["best_ever_usd"] = v
                cst["last_usd"] = v
            line = "  ".join(f"{k} {v['net_usd']:+.2f}" for k, v in best_by_coin.items())
            print(f"[{bn}] wins={wins}  best per coin $: {line}")
            if st["checks_this_run"] % 10 == 0:
                save_status(st)
        except Exception as e:
            print(f"[-] {str(e)[:200]}")
        time.sleep(s.CHECK_INTERVAL_SEC)
    save_status(st)
    print(f"RUN_SECONDS reached. checks={st['checks_this_run']} wins={st['wins_this_run']}")


def report():
    if not os.path.exists(PAPER_CSV):
        sys.exit("No winning trades yet.")
    days = defaultdict(lambda: {"wins": 0, "usd": 0.0})
    for r in csv.DictReader(open(PAPER_CSV)):
        d = days[(r["time_utc"][:10], r["coin"])]
        d["wins"] += 1
        d["usd"] += float(r["net_usd"])
    print(f"{'date':<12}{'coin':<9}{'wins':>6}{'paper $':>10}")
    for (date, coin) in sorted(days):
        d = days[(date, coin)]
        print(f"{date:<12}{coin:<9}{d['wins']:>6}{d['usd']:>10.2f}")


if __name__ == "__main__":
    {"run": run, "report": report}[sys.argv[1] if len(sys.argv) > 1 else "run"]()
