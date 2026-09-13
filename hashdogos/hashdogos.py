#!/usr/bin/env python3
# ============================================================================
#  HashDogos local GPU miner (single file, NVIDIA GPU) — Robinhood Chain
#  On-chain keccak256 target-based proof-of-work -> mint (paid, mintPrice ETH each).
#
#  Deps:  pip install pyopencl eth-account numpy eth-hash[pycryptodome]
#  Usage: set environment variable PK=your wallet private key (0x...), then:
#           # dry run first (no tx sent; verifies kernel + simulation passes):
#           PK=0x... COUNT=1 python hashdogos.py
#           # real run (spends real money, ~mintPrice ETH each):
#           PK=0x... COUNT=10 PRICE_LOCK=480000000000000 CAP_ETH=0.006 DO=1 python hashdogos.py
#         COUNT=how many to mine;  PRICE_LOCK=wei (price must equal this; stop if it changes; 0=no lock);
#         CAP_ETH=total spend cap (stop when exceeded);  only DO=1 sends real txs, otherwise simulate only. Stop: Ctrl+C.
#
#  Principle: hash = keccak256( abiEncode(
#              address contract, uint256 chainId, address miner,
#              uint256 nonce, bytes32 previousWork, bytes32 anchor) )   (6×32=192 bytes)
#        valid <=> hash < currentTarget(miner)  (per-miner difficulty).
#        Submit mine(uint256 nonce, uint256 anchorBlock)  value=mintPrice.
#        previousWork changes every time someone mints -> this miner re-reads it each round;
#        the anchor has a freshness window; expired submissions revert.
#  ⚠️ Paid: mintPrice rises with global supply. Use PRICE_LOCK to lock the price; it stops
#     automatically if the price moves — it will never quietly overspend.
# ============================================================================
import os, sys, time, json, urllib.request
import numpy as np
import pyopencl as cl
from eth_account import Account
from eth_hash.auto import keccak

# ---- Config (edit here to switch projects; contract/selectors reverse-engineered
#      from the project's frontend /api/config + a successful tx — don't guess) ----
RPC      = "https://rpc.mainnet.chain.robinhood.com"
CONTRACT = "0x9464A2e848BCE4F16bD8589c426894535c33629A"
CHAIN_ID = 4663
SEL = {  # function selectors = keccak(sig)[:4]
    "price":    "0x6817c76c",  # mintPrice() -> uint256
    "prevWork": "0x8b73c652",  # previousWork() -> bytes32
    "anchor":   "0xcd809b11",  # currentAnchor() -> (uint256 anchorBlock, bytes32 anchor)
    "target":   "0x8b08fe33",  # currentTarget(address miner) -> uint256
    "mine":     "0x071e9503",  # mine(uint256 nonce, uint256 anchorBlock) payable
}

PK = os.environ.get("PK", "").strip()
if not PK: sys.exit("Please set the environment variable PK=your wallet private key (0x...)  (burner wallet only!)")
acct = Account.from_key(PK if PK.startswith("0x") else "0x" + PK); ADDR = acct.address
COUNT      = int(os.environ.get("COUNT", "1"))
CAP        = float(os.environ.get("CAP_ETH", "0.005"))
PRICE_LOCK = int(os.environ.get("PRICE_LOCK", "0"))     # wei; if non-zero, price must equal it
DO         = os.environ.get("DO", "0") == "1"           # real transactions only when DO=1

def rpc(m, p):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": p}).encode()
    # note: many RPCs return 403 without a User-Agent
    req = urllib.request.Request(RPC, data=body, headers={"content-type": "application/json", "user-agent": "Mozilla/5.0"})
    j = json.loads(urllib.request.urlopen(req, timeout=25).read())
    if "error" in j: raise RuntimeError(j["error"])
    return j["result"]
def call(data, to=CONTRACT): return rpc("eth_call", [{"to": to, "data": data}, "latest"])
def read_state():
    price      = int(call(SEL["price"]), 16)
    prevWork   = call(SEL["prevWork"])                                    # bytes32
    an         = call(SEL["anchor"]); anchorBlock = int(an[2:66], 16); anchor = "0x" + an[66:130]
    tgt        = int(call(SEL["target"] + ADDR[2:].lower().rjust(64, "0")), 16)
    return price, prevWork, anchorBlock, anchor, tgt

# ---- Preimage (standard abiEncode, 6×32=192 bytes) ----
def preimage(prevWork, anchor, nonce):
    def w_addr(a): return bytes(12) + bytes.fromhex(a[2:])
    def w_uint(n): return int(n).to_bytes(32, "big")
    def w_b32(h):  return bytes.fromhex(h[2:].rjust(64, "0"))
    buf = w_addr(CONTRACT) + w_uint(CHAIN_ID) + w_addr(ADDR) + w_uint(nonce) + w_b32(prevWork) + w_b32(anchor)
    assert len(buf) == 192, len(buf)
    return buf
def lanes_for(prevWork, anchor):
    buf = preimage(prevWork, anchor, 0)
    return [int.from_bytes(buf[i:i + 8], "little") for i in range(0, 192, 8)]  # 24 little-endian lanes
def keccak_hi64(prevWork, anchor, nonce):
    h = keccak(preimage(prevWork, anchor, nonce)); return h, int.from_bytes(h[:8], "big")

# ---- OpenCL keccak256 (only the nonce changes: word3=lane15=bswap64(nonce)) ----
# Multi-GPU: every device on every platform is used, one thread per GPU.
GPUS = []
for p in cl.get_platforms():
    for d in p.get_devices():
        GPUS.append(d)
if not GPUS: sys.exit("❌ no OpenCL devices found")
CTXS = {d: cl.Context([d]) for d in GPUS}
print(f"GPUs: {len(GPUS)}  wallet {ADDR}", flush=True)
for i, d in enumerate(GPUS):
    print(f"  [gpu{i}] {d.name} {d.max_compute_units}CU", flush=True)


KSRC = r"""
__constant ulong RC[24]={0x0000000000000001UL,0x0000000000008082UL,0x800000000000808aUL,0x8000000080008000UL,
0x000000000000808bUL,0x0000000080000001UL,0x8000000080008081UL,0x8000000000008009UL,0x000000000000008aUL,
0x0000000000000088UL,0x0000000080008009UL,0x000000008000000aUL,0x000000008000808bUL,0x800000000000008bUL,
0x8000000000008089UL,0x8000000000008003UL,0x8000000000008002UL,0x8000000000000080UL,0x000000000000800aUL,
0x800000008000000aUL,0x8000000080008081UL,0x8000000000008080UL,0x0000000080000001UL,0x8000000080008008UL};
__constant int ROTC[24]={1,3,6,10,15,21,28,36,45,55,2,14,27,41,56,8,25,43,62,18,39,61,20,44};
__constant int PILN[24]={10,7,11,17,18,3,5,16,8,21,24,4,15,23,19,13,12,2,20,14,22,9,6,1};
#define R64(x,y) rotate((ulong)(x),(ulong)(y))
inline ulong bswap64(ulong x){
  return ((x&0xffUL)<<56)|((x&0xff00UL)<<40)|((x&0xff0000UL)<<24)|((x&0xff000000UL)<<8)
       |((x>>8)&0xff000000UL)|((x>>24)&0xff0000UL)|((x>>40)&0xff00UL)|((x>>56)&0xffUL);
}
inline void keccakf(ulong* s){
  ulong bc[5],t;
  for(int r=0;r<24;r++){
    for(int i=0;i<5;i++) bc[i]=s[i]^s[i+5]^s[i+10]^s[i+15]^s[i+20];
    for(int i=0;i<5;i++){ t=bc[(i+4)%5]^R64(bc[(i+1)%5],1); for(int j=0;j<25;j+=5) s[j+i]^=t; }
    t=s[1];
    for(int i=0;i<24;i++){ int j=PILN[i]; bc[0]=s[j]; s[j]=R64(t,ROTC[i]); t=bc[0]; }
    for(int j=0;j<25;j+=5){ for(int i=0;i<5;i++) bc[i]=s[j+i]; for(int i=0;i<5;i++) s[j+i]^=(~bc[(i+1)%5])&bc[(i+2)%5]; }
    s[0]^=RC[r];
  }
}
// L0..L23 baked into the source; block1=L0..16 (lane15=nonce), block2=L17..23 + keccak pad (0x01/0x80).
__kernel void mine(ulong base,__global volatile int* found,__global ulong* out,__global ulong* dbg){
  ulong nonce = base + (ulong)get_global_id(0)*__ITERS__;
  ulong Lc[24]={ __LANES__ };
  for(uint it=0; it<__ITERS__; it++){
    if(*found) return;
    ulong s[25]; for(int i=0;i<25;i++) s[i]=0;
    for(int i=0;i<17;i++) s[i]^= (i==15)? bswap64(nonce) : Lc[i];
    keccakf(s);
    for(int i=0;i<7;i++) s[i]^=Lc[17+i];
    s[7]^=0x0000000000000001UL; s[16]^=0x8000000000000000UL;
    keccakf(s);
    ulong hi=bswap64(s[0]);
    if(get_global_id(0)==0 && it==0) dbg[0]=s[0];        // self-test: lane0 of the first nonce
    if(hi < __THI__){ if(atomic_cmpxchg(found,0,1)==0){ out[0]=nonce; } return; }
    nonce++;
  }
}
"""
ITERS = 1024

# ---- multi-GPU mining: one thread per GPU, disjoint nonce ranges ----
import threading
from collections import namedtuple

GpuState = namedtuple("GpuState", "dev ctx q prg ker fg og dg found out dbg")

def gpu_worker(idx, dev, prevWork, anchor, tgt, base, stop_evt, result, stats, t_start):
    """Runs on its own thread. Writes (nonce) into result[0] when found. stats[idx] = hashes done."""
    ctx = CTXS[dev]; q = cl.CommandQueue(ctx)
    lanes = lanes_for(prevWork, anchor); THI = tgt >> 192   # valid <=> hash high-64 < target high-64 (exact)
    src = (KSRC.replace("__LANES__", ",".join(f"{l}UL" for l in lanes))
               .replace("__ITERS__", str(ITERS)).replace("__THI__", f"{THI}UL"))
    prg = cl.Program(ctx, src).build()
    mf = cl.mem_flags; found = np.zeros(1, np.int32); out = np.zeros(1, np.uint64); dbg = np.zeros(1, np.uint64)
    fg = cl.Buffer(ctx, mf.READ_WRITE | mf.COPY_HOST_PTR, hostbuf=found)
    og = cl.Buffer(ctx, mf.READ_WRITE | mf.COPY_HOST_PTR, hostbuf=out)
    dg = cl.Buffer(ctx, mf.READ_WRITE | mf.COPY_HOST_PTR, hostbuf=dbg)
    ker = cl.Kernel(prg, "mine")
    GLOBAL = 1 << 20; per = GLOBAL * ITERS
    checked = False; last_report = time.time()
    try:
        while not stop_evt.is_set() and not result[0]:
            ker(q, (GLOBAL,), None, np.uint64(base), fg, og, dg); q.finish()
            cl.enqueue_copy(q, found, fg); cl.enqueue_copy(q, dbg, dg); q.finish()
            if not checked:  # self-test: GPU keccak must match eth_hash bit-for-bit, otherwise abort all
                ref = keccak_hi64(prevWork, anchor, base)[0]; ref_l0 = int.from_bytes(ref[:8], "little")
                if int(dbg[0]) != ref_l0:
                    print(f"  ❌ [gpu{idx}] keccak kernel self-test FAILED! {int(dbg[0]):#018x} != {ref_l0:#018x}", flush=True)
                    result[1] = f"gpu{idx} self-test failed"; stop_evt.set(); return
                print(f"  ✓ [gpu{idx}] keccak kernel self-test passed (lane0={ref_l0:#018x})", flush=True); checked = True
            stats[idx] += per; base = (base + per) & ((1 << 64) - 1)
            if found[0]:
                cl.enqueue_copy(q, out, og); q.finish()
                result[0] = int(out[0]); stop_evt.set(); return
            now = time.time()
            if now - last_report >= 5:
                tot = sum(stats); print(f"  {tot/(now-t_start)/1e9:.2f} GH/s total · searched {tot/1e9:.1f}e9 (expected {(2**256)/tgt/1e9:.1f}e9)", flush=True)
                last_report = now
    except Exception as e:
        print(f"  [gpu{idx}] error: {e}", flush=True)
        result[1] = f"gpu{idx}: {e}"; stop_evt.set()

def mine(prevWork, anchor, tgt, stale, max_s=180):
    stop_evt = threading.Event()
    result = [None, None]          # [winning nonce or None, error or None]
    stats = [0] * len(GPUS)
    t_start = time.time()
    SPACE = 1 << 57                # disjoint nonce space per GPU (no overlap, no double work)
    base0 = int.from_bytes(os.urandom(6), "big")
    threads = []
    for idx, dev in enumerate(GPUS):
        t = threading.Thread(target=gpu_worker,
                             args=(idx, dev, prevWork, anchor, tgt, (base0 + idx * SPACE) & ((1 << 64) - 1),
                                   stop_evt, result, stats, t_start), daemon=True)
        t.start(); threads.append(t)
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.3)
            now = time.time()
            if result[0] is not None or result[1]: break
            if stale(): print("  challenge changed → re-mining", flush=True); stop_evt.set(); return None
            if now - t_start > max_s: print("  timed out; re-reading state and re-mining", flush=True); stop_evt.set(); return None
    finally:
        stop_evt.set()
        for t in threads: t.join(timeout=5)
    if result[1]: raise SystemExit(f"❌ {result[1]}")
    return result[0]


def encode_mine(nonce, anchorBlock):
    return SEL["mine"] + int(nonce).to_bytes(32, "big").hex() + int(anchorBlock).to_bytes(32, "big").hex()

spent = 0.0; got = 0
try:
    while got < COUNT:
        price, prevWork, anchorBlock, anchor, tgt = read_state()
        if PRICE_LOCK and price != PRICE_LOCK:
            print(f"⛔ mintPrice changed to {price/1e18} ETH (≠ locked {PRICE_LOCK/1e18}); stopping as instructed. Minted {got}, spent {spent:.5f} ETH."); break
        if got == 0:
            print(f"price {price/1e18} ETH · target {tgt:#x} · expected {(2**256)/tgt:.2e} hashes · anchorBlock {anchorBlock}", flush=True)
        gp = int(rpc("eth_gasPrice", []), 16); proj = spent + price/1e18 + 600000*gp/1e18
        if proj > CAP:
            print(f"⛔ projected spend {proj:.5f} > cap {CAP} ETH; stopping. Minted {got}, spent {spent:.5f} ETH."); break
        print(f"[{time.strftime('%H:%M:%S')}] mint #{got+1}/{COUNT} · prevWork {prevWork[:14]} · anchor {anchor[:14]}", flush=True)
        pw0 = prevWork
        def stale():
            try: return call(SEL["prevWork"]).lower() != pw0.lower()
            except Exception: return False   # RPC hiccups don't count as stale; keep mining
        nonce = mine(prevWork, anchor, tgt, stale)
        if nonce is None: continue
        h, hi = keccak_hi64(prevWork, anchor, nonce)
        if hi >= (tgt >> 192): print(f"  local recheck failed; re-mining"); continue
        data = encode_mine(nonce, anchorBlock)
        try:  # pre-submit eth_call simulation (authoritative gate: wrong solution/expired anchor reverts at zero cost)
            rpc("eth_call", [{"from": ADDR, "to": CONTRACT, "data": data, "value": hex(price)}, "latest"])
        except Exception as e:
            print(f"  simulation reverted (anchor expired/sniped?); re-reading and re-mining: {str(e)[:80]}"); continue
        print(f"  ✓ nonce found {nonce} · keccak {h.hex()[:18]} · simulation passed", flush=True)
        if not DO:
            print(f"  [DRY] not sent (set DO=1 for real). value {price/1e18} ETH"); got += 1; continue
        tx = {"nonce": int(rpc("eth_getTransactionCount", [ADDR, "pending"]), 16), "to": CONTRACT, "value": price,
              "gas": 600000, "maxFeePerGas": gp*2, "maxPriorityFeePerGas": gp, "data": data, "chainId": CHAIN_ID}
        signed = acct.sign_transaction(tx); txh = rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])
        rc = None
        for _ in range(50):
            rc = rpc("eth_getTransactionReceipt", [txh])
            if rc: break
            time.sleep(0.3)
        if rc and rc.get("status") == "0x1":
            gas_cost = int(rc["gasUsed"], 16) * int(rc.get("effectiveGasPrice", hex(gp)), 16) / 1e18
            spent += price/1e18 + gas_cost; got += 1
            print(f"  ✓✓ mint SUCCESS! tx {txh} · gas {int(rc['gasUsed'],16)} · total spent {spent:.5f} ETH")
        else:
            print(f"  ✗ mint reverted; re-mining · {txh}")
    print(f"\n=== done · minted {got}/{COUNT} · total spent {spent:.5f} ETH ===")
except KeyboardInterrupt:
    print(f"\nstopped · minted {got} · spent {spent:.5f} ETH")
